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

from livedoc_ot import apply_op, fits, is_wellformed, move_cursor_op, xf

LOG_FILE = "livedoc.log"
MAX_NAME_LENGTH = 100
CHECKPOINT_EVERY = 128  # keep the text every this many revisions, for fast snapshots


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
        self.checkpoints = {0: ""}  # revision -> text, every CHECKPOINT_EVERY revisions
        self.seen = {}            # (client, id) -> revision that committed it
        self.subscribers = set()  # Connections joined to this document
        self.members = {}         # client name -> number of connections joined under it
        self.cursors = {}         # client name -> cursor position at the current rev

    @property
    def rev(self):
        return len(self.ops)

    def apply(self, record):
        author = record["client"]
        self.text = apply_op(self.text, record["op"])
        self.lengths.append(len(self.text))
        self.ops.append(record)
        self.seen[(author, record["id"])] = record["rev"]
        if self.rev % CHECKPOINT_EVERY == 0:
            self.checkpoints[self.rev] = self.text
        for name, pos in self.cursors.items():
            self.cursors[name] = move_cursor_op(pos, record["op"], name == author)

    def text_at(self, rev):
        """Text at `rev`, rebuilt from the nearest checkpoint at or below it."""
        base = rev - rev % CHECKPOINT_EVERY
        text = self.checkpoints[base]
        for record in self.ops[base:rev]:
            text = apply_op(text, record["op"])
        return text


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

    def close(self):
        self._log.close()

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
                for name, client in self.names.items():
                    doc = self.store.docs[name]
                    doc.subscribers.discard(self)
                    self._release(doc, client)
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
        handler = {"join": self._join, "op": self._op, "cursor": self._cursor,
                   "cursors": self._cursors, "snapshot": self._snapshot}.get(kind)
        if handler is None:
            self.error("bad_request", f"unknown message type {kind!r}", message)
        else:
            handler(message)

    def _release(self, doc, client):
        """Drop one connection of `client` from `doc`. Caller holds the store lock.
        When it was the name's last connection, its cursor goes and the rest hear about it."""
        doc.members[client] -= 1
        if doc.members[client] > 0:
            return
        del doc.members[client]
        doc.cursors.pop(client, None)
        for sub in doc.subscribers:
            sub.send({"type": "leave", "doc": doc.name, "client": client})

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
            previous = self.names.get(name)
            if previous != client:
                doc.members[client] = doc.members.get(client, 0) + 1
            doc.subscribers.add(self)
            self.names[name] = client
            if has_since:
                ops = [{"rev": r["rev"], "client": r["client"], "id": r["id"],
                        "op": r["op"]} for r in doc.ops[since:]]
                self.send({"type": "joined", "doc": name, "rev": doc.rev, "ops": ops})
            else:
                self.send({"type": "joined", "doc": name, "rev": doc.rev,
                           "text": doc.text})
            if previous is not None and previous != client:
                self._release(doc, previous)  # this connection renamed itself

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

    def _cursor(self, message):
        name = message.get("doc")
        rev = message.get("rev")
        pos = message.get("pos")
        if not valid_doc_name(name) or not is_int(rev) or not is_int(pos):
            return self.error("bad_request", "cursor needs doc, integer rev and pos",
                              message)

        with self.store.lock:
            if name not in self.names:
                return self.error("not_joined", f"not joined to {name!r}", message)
            doc = self.store.docs[name]
            client = self.names[name]
            if not 0 <= rev <= doc.rev:
                return self.error("bad_rev", f"rev {rev} is outside 0..{doc.rev}", message)
            if not 0 <= pos <= doc.lengths[rev]:
                return self.error("out_of_range",
                                  f"position {pos} is outside the text at rev {rev}",
                                  message)
            # Bring the position up to date, as if it had been stored at `rev`.
            for committed in doc.ops[rev:]:
                pos = move_cursor_op(pos, committed["op"], committed["client"] == client)
            doc.cursors[client] = pos
            for sub in doc.subscribers:
                if sub is not self:
                    sub.send({"type": "cursor", "doc": name, "client": client,
                              "rev": doc.rev, "pos": pos})

    def _cursors(self, message):
        name = message.get("doc")
        if not valid_doc_name(name):
            return self.error("bad_request", "cursors needs a doc name", message)
        with self.store.lock:
            if name not in self.names:
                return self.error("not_joined", f"not joined to {name!r}", message)
            doc = self.store.docs[name]
            self.send({"type": "cursors", "doc": name, "rev": doc.rev,
                       "cursors": dict(doc.cursors)})

    def _snapshot(self, message):
        name = message.get("doc")
        rev = message.get("rev")
        if not valid_doc_name(name) or not is_int(rev):
            return self.error("bad_request", "snapshot needs doc and integer rev", message)
        with self.store.lock:
            doc = self.store.docs.get(name)
            if doc is None:
                return self.error("not_found", f"no document {name!r}", message)
            if not 0 <= rev <= doc.rev:
                return self.error("bad_rev", f"rev {rev} is outside 0..{doc.rev}", message)
            self.send({"type": "snapshot", "doc": name, "rev": rev,
                       "text": doc.text_at(rev)})


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

    def server_close(self):
        super().server_close()
        self.store.close()


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
