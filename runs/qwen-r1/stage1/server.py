#!/usr/bin/env python3
"""Livedoc server – real-time collaborative plain-text editing over TCP."""

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Transformation engine
#
# Two edits a and b, both written against the same base text T.
#
# _xf_a_past_b(a, b): transform later edit a past earlier edit b.
#   Result: a_prime such that apply(b, apply(T, a_prime)) = apply(a, apply(T, b))
#
# _xf_b_past_a(a, b): transform earlier edit b past later edit a (reverse).
#   Result: b_prime such that apply(a, apply(T, b_prime)) = apply(b, apply(T, a))
#
# The spec gives forward rules for transforming a past b.
# For the reverse, we apply the same rules with roles reversed.
# ---------------------------------------------------------------------------

def _xf_a_past_b(a, b):
    """Transform later edit a past earlier edit b.

    Returns a single edit dict, a list of edits, or an empty list.
    """
    ai = "ins" in a
    ad = "del" in a
    bi = "ins" in b
    bd = "del" in b

    if ai and bi:
        pa, ta = a["ins"], a["text"]
        qb, ub = b["ins"], b["text"]
        if pa < qb:
            return a
        else:
            # pa >= qb: shift right by len(ub). On tie, b (earlier) stays left.
            return {"ins": pa + len(ub), "text": ta}

    if ai and bd:
        pa, ta = a["ins"], a["text"]
        qb, kb = b["del"], b["len"]
        if pa <= qb:
            return a
        elif pa >= qb + kb:
            return {"ins": pa - kb, "text": ta}
        else:
            # a strictly inside b's deletion range
            return {"ins": qb, "text": ta}

    if ad and bi:
        pa, ma = a["del"], a["len"]
        qb, ub = b["ins"], b["text"]
        if qb <= pa:
            return {"del": pa + len(ub), "len": ma}
        elif qb >= pa + ma:
            return a
        else:
            # b's insert is strictly inside a's deletion range
            return [
                {"del": pa, "len": qb - pa},
                {"del": pa + len(ub), "len": pa + ma - qb},
            ]

    if ad and bd:
        pa, ma = a["del"], a["len"]
        qb, kb = b["del"], b["len"]
        if pa < qb:
            before_end = min(pa + ma, qb)
            before_len = before_end - pa
            if pa + ma > qb + kb:
                # Both parts exist and are adjacent after b's delete
                merged_len = before_len + (pa + ma - qb - kb)
                return [{"del": pa, "len": merged_len}]
            else:
                return [{"del": pa, "len": before_len}]
        else:
            rem_start = max(pa, qb + kb)
            rem_end = pa + ma
            if rem_end > rem_start:
                shifted_start = rem_start - kb
                length = rem_end - rem_start
                return [{"del": shifted_start, "len": length}]
            return []

    return a


def _xf_b_past_a(a, b):
    """Transform earlier edit b past later edit a (reverse transform).

    Apply the same transformation rules with roles reversed.
    """
    ai = "ins" in a
    ad = "del" in a
    bi = "ins" in b
    bd = "del" in b

    if ai and bi:
        # Both inserts. b is earlier. Move past later insert a.
        # On tie, b (earlier) stays left.
        pa, ta = a["ins"], a["text"]
        qb, ub = b["ins"], b["text"]
        if qb <= pa:
            return b
        else:
            return {"ins": qb + len(ta), "text": ub}

    if ai and bd:
        # a=ins (later), b=del (earlier). Move b past a.
        # Forward rule for del past ins: ins at q, del at p
        #   q <= p: del at p+len(u), same len
        #   q >= p+m: unchanged
        #   otherwise: split
        # Here b (del at qb) is being transformed past a (ins at pa).
        pa, ta = a["ins"], a["text"]
        qb, kb = b["del"], b["len"]
        if pa <= qb:
            return {"del": qb + len(ta), "len": kb}
        elif pa >= qb + kb:
            return b
        else:
            # a's insert is strictly inside b's deletion range
            # Split b into two deletes.
            # After first delete del(qb, pa-qb), positions shift.
            # Second delete: del(qb+len(ta), kb-len(ta))
            return [
                {"del": qb, "len": pa - qb},
                {"del": qb + len(ta), "len": kb - len(ta)},
            ]

    if ad and bi:
        # a=del (later), b=ins (earlier). Move b past a.
        # Forward rule for ins past del:
        #   del at p, ins at q:
        #     q <= p: unchanged
        #     q >= p+m: ins at q-m
        #     otherwise: ins at p
        pa, ma = a["del"], a["len"]
        qb, ub = b["ins"], b["text"]
        if qb <= pa:
            return b
        elif qb >= pa + ma:
            return {"ins": qb - ma, "text": ub}
        else:
            # b strictly inside a's deletion range
            return {"ins": pa, "text": ub}

    if ad and bd:
        # Both deletes. b is earlier. Move past a.
        pa, ma = a["del"], a["len"]
        qb, kb = b["del"], b["len"]
        if qb < pa:
            before_end = min(qb + kb, pa)
            before_len = before_end - qb
            rem_start = max(qb, pa + ma)
            rem_end = qb + kb
            if rem_end > rem_start:
                merged_len = before_len + (rem_end - rem_start)
                return [{"del": qb, "len": merged_len}]
            else:
                return [{"del": qb, "len": before_len}]
        else:
            rem_start = max(qb, pa + ma)
            rem_end = qb + kb
            if rem_end > rem_start:
                shifted_start = rem_start - ma
                length = rem_end - rem_start
                return [{"del": shifted_start, "len": length}]
            return []

    return b


def _flatten(e):
    if isinstance(e, list):
        return [x for x in e if x]
    elif isinstance(e, dict):
        return [e] if e else []
    return []


def transform_op(A, B):
    """Transform operation A past earlier-committed operation B.

    Returns (A_prime, B_prime) where:
    - A_prime applies after B (server transforms incoming op past history)
    - B_prime applies after A (for broadcasting to other clients)
    """
    if not A and not B:
        return ([], [])
    if not A:
        return ([], list(B))
    if not B:
        return (list(A), [])

    A_list = list(A)
    B_list = list(B)

    def xf(A_l, B_l):
        if not A_l or not B_l:
            return (list(A_l), list(B_l))

        if len(A_l) == 1 and len(B_l) == 1:
            a1 = A_l[0]
            b1 = B_l[0]
            a1p = _xf_a_past_b(a1, b1)
            b1p = _xf_b_past_a(a1, b1)
            return (_flatten(a1p), _flatten(b1p))

        if len(A_l) > 1:
            a1 = A_l[0]
            A_rest = A_l[1:]
            a1p, B1 = xf([a1], B_l)
            A_rest_p, B2 = xf(A_rest, B1)
            return (a1p + A_rest_p, B2)

        # len(B_l) > 1
        A1, b1p = xf(A_l, [B_l[0]])
        A2, B_rest = xf(A1, B_l[1:])
        return (A2, _flatten(b1p) + B_rest)

    A_prime, B_prime = xf(A_list, B_list)
    A_prime = [e for e in A_prime if e]
    B_prime = [e for e in B_prime if e]
    return (A_prime, B_prime)


# ---------------------------------------------------------------------------
# Document state
# ---------------------------------------------------------------------------

class Document:
    def __init__(self, name: str):
        self.name = name
        self.text = ""
        self.rev = 0
        self.history: list[dict] = []
        self.committed_ids: set[tuple[str, str]] = set()
        self._broadcast_queues: dict[int, list[dict]] = {}

    def apply_op(self, op):
        chars = list(self.text)
        for edit in op:
            if "ins" in edit:
                p = edit["ins"]
                t = edit["text"]
                chars[p:p] = list(t)
            elif "del" in edit:
                p = edit["del"]
                n = edit["len"]
                del chars[p : p + n]
        self.text = "".join(chars)
        return self.text

    def get_ops_since(self, rev):
        return [h for h in self.history if h["rev"] > rev]

    def broadcast(self, msg):
        for q in self._broadcast_queues.values():
            q.append(msg)

    def add_client(self, client_id):
        self._broadcast_queues[client_id] = []

    def remove_client(self, client_id):
        self._broadcast_queues.pop(client_id, None)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, port, data_dir):
        self.port = port
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.documents: dict[str, Document] = {}
        self._docs_lock = threading.Lock()
        self._conn_counter = 0
        self._conn_lock = threading.Lock()
        self._conn_names: dict[int, dict[str, str]] = {}

    def _load_state(self):
        path = self.data_dir / "documents.json"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            for d in state:
                doc = Document(d["name"])
                doc.text = d["text"]
                doc.rev = d["rev"]
                doc.history = d["history"]
                doc.committed_ids = set(tuple(x) for x in d["committed_ids"])
                self.documents[d["name"]] = doc

    def _save_state(self):
        path = self.data_dir / "documents.json"
        with self._docs_lock:
            data = []
            for doc in self.documents.values():
                data.append({
                    "name": doc.name,
                    "text": doc.text,
                    "rev": doc.rev,
                    "history": doc.history,
                    "committed_ids": sorted(doc.committed_ids),
                })
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))

    @staticmethod
    def _validate_op(op, text_len):
        if not isinstance(op, list):
            return False, "op must be a list"
        for edit in op:
            if not isinstance(edit, dict):
                return False, "each edit must be a dict"
            if "ins" in edit:
                p = edit["ins"]
                t = edit["text"]
                if not isinstance(p, int) or p < 0 or p > text_len:
                    return False, "insert position out of range"
                if not isinstance(t, str) or len(t) == 0:
                    return False, "insert text must be non-empty"
            elif "del" in edit:
                p = edit["del"]
                n = edit["len"]
                if not isinstance(p, int) or p < 0 or p + n > text_len:
                    return False, "delete out of range"
                if not isinstance(n, int) or n < 1:
                    return False, "delete len must be >= 1"
            else:
                return False, "edit must have 'ins' or 'del'"
        return True, None

    def _handle_join(self, conn_id, msg):
        doc_name = msg.get("doc")
        client_name = msg.get("client")
        since = msg.get("since")

        if not isinstance(doc_name, str) or not doc_name:
            return {"type": "error", "code": "bad_request", "message": "doc must be a non-empty string"}
        if not isinstance(client_name, str) or not client_name:
            return {"type": "error", "code": "bad_request", "message": "client must be a non-empty string"}
        if since is not None:
            if not isinstance(since, int) or since < 0:
                return {"type": "error", "code": "bad_rev", "message": "since must be non-negative"}

        with self._docs_lock:
            if doc_name not in self.documents:
                self.documents[doc_name] = Document(doc_name)
            doc = self.documents[doc_name]

            if since is not None and since > doc.rev:
                return {"type": "error", "code": "bad_rev", "message": f"since {since} > rev {doc.rev}"}

            doc.add_client(conn_id)
            self._conn_names.setdefault(conn_id, {})[doc_name] = client_name

            if since is not None and since < doc.rev:
                ops = doc.get_ops_since(since)
                return {"type": "joined", "doc": doc_name, "rev": doc.rev, "ops": ops}
            else:
                return {"type": "joined", "doc": doc_name, "rev": doc.rev, "text": doc.text}

    def _handle_op(self, conn_id, msg):
        doc_name = msg.get("doc")
        rev = msg.get("rev")
        op_id = msg.get("id")
        op = msg.get("op")
        client_name = self._conn_names.get(conn_id, {}).get(doc_name, "")

        if not isinstance(doc_name, str):
            return {"type": "error", "code": "bad_request", "message": "doc must be a string"}
        if not isinstance(op_id, str) or not op_id:
            return {"type": "error", "code": "bad_request", "message": "id must be non-empty"}

        with self._docs_lock:
            if doc_name not in self.documents:
                return {"type": "error", "code": "not_joined", "message": f"not joined to {doc_name}"}
            doc = self.documents[doc_name]

            if conn_id not in doc._broadcast_queues:
                return {"type": "error", "code": "not_joined", "message": f"not joined to {doc_name}"}

            if not isinstance(rev, int) or rev < 0 or rev > doc.rev:
                return {"type": "error", "code": "bad_rev", "message": f"rev {rev} out of range"}

            if (client_name, op_id) in doc.committed_ids:
                return {"type": "ack", "doc": doc_name, "id": op_id, "rev": rev}

            text_at_rev = self._reconstruct_text(doc, rev)
            ok, err = self._validate_op(op, len(text_at_rev))
            if not ok:
                return {"type": "error", "code": "out_of_range", "message": err}

            ops_after = doc.get_ops_since(rev)
            transformed_op = list(op)
            for eop in ops_after:
                transformed_op, _ = transform_op(transformed_op, eop["op"])

            doc.apply_op(transformed_op)
            doc.rev += 1
            new_rev = doc.rev

            entry = {"rev": new_rev, "client": client_name, "id": op_id, "op": transformed_op}
            doc.history.append(entry)
            doc.committed_ids.add((client_name, op_id))

        self._save_state()

        ack = {"type": "ack", "doc": doc_name, "id": op_id, "rev": new_rev}

        broadcast_msg = {
            "type": "op", "doc": doc_name, "rev": new_rev,
            "client": client_name, "id": op_id, "op": transformed_op,
        }
        doc.broadcast(broadcast_msg)

        return ack

    @staticmethod
    def _reconstruct_text(doc, rev):
        text = ""
        for h in doc.history:
            if h["rev"] > rev:
                break
            chars = list(text)
            for edit in h["op"]:
                if "ins" in edit:
                    p = edit["ins"]
                    t = edit["text"]
                    chars[p:p] = list(t)
                elif "del" in edit:
                    p = edit["del"]
                    n = edit["len"]
                    del chars[p : p + n]
            text = "".join(chars)
        return text

    def _flush_client(self, conn, conn_id):
        with self._docs_lock:
            for doc in self.documents.values():
                q = doc._broadcast_queues.get(conn_id)
                if q:
                    for m in q:
                        try:
                            conn.sendall((json.dumps(m, ensure_ascii=False) + "\n").encode("utf-8"))
                        except OSError:
                            pass
                    doc._broadcast_queues[conn_id] = []

    def _handle_client(self, conn, addr):
        with self._conn_lock:
            conn_id = self._conn_counter
            self._conn_counter += 1

        try:
            buf = b""
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    raw = line.decode("utf-8", errors="replace").strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        resp = {"type": "error", "code": "bad_json", "message": "invalid JSON"}
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                        self._flush_client(conn, conn_id)
                        continue

                    if not isinstance(msg, dict):
                        resp = {"type": "error", "code": "bad_json", "message": "not a JSON object"}
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                        self._flush_client(conn, conn_id)
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "join":
                        resp = self._handle_join(conn_id, msg)
                        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))

                    elif msg_type == "op":
                        resp = self._handle_op(conn_id, msg)
                        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))

                    elif msg_type is None:
                        resp = {"type": "error", "code": "bad_request", "message": "missing type field"}
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

                    else:
                        resp = {"type": "error", "code": "bad_request", "message": f"unknown type: {msg_type}"}
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

                    self._flush_client(conn, conn_id)

        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            with self._docs_lock:
                for doc in self.documents.values():
                    doc.remove_client(conn_id)
            self._conn_names.pop(conn_id, None)

    def run(self):
        self._load_state()

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", self.port))
        server_sock.listen(128)
        server_sock.settimeout(0.5)

        print(f"Livedoc server listening on 127.0.0.1:{self.port}", flush=True)

        self._shutdown = False

        def accept_loop():
            while not self._shutdown:
                try:
                    conn, addr = server_sock.accept()
                    t = threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True)
                    t.start()
                except OSError:
                    if self._shutdown:
                        break
                    continue

        accept_thread = threading.Thread(target=accept_loop, daemon=True)
        accept_thread.start()

        while not self._shutdown:
            try:
                time.sleep(2)
                self._save_state()
            except Exception:
                pass

    def shutdown(self):
        self._shutdown = True
        self._save_state()


def main():
    parser = argparse.ArgumentParser(description="Livedoc server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=str, default="./livedoc_data")
    args = parser.parse_args()

    srv = Server(args.port, args.data_dir)

    def _signal_handler(signum, frame):
        srv.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    srv.run()


if __name__ == "__main__":
    main()
