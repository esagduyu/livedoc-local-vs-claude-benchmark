#!/usr/bin/env python3
"""Livedoc server: many participants edit the same plain-text documents over TCP.

Usage: python3 server.py --port PORT --data-dir DIR

Every acknowledged operation is appended to DIR/journal.jsonl before the ack is
sent. On start-up the journal is replayed, which restores documents, revisions,
history and the exactly-once table. See README.md for the design.
"""

import argparse
import json
import os
import queue
import socketserver
import threading

from livedoc_client import apply_op, transform

MAX_DOC_NAME = 100
MAX_LINE = 16 * 1024 * 1024
SNAPSHOT_EVERY = 64
JOURNAL_NAME = "journal.jsonl"


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_name(msg, key):
    value = msg.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError("bad_request", f"'{key}' must be a non-empty string")
    if key == "doc" and len(value) > MAX_DOC_NAME:
        raise ProtocolError("bad_request", f"document name is longer than {MAX_DOC_NAME} characters")
    return value


def _check_op_shape(op):
    if not isinstance(op, list):
        raise ProtocolError("bad_request", "'op' must be a list of edits")
    for edit in op:
        if not isinstance(edit, dict):
            raise ProtocolError("bad_request", "each edit must be an object")
        keys = set(edit)
        if keys == {"ins", "text"}:
            ok = _is_int(edit["ins"]) and isinstance(edit["text"], str)
        elif keys == {"del", "len"}:
            ok = _is_int(edit["del"]) and _is_int(edit["len"])
        else:
            ok = False
        if not ok:
            raise ProtocolError("bad_request", "malformed edit")


class Document:
    """State of one document. Only Service touches it, under Service.lock."""

    def __init__(self, name):
        self.name = name
        self.records = []         # records[i] is (client, id, op) committed as revision i + 1
        self.committed = {}       # (client, id) -> revision, for exactly-once
        self.snapshots = {0: ""}  # text at every SNAPSHOT_EVERY-th revision
        self.text = ""
        self.subscribers = set()  # connections joined to this document

    @property
    def rev(self):
        return len(self.records)

    def text_at(self, rev):
        base = rev - rev % SNAPSHOT_EVERY
        text = self.snapshots[base]
        for _client, _id, op in self.records[base:rev]:
            text = apply_op(text, op)
        return text

    def append(self, client, op_id, op):
        self.text = apply_op(self.text, op)
        self.records.append((client, op_id, op))
        self.committed[(client, op_id)] = self.rev
        if self.rev % SNAPSHOT_EVERY == 0:
            self.snapshots[self.rev] = self.text
        return self.rev


class Service:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.docs = {}
        self.journal_path = os.path.join(data_dir, JOURNAL_NAME)
        self._replay()
        self.journal = open(self.journal_path, "ab")

    def _document(self, name):
        doc = self.docs.get(name)
        if doc is None:
            doc = self.docs[name] = Document(name)
        return doc

    def _replay(self):
        try:
            with open(self.journal_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return
        # A record is complete once its newline is written. Drop a torn tail:
        # that operation was never acknowledged.
        end = data.rfind(b"\n") + 1
        if end != len(data):
            with open(self.journal_path, "r+b") as f:
                f.truncate(end)
        for line in data[:end].split(b"\n"):
            if not line:
                continue
            record = json.loads(line)
            doc = self._document(record["doc"])
            doc.append(record["client"], record["id"], record["op"])
            if doc.rev != record["rev"]:
                raise RuntimeError(f"journal is inconsistent at {record['doc']!r} rev {record['rev']}")

    def _journal(self, record):
        self.journal.write(json.dumps(record).encode("utf-8") + b"\n")
        self.journal.flush()

    def join(self, conn, msg):
        name = _check_name(msg, "doc")
        client = _check_name(msg, "client")
        with self.lock:
            doc = self._document(name)
            if "since" in msg:
                since = msg["since"]
                if not _is_int(since):
                    raise ProtocolError("bad_request", "'since' must be an integer")
                if not 0 <= since <= doc.rev:
                    raise ProtocolError("bad_rev", f"'since' {since} is outside 0..{doc.rev}")
                ops = [
                    {"rev": rev, "client": c, "id": op_id, "op": op}
                    for rev, (c, op_id, op) in enumerate(doc.records[since:], start=since + 1)
                ]
                conn.send({"type": "joined", "doc": name, "rev": doc.rev, "ops": ops})
            else:
                conn.send({"type": "joined", "doc": name, "rev": doc.rev, "text": doc.text})
            conn.names[name] = client
            doc.subscribers.add(conn)

    def submit(self, conn, msg):
        name = _check_name(msg, "doc")
        base = msg.get("rev")
        op_id = msg.get("id")
        op = msg.get("op")
        if not _is_int(base):
            raise ProtocolError("bad_request", "'rev' must be an integer")
        if not isinstance(op_id, str):
            raise ProtocolError("bad_request", "'id' must be a string")
        _check_op_shape(op)

        with self.lock:
            client = conn.names.get(name)
            if client is None:
                raise ProtocolError("not_joined", f"not joined to {name!r}")
            doc = self.docs[name]
            if not 0 <= base <= doc.rev:
                raise ProtocolError("bad_rev", f"'rev' {base} is outside 0..{doc.rev}")
            key = (client, op_id)
            if key in doc.committed:
                conn.send({"type": "ack", "doc": name, "id": op_id, "rev": doc.committed[key]})
                return
            try:
                apply_op(doc.text_at(base), op)
            except ValueError as err:
                raise ProtocolError("out_of_range", str(err))

            for _client, _id, earlier in doc.records[base:]:
                op = transform(op, earlier)[0]
            rev = doc.rev + 1
            self._journal({"doc": name, "rev": rev, "client": client, "id": op_id, "op": op})
            doc.append(client, op_id, op)

            # Everything is queued under the lock, so each connection sees
            # broadcasts and acks in commit order.
            conn.send({"type": "ack", "doc": name, "id": op_id, "rev": rev})
            for other in doc.subscribers:
                if other is not conn:
                    other.send({"type": "op", "doc": name, "rev": rev,
                                "client": client, "id": op_id, "op": op})

    def leave(self, conn):
        with self.lock:
            for name in conn.names:
                self.docs[name].subscribers.discard(conn)
            conn.names.clear()


class Connection:
    def __init__(self, sock):
        self.sock = sock
        self.names = {}  # document name -> client name, for documents joined here
        self._outbox = queue.Queue()

    def send(self, msg):
        # Never blocks: a slow reader only grows its own queue.
        self._outbox.put(json.dumps(msg).encode("utf-8") + b"\n")

    def write_forever(self):
        while True:
            data = self._outbox.get()
            if data is None:
                return
            try:
                self.sock.sendall(data)
            except OSError:
                return  # the reader sees the broken socket and cleans up

    def close(self):
        self._outbox.put(None)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        service = self.server.service
        conn = Connection(self.request)
        threading.Thread(target=conn.write_forever, daemon=True).start()
        buf = b""
        try:
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    end = buf.find(b"\n")
                    if end < 0:
                        break
                    line, buf = buf[:end], buf[end + 1:]
                    self._dispatch(service, conn, line)
                if len(buf) > MAX_LINE:
                    conn.send({"type": "error", "code": "bad_request", "message": "line too long"})
                    break
        except OSError:
            pass
        finally:
            service.leave(conn)
            conn.close()

    def _dispatch(self, service, conn, line):
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            msg = None
        if not isinstance(msg, dict):
            conn.send({"type": "error", "code": "bad_json", "message": "line is not a JSON object"})
            return
        try:
            kind = msg.get("type")
            if kind == "join":
                service.join(conn, msg)
            elif kind == "op":
                service.submit(conn, msg)
            else:
                raise ProtocolError("bad_request", f"unknown message type {kind!r}")
        except ProtocolError as err:
            reply = {"type": "error", "code": err.code, "message": err.message}
            if "id" in msg:
                reply["id"] = msg["id"]
            conn.send(reply)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="Livedoc collaborative text server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    service = Service(args.data_dir)
    with Server(("127.0.0.1", args.port), service) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
