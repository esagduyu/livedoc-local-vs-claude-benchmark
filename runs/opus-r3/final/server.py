"""Livedoc server: real-time collaborative plain-text editing.

Usage: python3 server.py --port PORT --data-dir DIR
"""

import argparse
import asyncio
import json
import os
import sys

from livedoc_client import OpError, apply_op, check_op, move_cursor, transform

MAX_LINE = 16 * 1024 * 1024  # longest accepted request line, in bytes
MAX_OUTBUF = 64 * 1024 * 1024  # drop connections that stop reading
LOG_NAME = "ops.jsonl"
CHECKPOINT_EVERY = 100  # keep the full text of every Nth revision for snapshots


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class RequestError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Document:
    def __init__(self, name):
        self.name = name
        self.text = ""
        self.history = []  # committed entries {"rev", "client", "id", "op"}; rev r at index r-1
        self.lengths = [0]  # text length at each revision
        self.committed = {}  # (client, id) -> rev
        self.subscribers = {}  # Connection -> None (insertion-ordered set)
        self.checkpoints = [""]  # text at revisions 0, N, 2N, ... (N = CHECKPOINT_EVERY)
        self.members = {}  # client name -> number of joined connections
        self.cursors = {}  # client name -> position at the current revision

    @property
    def rev(self):
        return len(self.history)

    def commit(self, entry):
        op = entry["op"]
        self.text = apply_op(self.text, op)
        self.history.append(entry)
        self.lengths.append(len(self.text))
        self.committed[(entry["client"], entry["id"])] = entry["rev"]
        if entry["rev"] % CHECKPOINT_EVERY == 0:
            self.checkpoints.append(self.text)
        author = entry["client"]
        for name, pos in self.cursors.items():
            self.cursors[name] = move_cursor(pos, op, name == author)

    def text_at(self, rev):
        if rev == self.rev:
            return self.text
        base = rev // CHECKPOINT_EVERY
        text = self.checkpoints[base]
        for entry in self.history[base * CHECKPOINT_EVERY:rev]:
            text = apply_op(text, entry["op"])
        return text

    def broadcast(self, msg, skip=None):
        for conn in list(self.subscribers):
            if conn is not skip:
                conn.send(msg)


class Store:
    """All documents, backed by one append-only JSON-lines log of commits."""

    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, LOG_NAME)
        self.docs = {}
        self.fd = None
        self._replay()
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def get(self, name, create=True):
        """Return the document, creating it (persistently) if `create`."""
        doc = self.docs.get(name)
        if doc is None and create:
            self._write({"doc": name, "create": True})
            doc = self.docs[name] = Document(name)
        return doc

    def _replay(self):
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return
        good_end = 0
        pos = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl < 0:
                break  # torn final line from a crash mid-write
            line = data[pos:nl]
            pos = nl + 1
            try:
                rec = json.loads(line)
                if not isinstance(rec["doc"], str):
                    raise ValueError("bad doc name")
                doc = self.docs.get(rec["doc"])
                if doc is None:
                    doc = self.docs[rec["doc"]] = Document(rec["doc"])
                if rec.get("create"):
                    good_end = pos
                    continue
                if rec["rev"] != doc.rev + 1:
                    raise ValueError("revision out of sequence")
                op, _ = check_op(rec["op"], len(doc.text))
                if not isinstance(rec["client"], str) or not isinstance(rec["id"], str):
                    raise ValueError("bad client or id")
                doc.commit({"rev": rec["rev"], "client": rec["client"], "id": rec["id"], "op": op})
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                print(f"livedoc: skipping bad log record: {e}", file=sys.stderr)
                continue
            good_end = pos
        if good_end < len(data):
            # Drop an incomplete tail so later appends start on a fresh line.
            with open(self.path, "r+b") as f:
                f.truncate(good_end)

    def append(self, doc, entry):
        self._write({"doc": doc.name, **entry})

    def _write(self, rec):
        data = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]


class Connection(asyncio.Protocol):
    def __init__(self, server):
        self.server = server
        self.transport = None
        self.buf = bytearray()
        self.discarding = False  # skipping the rest of an over-long line
        self.joined = {}  # doc name -> client name

    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        for name, client in self.joined.items():
            doc = self.server.store.docs[name]
            doc.subscribers.pop(self, None)
            self._release(doc, client)
        self.joined.clear()

    @staticmethod
    def _release(doc, client):
        """One fewer connection for `client` on `doc`; the last one leaves."""
        doc.members[client] -= 1
        if doc.members[client] == 0:
            del doc.members[client]
            doc.cursors.pop(client, None)
            doc.broadcast({"type": "leave", "doc": doc.name, "client": client})

    def data_received(self, data):
        start = 0
        while True:
            nl = data.find(b"\n", start)
            if nl < 0:
                break
            chunk = data[start:nl]
            start = nl + 1
            if self.discarding:
                self.discarding = False
                self.buf.clear()
                continue
            if self.buf:
                self.buf += chunk
                line = bytes(self.buf)
                self.buf.clear()
            else:
                line = chunk
            self.handle_line(line)
            if self.transport.is_closing():
                return
        if not self.discarding:
            self.buf += data[start:]
            if len(self.buf) > MAX_LINE:
                self.buf.clear()
                self.discarding = True
                self.send({"type": "error", "code": "bad_request",
                           "message": f"line longer than {MAX_LINE} bytes"})

    def send(self, msg):
        t = self.transport
        if t is None or t.is_closing():
            return
        t.write((json.dumps(msg) + "\n").encode())
        if t.get_write_buffer_size() > MAX_OUTBUF:
            t.abort()  # a client that never reads must not exhaust memory

    def handle_line(self, line):
        if not line.strip():
            return
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            msg = None
        if not isinstance(msg, dict):
            self.send({"type": "error", "code": "bad_json", "message": "line is not a JSON object"})
            return
        try:
            kind = msg.get("type")
            if kind == "join":
                self.handle_join(msg)
            elif kind == "op":
                self.handle_op(msg)
            elif kind == "cursor":
                self.handle_cursor(msg)
            elif kind == "cursors":
                self.handle_cursors(msg)
            elif kind == "snapshot":
                self.handle_snapshot(msg)
            else:
                raise RequestError("bad_request", f"unknown type {kind!r}")
        except RequestError as e:
            err = {"type": "error", "code": e.code, "message": str(e)}
            if "id" in msg:
                err["id"] = msg["id"]
            self.send(err)

    @staticmethod
    def _doc_name(msg):
        name = msg.get("doc")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise RequestError("bad_request", "'doc' must be a string of 1 to 100 characters")
        return name

    def handle_join(self, msg):
        name = self._doc_name(msg)
        client = msg.get("client")
        if not isinstance(client, str) or not client:
            raise RequestError("bad_request", "'client' must be a non-empty string")
        since = msg.get("since")
        if since is not None and not _is_int(since):
            raise RequestError("bad_request", "'since' must be an integer")
        doc = self.server.store.get(name, create=False)
        current = doc.rev if doc is not None else 0
        if since is not None and not 0 <= since <= current:
            raise RequestError("bad_rev", f"'since' must be between 0 and {current}")
        if doc is None:
            doc = self.server.store.get(name)
        reply = {"type": "joined", "doc": name, "rev": doc.rev}
        if since is None:
            reply["text"] = doc.text
        else:
            reply["ops"] = doc.history[since:]
        self.send(reply)
        old = self.joined.get(name)
        if old != client:
            self.joined[name] = client
            doc.members[client] = doc.members.get(client, 0) + 1
            if old is not None:  # same connection rejoining under a new name
                self._release(doc, old)
        doc.subscribers[self] = None

    def handle_op(self, msg):
        name = self._doc_name(msg)
        rev = msg.get("rev")
        op_id = msg.get("id")
        if not _is_int(rev):
            raise RequestError("bad_request", "'rev' must be an integer")
        if not isinstance(op_id, str):
            raise RequestError("bad_request", "'id' must be a string")
        if "op" not in msg:
            raise RequestError("bad_request", "missing 'op'")
        try:
            check_op(msg["op"], float("inf"))  # shape only; bounds come later
        except OpError as e:
            if e.kind == "bad_request":
                raise RequestError(e.kind, str(e)) from None
        if name not in self.joined:
            raise RequestError("not_joined", f"not joined to {name!r}")
        client = self.joined[name]
        doc = self.server.store.docs[name]

        done = doc.committed.get((client, op_id))
        if done is not None:
            self.send({"type": "ack", "doc": name, "id": op_id, "rev": done})
            return
        if not 0 <= rev <= doc.rev:
            raise RequestError("bad_rev", f"'rev' must be between 0 and {doc.rev}")
        try:
            op, _ = check_op(msg["op"], doc.lengths[rev])
        except OpError as e:
            raise RequestError(e.kind, str(e)) from None

        for entry in doc.history[rev:]:
            op, _ = transform(op, entry["op"])
        entry = {"rev": doc.rev + 1, "client": client, "id": op_id, "op": op}
        self.server.store.append(doc, entry)  # on disk before it is applied or announced
        doc.commit(entry)

        doc.broadcast({"type": "op", "doc": name, "rev": entry["rev"], "client": client,
                       "id": op_id, "op": op}, skip=self)
        self.send({"type": "ack", "doc": name, "id": op_id, "rev": entry["rev"]})

    def _joined_doc(self, name):
        if name not in self.joined:
            raise RequestError("not_joined", f"not joined to {name!r}")
        return self.joined[name], self.server.store.docs[name]

    def handle_cursor(self, msg):
        name = self._doc_name(msg)
        rev, pos = msg.get("rev"), msg.get("pos")
        if not _is_int(rev) or not _is_int(pos):
            raise RequestError("bad_request", "'rev' and 'pos' must be integers")
        client, doc = self._joined_doc(name)
        if not 0 <= rev <= doc.rev:
            raise RequestError("bad_rev", f"'rev' must be between 0 and {doc.rev}")
        if not 0 <= pos <= doc.lengths[rev]:
            raise RequestError("out_of_range", f"position {pos} outside text of length {doc.lengths[rev]}")
        for entry in doc.history[rev:]:
            pos = move_cursor(pos, entry["op"], entry["client"] == client)
        doc.cursors[client] = pos
        doc.broadcast({"type": "cursor", "doc": name, "client": client, "rev": doc.rev, "pos": pos},
                      skip=self)

    def handle_cursors(self, msg):
        name = self._doc_name(msg)
        _, doc = self._joined_doc(name)
        self.send({"type": "cursors", "doc": name, "rev": doc.rev, "cursors": dict(doc.cursors)})

    def handle_snapshot(self, msg):
        name = self._doc_name(msg)
        rev = msg.get("rev")
        if not _is_int(rev):
            raise RequestError("bad_request", "'rev' must be an integer")
        doc = self.server.store.get(name, create=False)
        if doc is None:
            raise RequestError("not_found", f"no document {name!r}")
        if not 0 <= rev <= doc.rev:
            raise RequestError("bad_rev", f"'rev' must be between 0 and {doc.rev}")
        self.send({"type": "snapshot", "doc": name, "rev": rev, "text": doc.text_at(rev)})


class Server:
    def __init__(self, data_dir):
        self.store = Store(data_dir)

    async def serve(self, host, port):
        loop = asyncio.get_running_loop()
        srv = await loop.create_server(lambda: Connection(self), host, port, reuse_address=True)
        async with srv:
            await srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Livedoc collaborative editing server")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", required=True)
    args = ap.parse_args(argv)
    server = Server(args.data_dir)
    try:
        asyncio.run(server.serve("127.0.0.1", args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
