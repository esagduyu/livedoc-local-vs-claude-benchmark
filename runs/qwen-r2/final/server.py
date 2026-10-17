#!/usr/bin/env python3
"""Livedoc server – real-time collaborative plain-text editing over TCP."""

import argparse
import json
import os
import signal
import socket
import sys
import threading
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Transformation helpers
# ---------------------------------------------------------------------------

def transform_op_a_against_b(A, B):
    """Transform operation A against earlier-committed operation B.
    Returns (A_prime, B_prime) where each is a list of edits.
    A_prime applies after B, B_prime applies after A.

    Per the spec:
      if A or B is empty: return (A, B)
      if both have one edit: return edit-vs-edit rule (both directions)
      if A has more than one edit:
          (a1', B1) = xf([a1], B); (rest', B2) = xf(A[1:], B1); return (a1' + rest', B2)
      if B has more than one edit:
          (A1, b1') = xf(A, [b1]); (A2, rest') = xf(A1, B[1:]); return (A2, b1' + rest')
    """

    if not A or not B:
        return list(A) if A else [], list(B) if B else []

    if len(A) == 1 and len(B) == 1:
        a_prime, b_prime = transform_edit_a_against_b(A[0], B[0])
        A_prime = [] if a_prime is None else [a_prime]
        B_prime = [] if b_prime is None else [b_prime]
        return A_prime, B_prime

    if len(A) > 1:
        # (a1', B1) = xf([a1], B) where B1 is a list
        a1_prime, b1_prime = transform_edit_a_against_b(A[0], B[0])
        A1 = [] if a1_prime is None else [a1_prime]
        B1_extended = [b1_prime] + B[1:]
        A_rest = A[1:]
        if not A_rest:
            return A1, B1_extended
        else:
            # (rest', B2) = xf(A[1:], B1)
            A_rest_prime, B_final = transform_op_a_against_b(A_rest, B1_extended)
            return A1 + A_rest_prime, B_final

    # len(B) > 1, len(A) == 1
    # (A1, b1') = xf(A, [b1])
    A1, b1_prime = transform_edit_a_against_b(A[0], B[0])
    if A1 is None:
        A1 = []
    else:
        A1 = [A1]
    # (A2, rest') = xf(A1, B[1:])
    if len(B) == 1:
        return A1, []
    A2, B_rest_prime = transform_op_a_against_b(A1, B[1:])
    # return (A2, b1' + rest')
    B_prime = [b1_prime] + B_rest_prime
    return A2, B_prime


def transform_edit_a_against_b(a, b):
    """Transform edit *a* against earlier-committed edit *b*.
    Returns (a_prime, b_prime) where a_prime applies after b,
    and b_prime applies after a.
    a_prime is a single edit dict, None (vanished), or a list of 2 edits.
    b_prime is always a single edit dict (never a list)."""

    a_is_ins = "ins" in a
    b_is_ins = "ins" in b

    if a_is_ins and b_is_ins:
        pa = a["ins"]
        ta = a["text"]
        la = len(ta)
        qb = b["ins"]
        tb = b["text"]
        lb = len(tb)
        if pa < qb:
            a_prime = {"ins": pa, "text": ta}
        else:
            a_prime = {"ins": pa + lb, "text": ta}
        if qb <= pa:
            b_prime = {"ins": qb, "text": tb}
        else:
            b_prime = {"ins": qb + la, "text": tb}
        return a_prime, b_prime

    elif a_is_ins and not b_is_ins:
        pa = a["ins"]
        ta = a["text"]
        qb = b["del"]
        kb = b["len"]
        if pa <= qb:
            a_prime = {"ins": pa, "text": ta}
        elif pa >= qb + kb:
            a_prime = {"ins": pa - kb, "text": ta}
        else:
            a_prime = {"ins": qb, "text": ta}
        if pa <= qb:
            b_prime = {"del": qb + len(ta), "len": kb}
        elif pa >= qb + kb:
            b_prime = b
        else:
            b_prime = {"del": qb, "len": pa - qb}
        return a_prime, b_prime

    elif not a_is_ins and b_is_ins:
        pa = a["del"]
        ma = a["len"]
        qb = b["ins"]
        tb = b["text"]
        lb = len(tb)
        if qb <= pa:
            a_prime = {"del": pa + lb, "len": ma}
        elif qb >= pa + ma:
            a_prime = a
        else:
            a_prime = [{"del": pa, "len": qb - pa},
                        {"del": pa + lb, "len": pa + ma - qb}]
        if qb <= pa:
            b_prime = {"ins": qb, "text": tb}
        elif qb >= pa + ma:
            b_prime = {"ins": qb - ma, "text": tb}
        else:
            b_prime = {"ins": qb, "text": tb}
        return a_prime, b_prime

    else:
        # both del
        pa = a["del"]
        ma = a["len"]
        qb = b["del"]
        kb = b["len"]
        if pa + ma <= qb or pa >= qb + kb:
            a_prime = a
        elif pa >= qb and pa + ma <= qb + kb:
            a_prime = None
        elif pa < qb and pa + ma > qb + kb:
            a_prime = {"del": pa, "len": ma - kb}
        elif pa < qb:
            a_prime = {"del": pa, "len": qb - pa}
        else:
            # a starts inside b and extends beyond b's end
            # a's remaining part [qb+kb, pa+ma) shifts left by kb
            a_prime = {"del": qb, "len": pa + ma - qb - kb}

        if a_prime is None:
            b_prime = b
        else:
            # Determine b' based on how b relates to a
            if qb <= pa and qb + kb <= pa:
                # b is entirely before a
                b_prime = {"del": qb, "len": kb}
            elif qb >= pa + ma:
                # b is entirely after a
                b_prime = {"del": qb - ma, "len": kb}
            elif qb < pa and qb + kb > pa:
                # b starts before a and overlaps
                # b's part before a: [qb, pa)
                # b's part after a: [qb+kb, pa+kb) but only if qb+kb < pa+ma
                overlap_start = max(qb, pa)
                overlap_end = min(qb + kb, pa + ma)
                overlap_len = overlap_end - overlap_start
                # b_prime removes b's part that doesn't overlap with a
                b_prime = {"del": qb, "len": kb - overlap_len}
            elif qb + kb <= pa:
                # b is entirely before a
                b_prime = {"del": qb, "len": kb}
            elif qb >= pa:
                # b starts at or after a, but b extends beyond a
                # a is inside b or they overlap with b starting at or after a
                overlap_start = max(qb, pa)
                overlap_end = min(qb + kb, pa + ma)
                overlap_len = overlap_end - overlap_start
                b_prime = {"del": qb, "len": kb - overlap_len}
            else:
                # b is entirely inside a
                b_prime = None
        return a_prime, b_prime


def transform_op_against_history(op, history_ops):
    """Transform *op* against a sequence of earlier-committed operations.
    Returns the transformed operation."""
    for hist_op in history_ops:
        # Extract the op from history entry (dict with "op" key) or use as-is (list)
        if isinstance(hist_op, dict) and "op" in hist_op:
            edits = hist_op["op"]
        elif isinstance(hist_op, list):
            edits = hist_op
        else:
            edits = [hist_op]
        # Transform op against each edit of the history operation in order
        for edit in edits:
            op, _ = transform_op_a_against_b(op, [edit])
    return op


# ---------------------------------------------------------------------------
# Document / persistence
# ---------------------------------------------------------------------------

def apply_op(text, op):
    """Apply an operation (list of edits) to a text string. Returns new text."""
    chars = list(text)
    for edit in op:
        if "ins" in edit:
            pos = edit["ins"]
            t = edit["text"]
            chars[pos:pos] = list(t)
        elif "del" in edit:
            pos = edit["del"]
            ln = edit["len"]
            del chars[pos:pos + ln]
    return "".join(chars)


class Document:
    def __init__(self, name):
        self.name = name
        self.text = ""
        self.rev = 0
        self.history = []  # list of {"rev": r, "client": c, "id": i, "op": op}
        self.committed_ids = set()  # (client, id) tuples for exactly-once

    def commit_op(self, client, id_, op, base_rev):
        """Transform op against history since base_rev, apply, commit.
        Returns (new_rev, transformed_op) or None if duplicate."""
        key = (client, id_)
        if key in self.committed_ids:
            return None  # duplicate

        hist = self.history[base_rev:]
        transformed = transform_op_against_history(op, hist)
        self.text = apply_op(self.text, transformed)
        self.rev += 1
        self.history.append({"rev": self.rev, "client": client, "id": id_, "op": transformed})
        self.committed_ids.add(key)
        return (self.rev, transformed)


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, port, data_dir):
        self.port = port
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.docs = {}  # name -> Document
        self.lock = threading.Lock()
        self.connections = []  # list of Connection
        self._shutdown = False

    # --- Persistence ---

    def _save_path(self):
        return self.data_dir / "state.json"

    def save_state(self):
        state = {}
        for name, doc in self.docs.items():
            state[name] = {
                "text": doc.text,
                "rev": doc.rev,
                "history": doc.history,
                "committed_ids": [list(k) for k in doc.committed_ids],
            }
        tmp = self.data_dir / "state.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        tmp.replace(self._save_path())

    def load_state(self):
        sp = self._save_path()
        if not sp.exists():
            return
        try:
            with open(sp, "r", encoding="utf-8") as f:
                state = json.load(f)
            for name, info in state.items():
                doc = Document(name)
                doc.text = info["text"]
                doc.rev = info["rev"]
                doc.history = info["history"]
                doc.committed_ids = set(tuple(k) for k in info["committed_ids"])
                self.docs[name] = doc
        except Exception:
            pass

    # --- Connection handling ---

    def add_connection(self, conn):
        with self.lock:
            self.connections.append(conn)

    def remove_connection(self, conn):
        with self.lock:
            if conn in self.connections:
                self.connections.remove(conn)

    def broadcast(self, doc_name, msg, exclude=None):
        with self.lock:
            targets = [c for c in self.connections if c.doc_names and doc_name in c.doc_names and c is not exclude]
        for c in targets:
            c.send(msg)

    def shutdown(self):
        self._shutdown = True
        with self.lock:
            for c in self.connections:
                c.close()

    # --- Message dispatch ---

    def handle_message(self, conn, msg, raw_line):
        """Process one JSON message from a connection."""
        try:
            msg_type = msg.get("type")
        except Exception:
            conn.send_error("bad_json", "Not a JSON object")
            return

        if msg_type == "join":
            self._handle_join(conn, msg)
        elif msg_type == "op":
            self._handle_op(conn, msg)
        else:
            conn.send_error("bad_request", f"Unknown type: {msg_type}")

    def _handle_join(self, conn, msg):
        doc_name = msg.get("doc")
        client_name = msg.get("client")
        since = msg.get("since")

        if not isinstance(doc_name, str) or not doc_name:
            conn.send_error("bad_request", "doc must be a non-empty string")
            return
        if not isinstance(client_name, str) or not client_name:
            conn.send_error("bad_request", "client must be a non-empty string")
            return
        if len(doc_name) > 100:
            conn.send_error("bad_request", "doc name too long")
            return

        with self.lock:
            if doc_name not in self.docs:
                self.docs[doc_name] = Document(doc_name)
            doc = self.docs[doc_name]

        if since is not None:
            if not isinstance(since, int) or since < 0 or since > doc.rev:
                conn.send_error("bad_rev", f"since must be 0 <= since <= {doc.rev}")
                return
            ops = [{"rev": h["rev"], "client": h["client"], "id": h["id"], "op": h["op"]}
                   for h in doc.history[since:]]
            reply = {"type": "joined", "doc": doc_name, "rev": doc.rev, "ops": ops}
        else:
            reply = {"type": "joined", "doc": doc_name, "rev": doc.rev, "text": doc.text}

        conn.doc_names.add(doc_name)
        conn.client_name = client_name
        conn.send(reply)

    def _handle_op(self, conn, msg):
        doc_name = msg.get("doc")
        client_name = conn.client_name
        rev = msg.get("rev")
        op_id = msg.get("id")
        op = msg.get("op")

        # Validate fields
        if not isinstance(doc_name, str) or not doc_name:
            conn.send_error("bad_request", "doc must be a non-empty string")
            return
        if not isinstance(client_name, str) or not client_name:
            conn.send_error("bad_request", "client must have joined first")
            return
        if not isinstance(op_id, str) or not op_id:
            conn.send_error("bad_request", "id must be a non-empty string")
            return
        if not isinstance(op, list):
            conn.send_error("bad_request", "op must be a list")
            return

        # Check joined
        if doc_name not in conn.doc_names:
            conn.send_error("not_joined", f"Not joined to {doc_name}")
            return

        # Validate rev
        with self.lock:
            if doc_name not in self.docs:
                conn.send_error("bad_rev", f"Document {doc_name} does not exist")
                return
            doc = self.docs[doc_name]

        if not isinstance(rev, int) or rev < 0 or rev > doc.rev:
            conn.send_error("bad_rev", f"rev must be 0 <= rev <= {doc.rev}")
            return

        # Validate operation against text at rev
        text_at_rev = self._text_at_rev(doc, rev)
        for edit in op:
            if not isinstance(edit, dict):
                conn.send_error("bad_request", "Each edit must be a dict")
                return
            if "ins" in edit:
                if not isinstance(edit["ins"], int) or edit["ins"] < 0 or edit["ins"] > len(text_at_rev):
                    conn.send_error("out_of_range", "Insert position out of range")
                    return
                if not isinstance(edit["text"], str) or len(edit["text"]) == 0:
                    conn.send_error("out_of_range", "Insert text must be non-empty")
                    return
            elif "del" in edit:
                if not isinstance(edit["del"], int) or edit["del"] < 0 or edit["del"] > len(text_at_rev):
                    conn.send_error("out_of_range", "Delete position out of range")
                    return
                if not isinstance(edit["len"], int) or edit["len"] < 1:
                    conn.send_error("out_of_range", "Delete length must be >= 1")
                    return
                if edit["del"] + edit["len"] > len(text_at_rev):
                    conn.send_error("out_of_range", "Delete extends past end of text")
                    return
            else:
                conn.send_error("bad_request", "Edit must have 'ins' or 'del'")
                return

        # Commit (under lock)
        with self.lock:
            result = doc.commit_op(client_name, op_id, op, rev)
            if result is None:
                # Duplicate
                conn.send({"type": "ack", "doc": doc_name, "id": op_id, "rev": doc.rev})
                return
            new_rev, transformed_op = result

        # Ack to sender first, then broadcast to others
        conn.send({"type": "ack", "doc": doc_name, "id": op_id, "rev": new_rev})

        broadcast_msg = {
            "type": "op",
            "doc": doc_name,
            "rev": new_rev,
            "client": client_name,
            "id": op_id,
            "op": transformed_op,
        }
        self.broadcast(doc_name, broadcast_msg, exclude=conn)

        # Persist
        self.save_state()

    def _text_at_rev(self, doc, rev):
        """Compute the text at a given revision."""
        t = ""
        for h in doc.history[:rev]:
            t = apply_op(t, h["op"])
        return t


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

class Connection:
    def __init__(self, sock, server, handler):
        self.sock = sock
        self.server = server
        self.handler = handler
        self.doc_names = set()
        self.client_name = None  # set on join
        self._closed = False
        self._lock = threading.Lock()

    def send(self, obj):
        if self._closed:
            return
        try:
            line = json.dumps(obj, ensure_ascii=False) + "\n"
            self.sock.sendall(line.encode("utf-8"))
        except OSError:
            self._closed = True

    def send_error(self, code, message, id_=None):
        err = {"type": "error", "code": code, "message": message}
        if id_ is not None:
            err["id"] = id_
        self.send(err)

    def close(self):
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def run(self):
        buf = b""
        try:
            while not self.server._shutdown:
                data = self.sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        msg = json.loads(line_str)
                    except (json.JSONDecodeError, ValueError):
                        self.send_error("bad_json", "Invalid JSON")
                        continue
                    if not isinstance(msg, dict):
                        self.send_error("bad_json", "Not a JSON object")
                        continue
                    self.server.handle_message(self, msg, line_str)
        except OSError:
            pass
        finally:
            self.server.remove_connection(self)
            self.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Livedoc server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=str, default=".livedoc_data")
    args = parser.parse_args()

    server = Server(args.port, args.data_dir)
    server.load_state()

    # Listen
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(128)
    sock.settimeout(0.5)  # for shutdown polling

    print(f"Livedoc server listening on 127.0.0.1:{args.port}")

    def handle_client(conn_sock):
        conn = Connection(conn_sock, server, None)
        server.add_connection(conn)
        conn.run()

    def shutdown_handler(signum, frame):
        server.save_state()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        while not server._shutdown:
            try:
                conn_sock, addr = sock.accept()
                conn_sock.settimeout(None)
                t = threading.Thread(target=handle_client, args=(conn_sock,), daemon=True)
                t.start()
            except OSError:
                if server._shutdown:
                    break
                continue
    finally:
        server.save_state()
        sock.close()


if __name__ == "__main__":
    main()
