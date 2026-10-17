#!/usr/bin/env python3
"""Livedoc server: real-time collaborative plain-text editing.

Usage: python3 server.py --port PORT --data-dir DIR
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys

from livedoc_client import apply_op, transform, transform_cursor

MAX_DOC_NAME = 100
MAX_LINE = 16 * 1024 * 1024        # longest accepted request line, in bytes
MAX_OUTBUF = 64 * 1024 * 1024      # drop a connection that stops reading past this
CHECKPOINT_EVERY = 100             # keep the full text of every Nth revision in memory

_MISSING = object()


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_doc_name(v):
    return isinstance(v, str) and 0 < len(v) <= MAX_DOC_NAME


def parse_op(raw):
    """Check the shape of an operation and return a normalized copy (bad_request on failure)."""
    if not isinstance(raw, list):
        raise ProtocolError("bad_request", "'op' must be a list of edits")
    op = []
    for edit in raw:
        if isinstance(edit, dict) and edit.keys() == {"ins", "text"}:
            if not _is_int(edit["ins"]) or not isinstance(edit["text"], str):
                raise ProtocolError("bad_request", f"malformed insert: {edit!r}")
            op.append({"ins": edit["ins"], "text": edit["text"]})
        elif isinstance(edit, dict) and edit.keys() == {"del", "len"}:
            if not _is_int(edit["del"]) or not _is_int(edit["len"]):
                raise ProtocolError("bad_request", f"malformed delete: {edit!r}")
            op.append({"del": edit["del"], "len": edit["len"]})
        else:
            raise ProtocolError("bad_request", f"malformed edit: {edit!r}")
    return op


def check_range(op, length):
    """Raise out_of_range unless `op` is valid against a text of `length` code points."""
    for edit in op:
        if "ins" in edit:
            if not edit["text"]:
                raise ProtocolError("out_of_range", "insert text must not be empty")
            if not 0 <= edit["ins"] <= length:
                raise ProtocolError("out_of_range", f"insert at {edit['ins']} outside text of length {length}")
            length += len(edit["text"])
        else:
            p, n = edit["del"], edit["len"]
            if n < 1:
                raise ProtocolError("out_of_range", "delete length must be at least 1")
            if p < 0 or p + n > length:
                raise ProtocolError("out_of_range", f"delete [{p}, {p + n}) outside text of length {length}")
            length -= n


class Document:
    """One document: its text, full history and append-only log file.

    The log is JSON lines: a header `{"doc": NAME}` followed by one record per
    committed operation, `{"rev", "client", "id", "op"}`. The header is written
    when the document is created (first join), so an empty document survives a
    restart. A record is written to the file before the operation is acknowledged.

    Presence (members and cursors) lives only in memory.
    """

    def __init__(self, name, data_dir):
        self.name = name
        self.path = self.path_for(name, data_dir)
        self.text = ""
        self.lengths = [0]      # lengths[r] = len(text at revision r)
        self.history = []       # history[r - 1] = record committed as revision r
        self.checkpoints = {0: ""}  # r -> text at revision r, for r % CHECKPOINT_EVERY == 0
        self.seen = {}          # (client, id) -> revision
        self.subscribers = {}   # Connection -> None (insertion-ordered set)
        self.members = {}       # client name -> set of Connections joined under it
        self.cursors = {}       # client name -> cursor position at the current revision
        self._has_header = False
        self._load()
        if not self._has_header:
            with open(self.path, "ab") as f:
                f.write((json.dumps({"doc": self.name}) + "\n").encode("utf-8"))
            self._has_header = True

    @staticmethod
    def path_for(name, data_dir):
        digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()
        return os.path.join(data_dir, digest + ".log")

    @property
    def rev(self):
        return len(self.history)

    def _load(self):
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return
        good_end = 0
        pos = 0
        while True:
            nl = data.find(b"\n", pos)
            if nl < 0:
                break
            try:
                rec = json.loads(data[pos:nl].decode("utf-8"))
                if not self._has_header:
                    if rec.get("doc") != self.name:
                        raise ValueError("header does not match document name")
                    self._has_header = True
                else:
                    if rec["rev"] != self.rev + 1:
                        raise ValueError("revision gap in log")
                    self._apply_record(rec)
            except (ValueError, KeyError, TypeError, AttributeError):
                break
            pos = good_end = nl + 1
        if good_end < len(data):
            # A torn final write (the process was killed mid-write) or garbage:
            # drop it so future appends start on a clean line. It was never acked.
            os.truncate(self.path, good_end)

    def _apply_record(self, rec):
        self.text = apply_op(self.text, rec["op"])
        self.lengths.append(len(self.text))
        self.history.append(rec)
        self.seen[(rec["client"], rec["id"])] = rec["rev"]
        if rec["rev"] % CHECKPOINT_EVERY == 0:
            self.checkpoints[rec["rev"]] = self.text

    def commit(self, client, op_id, op):
        rec = {"rev": self.rev + 1, "client": client, "id": op_id, "op": op}
        apply_op(self.text, op)  # validate before anything is written
        with open(self.path, "ab") as f:
            f.write((json.dumps(rec) + "\n").encode("utf-8"))
        self._apply_record(rec)
        for name, pos in self.cursors.items():
            self.cursors[name] = transform_cursor(pos, op, name == client)
        return rec

    def text_at(self, rev):
        """Text at revision `rev`: nearest checkpoint plus fewer than CHECKPOINT_EVERY ops."""
        if rev == self.rev:
            return self.text
        base = rev - rev % CHECKPOINT_EVERY
        text = self.checkpoints[base]
        for rec in self.history[base:rev]:
            text = apply_op(text, rec["op"])
        return text


class Connection:
    def __init__(self, writer):
        self.writer = writer
        self.joined = {}  # doc name -> client name used when joining
        self.closed = False

    def send(self, msg):
        if self.closed:
            return
        transport = self.writer.transport
        if transport.is_closing():
            self.closed = True
            return
        self.writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        if transport.get_write_buffer_size() > MAX_OUTBUF:
            # This client is not reading; cut it loose rather than buffer forever.
            self.closed = True
            transport.abort()


class Server:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.docs = {}

    def get_doc(self, name, create=True):
        """Return the document, loading it from disk if needed.

        With create=False, return None for a document nobody has ever joined.
        """
        doc = self.docs.get(name)
        if doc is None:
            if not create and not os.path.exists(Document.path_for(name, self.data_dir)):
                return None
            doc = self.docs[name] = Document(name, self.data_dir)
        return doc

    # All request handling is synchronous and runs on the event loop thread, so
    # each request is processed atomically: commits on a document are serialized
    # and every enqueue to a connection happens in commit order.

    def handle_line(self, conn, line):
        msg = None
        try:
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                raise ProtocolError("bad_json", "line is not valid JSON")
            if not isinstance(msg, dict):
                msg = None
                raise ProtocolError("bad_json", "message must be a JSON object")
            kind = msg.get("type")
            if kind == "join":
                self.handle_join(conn, msg)
            elif kind == "op":
                self.handle_op(conn, msg)
            elif kind == "cursor":
                self.handle_cursor(conn, msg)
            elif kind == "cursors":
                self.handle_cursors(conn, msg)
            elif kind == "snapshot":
                self.handle_snapshot(conn, msg)
            else:
                raise ProtocolError("bad_request", f"unknown message type {kind!r}")
        except ProtocolError as e:
            self.send_error(conn, msg, e.code, e.message)
        except Exception as e:  # never let one request take the server down
            print(f"internal error: {e!r}", file=sys.stderr, flush=True)
            self.send_error(conn, msg, "internal", "internal server error")

    @staticmethod
    def send_error(conn, msg, code, message):
        err = {"type": "error", "code": code, "message": message}
        req_id = msg.get("id", _MISSING) if isinstance(msg, dict) else _MISSING
        if req_id is not _MISSING:
            err["id"] = req_id
        conn.send(err)

    def handle_join(self, conn, msg):
        name, client = msg.get("doc"), msg.get("client")
        since = msg.get("since")
        if not _valid_doc_name(name):
            raise ProtocolError("bad_request", "'doc' must be a string of 1 to 100 characters")
        if not isinstance(client, str) or not client:
            raise ProtocolError("bad_request", "'client' must be a non-empty string")
        if since is not None and not _is_int(since):
            raise ProtocolError("bad_request", "'since' must be an integer")
        doc = self.get_doc(name)
        if since is not None and not 0 <= since <= doc.rev:
            raise ProtocolError("bad_rev", f"'since' must be between 0 and {doc.rev}")
        previous = conn.joined.get(name)
        if previous is not None and previous != client:
            self.remove_member(doc, previous, conn)  # rejoined under another name
        conn.joined[name] = client
        doc.subscribers[conn] = None
        doc.members.setdefault(client, set()).add(conn)
        reply = {"type": "joined", "doc": name, "rev": doc.rev}
        if since is None:
            reply["text"] = doc.text
        else:
            reply["ops"] = doc.history[since:]
        conn.send(reply)

    def handle_op(self, conn, msg):
        name, rev, op_id = msg.get("doc"), msg.get("rev"), msg.get("id")
        if not _valid_doc_name(name):
            raise ProtocolError("bad_request", "'doc' must be a string of 1 to 100 characters")
        if not _is_int(rev):
            raise ProtocolError("bad_request", "'rev' must be an integer")
        if not isinstance(op_id, str):
            raise ProtocolError("bad_request", "'id' must be a string")
        if "op" not in msg:
            raise ProtocolError("bad_request", "missing 'op'")
        op = parse_op(msg["op"])
        if name not in conn.joined:
            raise ProtocolError("not_joined", f"not joined to document {name!r}")
        client = conn.joined[name]
        doc = self.docs[name]

        committed = doc.seen.get((client, op_id))
        if committed is not None:  # exactly once: re-acknowledge, don't re-apply
            conn.send({"type": "ack", "doc": name, "id": op_id, "rev": committed})
            return
        if not 0 <= rev <= doc.rev:
            raise ProtocolError("bad_rev", f"'rev' must be between 0 and {doc.rev}")
        check_range(op, doc.lengths[rev])

        for rec in doc.history[rev:]:
            op, _ = transform(op, rec["op"])
        rec = doc.commit(client, op_id, op)

        broadcast = {"type": "op", "doc": name, "rev": rec["rev"], "client": client,
                     "id": op_id, "op": op}
        for other in doc.subscribers:
            if other is not conn:
                other.send(broadcast)
        conn.send({"type": "ack", "doc": name, "id": op_id, "rev": rec["rev"]})

    def joined_doc(self, conn, msg):
        """Validate `doc` and return (Document, client name) for a joined document."""
        name = msg.get("doc")
        if not _valid_doc_name(name):
            raise ProtocolError("bad_request", "'doc' must be a string of 1 to 100 characters")
        if name not in conn.joined:
            raise ProtocolError("not_joined", f"not joined to document {name!r}")
        return self.docs[name], conn.joined[name]

    def handle_cursor(self, conn, msg):
        rev, pos = msg.get("rev"), msg.get("pos")
        if not _is_int(rev):
            raise ProtocolError("bad_request", "'rev' must be an integer")
        if not _is_int(pos):
            raise ProtocolError("bad_request", "'pos' must be an integer")
        doc, client = self.joined_doc(conn, msg)
        if not 0 <= rev <= doc.rev:
            raise ProtocolError("bad_rev", f"'rev' must be between 0 and {doc.rev}")
        if not 0 <= pos <= doc.lengths[rev]:
            raise ProtocolError("out_of_range",
                                f"cursor {pos} outside text of length {doc.lengths[rev]}")
        for rec in doc.history[rev:]:
            pos = transform_cursor(pos, rec["op"], rec["client"] == client)
        doc.cursors[client] = pos
        broadcast = {"type": "cursor", "doc": doc.name, "client": client,
                     "rev": doc.rev, "pos": pos}
        for other in doc.subscribers:
            if other is not conn:
                other.send(broadcast)

    def handle_cursors(self, conn, msg):
        doc, _ = self.joined_doc(conn, msg)
        conn.send({"type": "cursors", "doc": doc.name, "rev": doc.rev,
                   "cursors": dict(doc.cursors)})

    def handle_snapshot(self, conn, msg):
        name, rev = msg.get("doc"), msg.get("rev")
        if not _valid_doc_name(name):
            raise ProtocolError("bad_request", "'doc' must be a string of 1 to 100 characters")
        if not _is_int(rev):
            raise ProtocolError("bad_request", "'rev' must be an integer")
        doc = self.get_doc(name, create=False)
        if doc is None:
            raise ProtocolError("not_found", f"no document named {name!r}")
        if not 0 <= rev <= doc.rev:
            raise ProtocolError("bad_rev", f"'rev' must be between 0 and {doc.rev}")
        conn.send({"type": "snapshot", "doc": name, "rev": rev, "text": doc.text_at(rev)})

    def remove_member(self, doc, client, conn):
        """`conn` no longer represents `client` in `doc`; announce if it was the last."""
        conns = doc.members.get(client)
        if conns is None:
            return
        conns.discard(conn)
        if conns:
            return
        del doc.members[client]
        doc.cursors.pop(client, None)
        leave = {"type": "leave", "doc": doc.name, "client": client}
        for other in doc.subscribers:
            if other is not conn:
                other.send(leave)

    def disconnect(self, conn):
        conn.closed = True
        for name, client in conn.joined.items():
            doc = self.docs.get(name)
            if doc is not None:
                doc.subscribers.pop(conn, None)
                self.remove_member(doc, client, conn)
        conn.joined.clear()

    async def handle_connection(self, reader, writer):
        conn = Connection(writer)
        buf = bytearray()
        scan_from = 0
        discarding = False  # inside an over-long line; skip to its end
        try:
            while not conn.closed:
                data = await reader.read(65536)
                if not data:
                    break  # EOF; an unterminated partial line is dropped
                buf += data
                start = 0
                while True:
                    nl = buf.find(b"\n", max(start, scan_from))
                    if nl < 0:
                        break
                    if discarding:
                        discarding = False
                    else:
                        self.handle_line(conn, bytes(buf[start:nl]))
                    start = nl + 1
                    scan_from = 0
                if start:
                    del buf[:start]
                scan_from = len(buf)
                if len(buf) > MAX_LINE:
                    if not discarding:
                        self.send_error(conn, None, "bad_request", "line too long")
                    discarding = True
                    buf.clear()
                    scan_from = 0
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.disconnect(conn)
            writer.close()


async def main_async(port, data_dir):
    server = Server(data_dir)
    srv = await asyncio.start_server(server.handle_connection, "127.0.0.1", port)
    print(f"livedoc listening on 127.0.0.1:{port}, data in {data_dir}", flush=True)
    async with srv:
        await srv.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Livedoc collaborative editing server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.port, args.data_dir))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
