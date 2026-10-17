"""Livedoc server: real-time collaborative plain-text editing over line-delimited JSON on TCP.

Usage: python3 server.py --port PORT --data-dir DIR
"""

import argparse
import asyncio
import json
import os
import sys

from livedoc_client import OpError, apply_op, check_op, validate_op, xf

MAX_LINE = 64 * 1024 * 1024          # longest request line accepted, in bytes
MAX_OUTBOX = 256 * 1024 * 1024       # a connection that lets this much output pile up is dropped
MAX_DOC_NAME = 100
LOG_NAME = "oplog.jsonl"


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
        self.subscribers = set()

    @property
    def rev(self):
        return len(self.history)

    def commit(self, client, op_id, op):
        self.text = apply_op(self.text, op)
        self.history.append((client, op_id, op))
        self.lengths.append(len(self.text))
        self.committed[(client, op_id)] = self.rev
        return self.rev


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
                    doc = self.get(rec["doc"])
                    if rec["rev"] != doc.rev + 1:
                        raise ValueError("revision gap in log")
                    doc.commit(rec["client"], rec["id"], rec["op"])
                except (ValueError, KeyError, TypeError):
                    break
                good += len(line)
        if good != os.path.getsize(self.path):
            with open(self.path, "r+b") as f:
                f.truncate(good)

    def get(self, name):
        doc = self.docs.get(name)
        if doc is None:
            doc = self.docs[name] = Document(name)
        return doc

    def commit(self, doc, client, op_id, op):
        rec = {"doc": doc.name, "rev": doc.rev + 1, "client": client, "id": op_id, "op": op}
        # One unbuffered write per record; once it returns, the record is in the kernel and
        # survives the process being killed.
        data = memoryview((_dumps(rec) + "\n").encode("utf-8"))
        while data:
            data = data[self.log.write(data):]
        return doc.commit(client, op_id, op)


class Connection:
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.names = {}          # doc name -> client name this connection joined it as
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
            for name in self.names:
                self.server.store.docs[name].subscribers.discard(self)
            self.writer.close()

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
        # Only create the document once the request is known to be well-formed.
        doc = self.server.store.get(name)
        if since is not None and not 0 <= since <= doc.rev:
            raise RequestError("bad_rev", "since=%d but document is at revision %d" % (since, doc.rev))
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

        bcast = {"type": "op", "doc": name, "rev": new_rev, "client": client, "id": op_id, "op": op}
        for conn in list(doc.subscribers):
            if conn is not self:
                conn.send(bcast)
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
