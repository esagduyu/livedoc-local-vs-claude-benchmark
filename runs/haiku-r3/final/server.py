#!/usr/bin/env python3
"""Livedoc server: real-time collaborative plain-text editing over TCP.

Usage: python3 server.py --port PORT --data-dir DIR

Every committed operation is appended to DIR/oplog.jsonl before it is
acknowledged. At start-up the log is replayed to rebuild all documents.
"""

import argparse
import asyncio
import fcntl
import json
import os
import sys
import traceback

from livedoc_ot import OpError, apply, edits_from_wire, edits_to_wire, move_cursor, validate, xf

LOG_NAME = "oplog.jsonl"
MAX_DOC_NAME = 100
MAX_LINE = 8 * 1024 * 1024
CHECKPOINT_EVERY = 128  # the text is kept every this many revisions, for snapshots


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value):
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _parse_doc(value):
    if not isinstance(value, str) or not 0 < len(value) <= MAX_DOC_NAME:
        raise ProtocolError("bad_request", "doc must be a non-empty string of at most 100 characters")
    return value


def _parse_op(value):
    if not isinstance(value, list):
        raise ProtocolError("bad_request", "op must be a list of edits")
    for e in value:
        if not isinstance(e, dict):
            raise ProtocolError("bad_request", "each edit must be an object")
        if "ins" in e:
            ok = _is_int(e["ins"]) and _is_text(e.get("text"))
        elif "del" in e:
            ok = _is_int(e["del"]) and _is_int(e.get("len"))
        else:
            ok = False
        if not ok:
            raise ProtocolError("bad_request", "malformed edit")
    return edits_from_wire(value)


class Doc:
    def __init__(self, name):
        self.name = name
        self.text = ""
        self.lengths = [0]  # lengths[r] is the length of the text at revision r
        self.checkpoints = [""]  # checkpoints[i] is the text at revision i * CHECKPOINT_EVERY
        self.history = []  # history[r - 1] is (client, id, edits) for revision r
        self.committed = {}  # (client, id) -> revision that carried it
        self.subscribers = set()
        self.cursors = {}  # client name -> cursor position at the current revision

    @property
    def rev(self):
        return len(self.history)

    def commit(self, client, op_id, edits):
        self.text = apply(self.text, edits)
        self.lengths.append(len(self.text))
        self.history.append((client, op_id, edits))
        self.committed[(client, op_id)] = self.rev
        if self.rev % CHECKPOINT_EVERY == 0:
            self.checkpoints.append(self.text)
        for name, pos in list(self.cursors.items()):
            self.cursors[name] = move_cursor(pos, edits, name == client)

    def text_at(self, rev):
        """The text at ``rev``: the nearest checkpoint, then the operations after it."""
        base = rev - rev % CHECKPOINT_EVERY
        text = self.checkpoints[base // CHECKPOINT_EVERY]
        for _, _, edits in self.history[base:rev]:
            text = apply(text, edits)
        return text


class Connection:
    def __init__(self, writer):
        self.writer = writer
        self.names = {}  # doc name -> client name used when joining it


def load_docs(data_dir):
    """Open the operation log under ``data_dir`` and replay it.

    Returns ``(docs, fd)``. ``fd`` is the log opened for appending.
    """
    path = os.path.join(data_dir, LOG_NAME)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(f"data dir {data_dir} is in use by another server")
    with open(path, "rb") as f:
        data = f.read()
    end = data.rfind(b"\n") + 1
    if end != len(data):
        # A record cut short by a crash. It was never acknowledged, so drop it.
        os.ftruncate(fd, end)
        data = data[:end]
    docs = {}
    for line in data.split(b"\n")[:-1]:
        rec = json.loads(line)
        doc = docs.get(rec["doc"])
        if doc is None:
            doc = docs[rec["doc"]] = Doc(rec["doc"])
        if rec.get("created"):
            continue
        if rec["rev"] != doc.rev + 1:
            raise SystemExit(f"corrupt log: {rec['doc']!r} has revision {rec['rev']} out of sequence")
        doc.commit(rec["client"], rec["id"], edits_from_wire(rec["op"]))
    return docs, fd


def _append(fd, record):
    data = (json.dumps(record) + "\n").encode()
    while data:
        data = data[os.write(fd, data):]


class Server:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.docs, self.log_fd = load_docs(data_dir)

    async def serve(self, port):
        server = await asyncio.start_server(self._on_connect, "127.0.0.1", port)
        print(f"livedoc listening on 127.0.0.1:{port}", file=sys.stderr, flush=True)
        async with server:
            await server.serve_forever()

    async def _on_connect(self, reader, writer):
        conn = Connection(writer)
        try:
            await self._read_loop(conn, reader)
        except OSError:
            pass
        finally:
            self._detach(conn)
            writer.close()

    async def _read_loop(self, conn, reader):
        buf = bytearray()
        discarding = False
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                if discarding:
                    discarding = False
                else:
                    self._handle_line(conn, line)
            if len(buf) > MAX_LINE:
                buf.clear()
                if not discarding:
                    self._send(conn, {"type": "error", "code": "bad_json", "message": "line too long"})
                discarding = True
            await conn.writer.drain()

    @staticmethod
    def _send(conn, obj):
        if not conn.writer.is_closing():
            conn.writer.write((json.dumps(obj) + "\n").encode())

    def _handle_line(self, conn, line):
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            msg = None
        if not isinstance(msg, dict):
            self._send(conn, {"type": "error", "code": "bad_json", "message": "line is not a JSON object"})
            return
        try:
            self._dispatch(conn, msg)
        except ProtocolError as e:
            reply = {"type": "error", "code": e.code, "message": e.message}
            if isinstance(msg.get("id"), str):
                reply["id"] = msg["id"]
            self._send(conn, reply)
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _dispatch(self, conn, msg):
        kind = msg.get("type")
        if kind == "join":
            self._join(conn, msg)
        elif kind == "op":
            self._op(conn, msg)
        elif kind == "cursor":
            self._cursor(conn, msg)
        elif kind == "cursors":
            self._cursors(conn, msg)
        elif kind == "snapshot":
            self._snapshot(conn, msg)
        else:
            raise ProtocolError("bad_request", "unknown message type")

    def _join(self, conn, msg):
        name = _parse_doc(msg.get("doc"))
        client = msg.get("client")
        if not isinstance(client, str) or not client:
            raise ProtocolError("bad_request", "client must be a non-empty string")
        since = msg.get("since")
        if "since" in msg and not _is_int(since):
            raise ProtocolError("bad_request", "since must be an integer")
        doc = self.docs.get(name)
        if doc is None:
            doc = self.docs[name] = Doc(name)
            _append(self.log_fd, {"doc": name, "created": True})
        if since is None:
            reply = {"type": "joined", "doc": name, "rev": doc.rev, "text": doc.text}
        else:
            if not 0 <= since <= doc.rev:
                raise ProtocolError("bad_rev", "since is negative or after the current revision")
            ops = []
            for r in range(since, doc.rev):
                author, op_id, edits = doc.history[r]
                ops.append({"rev": r + 1, "client": author, "id": op_id, "op": edits_to_wire(edits)})
            reply = {"type": "joined", "doc": name, "rev": doc.rev, "ops": ops}
        old = self._attach(conn, doc, client)
        self._send(conn, reply)
        if old is not None:
            self._release(doc, old)

    @staticmethod
    def _attach(conn, doc, client):
        """Subscribe ``conn`` to ``doc`` as ``client``. Returns the name it used before, if different."""
        old = conn.names.get(doc.name)
        conn.names[doc.name] = client
        doc.subscribers.add(conn)
        return old if old not in (None, client) else None

    def _detach(self, conn):
        for doc_name, name in list(conn.names.items()):
            doc = self.docs[doc_name]
            doc.subscribers.discard(conn)
            self._release(doc, name)
        conn.names.clear()

    def _release(self, doc, name):
        """Called when a connection stops using ``name`` on ``doc``.

        If no connection still uses the name, its cursor is dropped and the
        others are told it has left.
        """
        if any(other.names.get(doc.name) == name for other in doc.subscribers):
            return
        doc.cursors.pop(name, None)
        for other in doc.subscribers:
            self._send(other, {"type": "leave", "doc": doc.name, "client": name})

    def _op(self, conn, msg):
        name = _parse_doc(msg.get("doc"))
        rev = msg.get("rev")
        if not _is_int(rev):
            raise ProtocolError("bad_request", "rev must be an integer")
        op_id = msg.get("id")
        if not isinstance(op_id, str):
            raise ProtocolError("bad_request", "id must be a string")
        edits = _parse_op(msg.get("op"))
        if name not in conn.names:
            raise ProtocolError("not_joined", "this connection has not joined the document")

        doc = self.docs[name]
        client = conn.names[name]
        original = doc.committed.get((client, op_id))
        if original is not None:
            self._send(conn, {"type": "ack", "doc": name, "id": op_id, "rev": original})
            return
        if not 0 <= rev <= doc.rev:
            raise ProtocolError("bad_rev", "rev is negative or after the current revision")
        try:
            validate(edits, doc.lengths[rev])
        except OpError as e:
            raise ProtocolError("out_of_range", str(e))

        for _, _, committed in doc.history[rev:]:
            edits, _ = xf(edits, committed)

        new_rev = doc.rev + 1
        wire = edits_to_wire(edits)
        _append(self.log_fd, {"doc": name, "rev": new_rev, "client": client, "id": op_id, "op": wire})
        doc.commit(client, op_id, edits)

        broadcast = {"type": "op", "doc": name, "rev": new_rev, "client": client, "id": op_id, "op": wire}
        for other in doc.subscribers:
            if other is not conn:
                self._send(other, broadcast)
        self._send(conn, {"type": "ack", "doc": name, "id": op_id, "rev": new_rev})

    def _cursor(self, conn, msg):
        name = _parse_doc(msg.get("doc"))
        rev = msg.get("rev")
        if not _is_int(rev):
            raise ProtocolError("bad_request", "rev must be an integer")
        pos = msg.get("pos")
        if not _is_int(pos):
            raise ProtocolError("bad_request", "pos must be an integer")
        if name not in conn.names:
            raise ProtocolError("not_joined", "this connection has not joined the document")

        doc = self.docs[name]
        client = conn.names[name]
        if not 0 <= rev <= doc.rev:
            raise ProtocolError("bad_rev", "rev is negative or after the current revision")
        if not 0 <= pos <= doc.lengths[rev]:
            raise ProtocolError("out_of_range", "cursor position is outside the text")

        for author, _, edits in doc.history[rev:]:
            pos = move_cursor(pos, edits, author == client)
        doc.cursors[client] = pos
        message = {"type": "cursor", "doc": name, "client": client, "rev": doc.rev, "pos": pos}
        for other in doc.subscribers:
            if other is not conn:
                self._send(other, message)

    def _cursors(self, conn, msg):
        name = _parse_doc(msg.get("doc"))
        if name not in conn.names:
            raise ProtocolError("not_joined", "this connection has not joined the document")
        doc = self.docs[name]
        self._send(conn, {"type": "cursors", "doc": name, "rev": doc.rev, "cursors": dict(doc.cursors)})

    def _snapshot(self, conn, msg):
        name = _parse_doc(msg.get("doc"))
        rev = msg.get("rev")
        if not _is_int(rev):
            raise ProtocolError("bad_request", "rev must be an integer")
        doc = self.docs.get(name)
        if doc is None:
            raise ProtocolError("not_found", "no such document")
        if not 0 <= rev <= doc.rev:
            raise ProtocolError("bad_rev", "rev is negative or after the current revision")
        self._send(conn, {"type": "snapshot", "doc": name, "rev": rev, "text": doc.text_at(rev)})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Livedoc collaborative editing server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)
    server = Server(args.data_dir)
    try:
        asyncio.run(server.serve(args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
