"""Livedoc server: real-time collaborative plain-text editing over line-delimited JSON on TCP.

Usage: python3 server.py --port PORT --data-dir DIR
"""

import argparse
import asyncio
import json
import os
import sys

from livedoc_client import OpError, apply_op, check_op, move_cursor, validate_op, xf

MAX_LINE = 64 * 1024 * 1024          # longest request line accepted, in bytes
MAX_OUTBOX = 256 * 1024 * 1024       # a connection that lets this much output pile up is dropped
MAX_DOC_NAME = 100
LOG_NAME = "oplog.jsonl"
CHECKPOINT_EVERY = 64               # keep the full text of every 64th revision for snapshots


def _dumps(obj):
    # ensure_ascii keeps lone surrogates (legal in JSON strings) encodable as UTF-8.
    return json.dumps(obj, separators=(",", ":"))


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
        self.history = []        # history[r - 1] = (client, id, op) committed as revision r
        self.lengths = [0]       # lengths[r] = len(text at revision r)
        self.committed = {}      # (client, id) -> revision
        self.checkpoints = [""]  # checkpoints[i] = text at revision i * CHECKPOINT_EVERY
        self.subscribers = set()
        self.members = {}        # client name -> number of open connections joined under it
        self.cursors = {}        # client name -> cursor position at the current revision

    @property
    def rev(self):
        return len(self.history)

    def commit(self, client, op_id, op):
        self.text = apply_op(self.text, op)
        self.history.append((client, op_id, op))
        self.lengths.append(len(self.text))
        self.committed[(client, op_id)] = self.rev
        if self.rev % CHECKPOINT_EVERY == 0:
            self.checkpoints.append(self.text)   # strings are immutable: no copy is made
        for name, c in self.cursors.items():
            self.cursors[name] = move_cursor(c, op, name == client)
        return self.rev

    def text_at(self, rev):
        """Text at revision `rev`: the nearest checkpoint, plus at most CHECKPOINT_EVERY - 1 ops."""
        if rev == self.rev:
            return self.text
        base = rev // CHECKPOINT_EVERY
        text = self.checkpoints[base]
        for _, _, op in self.history[base * CHECKPOINT_EVERY:rev]:
            text = apply_op(text, op)
        return text

    def send_others(self, msg, sender):
        for conn in list(self.subscribers):
            if conn is not sender:
                conn.send(msg)


class Store:
    """All documents, backed by one append-only log of committed operations."""

    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, LOG_NAME)
        self.docs = {}
        self._load()
        self.log = open(self.path, "ab", buffering=0)

    def _load(self):
        if not os.path.exists(self.path):
            return
        good = 0
        with open(self.path, "rb") as f:
            for line in f:
                if not line.endswith(b"\n"):
                    break  # torn final write: never acknowledged
                try:
                    rec = json.loads(line)
                    doc = self._doc(rec["doc"])
                    if rec["rev"] == 0 and "op" not in rec:
                        good += len(line)
                        continue     # creation record
                    if rec["rev"] != doc.rev + 1:
                        raise ValueError("revision gap in log")
                    doc.commit(rec["client"], rec["id"], rec["op"])
                except (ValueError, KeyError, TypeError):
                    break
                good += len(line)
        if good != os.path.getsize(self.path):
            with open(self.path, "r+b") as f:
                f.truncate(good)

    def _doc(self, name):
        doc = self.docs.get(name)
        if doc is None:
            doc = self.docs[name] = Document(name)
        return doc

    def get(self, name):
        """Return the named document, creating (and recording) it if it does not exist yet."""
        doc = self.docs.get(name)
        if doc is None:
            # Record the creation so the empty document still exists (for snapshot) after a restart.
            self._append({"doc": name, "rev": 0})
            doc = self.docs[name] = Document(name)
        return doc

    def commit(self, doc, client, op_id, op):
        self._append({"doc": doc.name, "rev": doc.rev + 1, "client": client, "id": op_id, "op": op})
        return doc.commit(client, op_id, op)

    def _append(self, rec):
        # One unbuffered write per record; once it returns, the record is in the kernel and
        # survives the process being killed.
        data = memoryview((_dumps(rec) + "\n").encode("utf-8"))
        while data:
            data = data[self.log.write(data):]


class Connection:
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.names = {}          # doc name -> client name this connection joined it as (latest)
        self.member_of = {}      # doc name -> every client name this connection joined it as
        self.closed = False

    def send(self, msg):
        if self.closed:
            return
        self.writer.write((_dumps(msg) + "\n").encode("utf-8"))
        if self.writer.transport.get_write_buffer_size() > MAX_OUTBOX:
            self.abort()

    def abort(self):
        if not self.closed:
            self.closed = True
            self.writer.transport.abort()

    async def run(self):
        buf = bytearray()
        skipping = False   # discarding the rest of an over-long line
        try:
            while not self.closed:
                chunk = await self.reader.read(1 << 16)
                if not chunk:
                    break
                start = 0
                while True:
                    i = chunk.find(b"\n", start)
                    if i < 0:
                        break
                    if skipping:
                        skipping = False
                    else:
                        buf += chunk[start:i]
                        self.handle_line(bytes(buf))
                    buf.clear()
                    start = i + 1
                if not skipping:
                    buf += chunk[start:]
                    if len(buf) > MAX_LINE:
                        buf.clear()
                        skipping = True
                        self.send({"type": "error", "code": "bad_request", "message": "line too long"})
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            self.leave_all()
            self.writer.close()

    def leave_all(self):
        docs = self.server.store.docs
        for name in self.names:
            docs[name].subscribers.discard(self)
        for name, clients in self.member_of.items():
            doc = docs[name]
            for client in clients:
                doc.members[client] -= 1
                if doc.members[client] == 0:
                    del doc.members[client]
                    doc.cursors.pop(client, None)
                    doc.send_others({"type": "leave", "doc": name, "client": client}, self)

    def handle_line(self, line):
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
                raise RequestError("bad_request", "unknown message type %r" % (kind,))
        except (RequestError, OpError) as e:
            err = {"type": "error", "code": e.code, "message": str(e)}
            if "id" in msg:
                err["id"] = msg["id"]
            self.send(err)

    def _doc_name(self, msg):
        name = msg.get("doc")
        if not isinstance(name, str) or not 1 <= len(name) <= MAX_DOC_NAME:
            raise RequestError("bad_request", "'doc' must be a string of 1 to %d characters" % MAX_DOC_NAME)
        return name

    def handle_join(self, msg):
        name = self._doc_name(msg)
        client = msg.get("client")
        if not isinstance(client, str) or not client:
            raise RequestError("bad_request", "'client' must be a non-empty string")
        since = msg.get("since")
        if "since" in msg and not _is_int(since):
            raise RequestError("bad_request", "'since' must be an integer")
        existing = self.server.store.docs.get(name)
        current = existing.rev if existing else 0
        if since is not None and not 0 <= since <= current:
            raise RequestError("bad_rev", "since=%d but document is at revision %d" % (since, current))
        # Only create the document once the request is known to be valid.
        doc = self.server.store.get(name)
        reply = {"type": "joined", "doc": name, "rev": doc.rev}
        if since is None:
            reply["text"] = doc.text
        else:
            reply["ops"] = [
                {"rev": r, "client": c, "id": i, "op": op}
                for r, (c, i, op) in enumerate(doc.history[since:], since + 1)
            ]
        self.send(reply)
        self.names[name] = client
        doc.subscribers.add(self)
        clients = self.member_of.setdefault(name, set())
        if client not in clients:
            clients.add(client)
            doc.members[client] = doc.members.get(client, 0) + 1

    def _joined_doc(self, msg):
        name = self._doc_name(msg)
        if name not in self.names:
            raise RequestError("not_joined", "join %r first" % name)
        return self.server.store.docs[name]

    def _rev(self, msg, doc):
        rev = msg.get("rev")
        if not _is_int(rev):
            raise RequestError("bad_request", "'rev' must be an integer")
        if not 0 <= rev <= doc.rev:
            raise RequestError("bad_rev", "rev=%d but document is at revision %d" % (rev, doc.rev))
        return rev

    def handle_cursor(self, msg):
        doc = self._joined_doc(msg)
        pos = msg.get("pos")
        if not _is_int(msg.get("rev")) or not _is_int(pos):
            raise RequestError("bad_request", "'rev' and 'pos' must be integers")
        rev = self._rev(msg, doc)
        if not 0 <= pos <= doc.lengths[rev]:
            raise RequestError("out_of_range", "cursor %d outside text of length %d" % (pos, doc.lengths[rev]))
        client = self.names[doc.name]
        for author, _, op in doc.history[rev:]:
            pos = move_cursor(pos, op, author == client)
        doc.cursors[client] = pos
        doc.send_others({"type": "cursor", "doc": doc.name, "client": client, "rev": doc.rev, "pos": pos}, self)

    def handle_cursors(self, msg):
        doc = self._joined_doc(msg)
        self.send({"type": "cursors", "doc": doc.name, "rev": doc.rev, "cursors": dict(doc.cursors)})

    def handle_snapshot(self, msg):
        name = self._doc_name(msg)
        if not _is_int(msg.get("rev")):
            raise RequestError("bad_request", "'rev' must be an integer")
        doc = self.server.store.docs.get(name)
        if doc is None:
            raise RequestError("not_found", "no document %r" % name)
        rev = self._rev(msg, doc)
        self.send({"type": "snapshot", "doc": name, "rev": rev, "text": doc.text_at(rev)})

    def handle_op(self, msg):
        name = self._doc_name(msg)
        if name not in self.names:
            raise RequestError("not_joined", "join %r before sending operations" % name)
        client = self.names[name]
        doc = self.server.store.docs[name]
        rev, op_id = msg.get("rev"), msg.get("id")
        if not _is_int(rev):
            raise RequestError("bad_request", "'rev' must be an integer")
        if not isinstance(op_id, str):
            raise RequestError("bad_request", "'id' must be a string")
        if "op" not in msg:
            raise RequestError("bad_request", "missing 'op'")
        op = validate_op(msg["op"])

        prior = doc.committed.get((client, op_id))
        if prior is not None:
            self.send({"type": "ack", "doc": name, "id": op_id, "rev": prior})
            return
        if not 0 <= rev <= doc.rev:
            raise RequestError("bad_rev", "rev=%d but document is at revision %d" % (rev, doc.rev))
        check_op(op, doc.lengths[rev])

        for _, _, earlier in doc.history[rev:]:
            op, _ = xf(op, earlier)
        new_rev = self.server.store.commit(doc, client, op_id, op)

        doc.send_others({"type": "op", "doc": name, "rev": new_rev, "client": client, "id": op_id, "op": op}, self)
        self.send({"type": "ack", "doc": name, "id": op_id, "rev": new_rev})


class Server:
    def __init__(self, data_dir):
        self.store = Store(data_dir)

    async def on_connect(self, reader, writer):
        await Connection(self, reader, writer).run()

    async def serve(self, host, port):
        srv = await asyncio.start_server(self.on_connect, host, port, limit=MAX_LINE)
        async with srv:
            await srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Livedoc collaborative editing server")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    server = Server(args.data_dir)
    try:
        asyncio.run(server.serve(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
