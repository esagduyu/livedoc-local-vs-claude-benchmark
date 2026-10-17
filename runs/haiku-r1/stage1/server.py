#!/usr/bin/env python3
"""Livedoc server: real-time collaborative editing of plain-text documents.

    python3 server.py --port PORT --data-dir DIR

Listens on 127.0.0.1:PORT. Every committed operation is appended to
DIR/livedoc.log before it is acknowledged, so the server rebuilds all state
from that file after a restart. See README.md for the design.
"""

import argparse
import json
import os
import queue
import socket
import socketserver
import threading

from livedoc_ot import apply_op, fits, is_wellformed, xf

LOG_FILE = "livedoc.log"
MAX_NAME_LENGTH = 100


def valid_doc_name(name):
    return isinstance(name, str) and 1 <= len(name) <= MAX_NAME_LENGTH


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class Document:
    """In-memory state of one document. Rebuilt from the log at startup."""

    def __init__(self, name):
        self.name = name
        self.text = ""
        self.ops = []             # ops[i] is the record of revision i + 1
        self.lengths = [0]        # lengths[r] is the text length at revision r
        self.seen = {}            # (client, id) -> revision that committed it
        self.subscribers = set()  # Connections joined to this document

    @property
    def rev(self):
        return len(self.ops)

    def apply(self, record):
        self.text = apply_op(self.text, record["op"])
        self.lengths.append(len(self.text))
        self.ops.append(record)
        self.seen[(record["client"], record["id"])] = record["rev"]


class Store:
    """All documents, plus the append-only log that makes them durable."""

    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, LOG_FILE)
        self.lock = threading.RLock()  # serializes every read and commit
        self.docs = {}
        self._load()
        self._log = open(self.path, "ab")

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            data = f.read()
        # A record cut off by a kill was never acknowledged: drop it so that
        # the next append starts on a fresh line.
        complete = data[: data.rfind(b"\n") + 1]
        if len(complete) != len(data):
            with open(self.path, "r+b") as f:
                f.truncate(len(complete))
        for line in complete.split(b"\n")[:-1]:
            record = json.loads(line)
            self.get_doc(record["doc"]).apply(record)

    def get_doc(self, name):
        doc = self.docs.get(name)
        if doc is None:
            doc = self.docs[name] = Document(name)
        return doc

    def commit(self, doc, client, op_id, op):
        """Write the operation to the log, then apply it. Caller holds self.lock."""
        record = {"doc": doc.name, "rev": doc.rev + 1, "client": client,
                  "id": op_id, "op": op}
        self._log.write(json.dumps(record).encode("ascii") + b"\n")
        self._log.flush()  # survives SIGKILL; fsync is not required
        doc.apply(record)
        return record


class Connection:
    """One client socket. The reader thread handles requests; a writer thread
    drains an outbox, so a slow client never blocks the commit path."""

    def __init__(self, sock, store):
        self.sock = sock
        self.store = store
        self.outbox = queue.Queue()
        self.names = {}  # document name -> client name, for documents joined here

    def send(self, message):
        """Queue a message. Never blocks, so it is safe to call under store.lock."""
        self.outbox.put(json.dumps(message).encode("ascii") + b"\n")

    def error(self, code, message, request=None):
        reply = {"type": "error", "code": code, "message": message}
        if isinstance(request, dict) and "id" in request:
            reply["id"] = request["id"]
        self.send(reply)

    def run(self):
        writer = threading.Thread(target=self._write_loop, daemon=True)
        writer.start()
        try:
            with self.sock.makefile("rb") as rfile:
                for line in rfile:
                    if not line.endswith(b"\n"):
                        break  # the client went away in the middle of a line
                    self._handle_line(line)
        except OSError:
            pass
        finally:
            with self.store.lock:
                for name in self.names:
                    self.store.docs[name].subscribers.discard(self)
            self.names.clear()
            self.outbox.put(None)
            writer.join()  # let queued replies go out before the socket closes

    def _write_loop(self):
        while True:
            data = self.outbox.get()
            if data is None:
                return
            try:
                self.sock.sendall(data)
            except OSError:
                # Wake the reader so this connection is cleaned up.
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    def _handle_line(self, line):
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            self.error("bad_json", "each line must be one JSON object")
            return
        kind = message.get("type")
        if kind == "join":
            self._join(message)
        elif kind == "op":
            self._op(message)
        else:
            self.error("bad_request", f"unknown message type {kind!r}", message)

    def _join(self, message):
        name = message.get("doc")
        client = message.get("client")
        if not valid_doc_name(name) or not isinstance(client, str) or not client:
            return self.error("bad_request",
                              "join needs doc (1-100 chars) and client (non-empty)",
                              message)
        has_since = "since" in message
        since = message.get("since")
        if has_since and not is_int(since):
            return self.error("bad_request", "since must be an integer", message)

        with self.store.lock:
            doc = self.store.get_doc(name)
            if has_since and not 0 <= since <= doc.rev:
                return self.error("bad_rev", f"since {since} is outside 0..{doc.rev}",
                                  message)
            # Subscribe and reply under the lock, so no broadcast can slip in
            # between the snapshot and the first broadcast this connection sees.
            doc.subscribers.add(self)
            self.names[name] = client
            if has_since:
                ops = [{"rev": r["rev"], "client": r["client"], "id": r["id"],
                        "op": r["op"]} for r in doc.ops[since:]]
                self.send({"type": "joined", "doc": name, "rev": doc.rev, "ops": ops})
            else:
                self.send({"type": "joined", "doc": name, "rev": doc.rev,
                           "text": doc.text})

    def _op(self, message):
        name = message.get("doc")
        rev = message.get("rev")
        op_id = message.get("id")
        op = message.get("op")
        if (not valid_doc_name(name) or not is_int(rev) or not isinstance(op_id, str)
                or not is_wellformed(op)):
            return self.error("bad_request", "malformed op message", message)

        with self.store.lock:
            if name not in self.names:
                return self.error("not_joined", f"not joined to {name!r}", message)
            doc = self.store.docs[name]
            client = self.names[name]

            committed_rev = doc.seen.get((client, op_id))
            if committed_rev is not None:
                # Exactly once: a retransmission is acknowledged, never reapplied.
                return self.send({"type": "ack", "doc": name, "id": op_id,
                                  "rev": committed_rev})
            if not 0 <= rev <= doc.rev:
                return self.error("bad_rev", f"rev {rev} is outside 0..{doc.rev}", message)
            if not fits(op, doc.lengths[rev]):
                return self.error("out_of_range",
                                  f"operation does not fit the text at rev {rev}",
                                  message)

            for committed in doc.ops[rev:]:
                op, _ = xf(op, committed["op"])
            record = self.store.commit(doc, client, op_id, op)

            broadcast = {"type": "op", "doc": name, "rev": record["rev"],
                         "client": client, "id": op_id, "op": op}
            for sub in doc.subscribers:
                if sub is not self:
                    sub.send(broadcast)
            self.send({"type": "ack", "doc": name, "id": op_id, "rev": record["rev"]})


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        Connection(self.request, self.server.store).run()


class LivedocServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, data_dir):
        self.store = Store(data_dir)
        super().__init__(address, _Handler)


def make_server(port, data_dir, host="127.0.0.1"):
    """Bind a server without starting it. Pass port 0 to pick a free port."""
    return LivedocServer((host, port), data_dir)


def main():
    parser = argparse.ArgumentParser(description="Livedoc collaborative text server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    server = make_server(args.port, args.data_dir)
    print(f"livedoc listening on 127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
