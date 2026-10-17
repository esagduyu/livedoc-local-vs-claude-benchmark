#!/usr/bin/env python3
"""Livedoc server – real-time collaborative plain-text editing service."""

import argparse
import json
import os
import signal
import socket
import threading
import time
from pathlib import Path


# ─── Persistence ────────────────────────────────────────────────────────────

def _load_state(data_dir: Path) -> dict:
    """Load persisted state from disk, or return empty state."""
    state_file = data_dir / "state.json"
    if state_file.exists():
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(data_dir: Path, state: dict) -> None:
    """Save state to disk (non-durable – no fsync)."""
    state_file = data_dir / "state.json"
    tmp = str(state_file) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, str(state_file))


# ─── Transformation ─────────────────────────────────────────────────────────
#
# The spec defines edit-vs-edit rules for transforming a (later) against b (earlier).
# The result is (a', b') where:
#   - a' applies after b was applied to the original text
#   - b' applies after a was applied to the original text
#   - Both orderings produce the same final text
#
# For the "forward" direction (a' = a transformed past b), we use the table directly.
# For the "reverse" direction (b' = b transformed past a), we apply the same rules
# with roles reversed, keeping the "earlier-committed side keeps priority on ties".
#
# Key: "earlier-committed" = the one that was already applied.
# When moving b' past a (b is earlier, a is later):
#   - For ins vs ins: "moving an earlier insert at q past a later insert at p
#     leaves it unchanged when q <= p and shifts it when q > p"
#   - For del vs ins: "moving an earlier del past a later ins" – use del-vs-ins
#     rules with b as del, a as ins.
#   - For ins vs del: "moving an earlier del past a later ins" – use del-vs-ins
#     rules with b as del, a as ins.
#   - For del vs del: "moving an earlier del past a later del" – use del-vs-del
#     rules with b as del, a as del.
#
# The del-vs-ins table (a=del, b=ins):
#   del [p, p+m) vs ins at q, text u:
#     q <= p: a' = del at p+len(u), same length
#     q >= p+m: a' = unchanged
#     otherwise: split into [del p, len q-p], [del p+len(u), len p+m-q]
#
# The del-vs-del table (a=del, b=del):
#   del [p, p+m) vs del [q, q+k):
#     a deletes only chars b hasn't already deleted – one contiguous range.
#     If nothing remains, result is empty.

def _edit_transform(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """Transform edit a against earlier edit b. Returns (a', b')."""
    a_type = list(a.keys())[0]
    b_type = list(b.keys())[0]
    if a_type == "ins" and b_type == "ins":
        return _xf_ins_ins(a, b)
    elif a_type == "ins" and b_type == "del":
        return _xf_ins_del(a, b)
    elif a_type == "del" and b_type == "ins":
        return _xf_del_ins(a, b)
    else:  # del vs del
        return _xf_del_del(a, b)


def _xf_ins_ins(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """ins at p, text t vs ins at q, text u."""
    p = a["ins"]
    q = b["ins"]
    t = a["text"]
    u = b["text"]
    len_u = len(u)

    # a' (a transformed past b): p < q unchanged, p >= q shifts right
    if p < q:
        a_prime = [a]
    else:
        a_prime = [{"ins": p + len_u, "text": t}]

    # b' (b moved past a): earlier at q past later at p, unchanged when q <= p
    if q <= p:
        b_prime = [b]
    else:
        b_prime = [{"ins": q + len(t), "text": u}]

    return a_prime, b_prime


def _xf_ins_del(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """ins at p, text t vs del [q, q+k)."""
    p = a["ins"]
    t = a["text"]
    q = b["del"]
    k = b["len"]
    len_t = len(t)

    # a' (ins past del): p <= q unchanged, p >= q+k shifts left, inside -> q
    if p <= q:
        a_prime = [a]
    elif p >= q + k:
        a_prime = [{"ins": p - k, "text": t}]
    else:
        a_prime = [{"ins": q, "text": t}]

    # b' (earlier del past later ins): use del-vs-ins rules with b=del, a=ins
    # del [q, q+k) vs ins at p, text t:
    #   p <= q: b' = del at q + len(t), len k  (del is after ins, shift right)
    #   p >= q + k: b' = unchanged  (del is before ins, no effect)
    #   otherwise: split the del
    if p <= q:
        b_prime = [{"del": q + len_t, "len": k}]
    elif p >= q + k:
        b_prime = [b]
    else:
        # ins strictly inside del range – split del, ins text must not be deleted
        b_prime = [
            {"del": q, "len": p - q},
            {"del": p + len_t, "len": q + k - p},
        ]

    return a_prime, b_prime


def _xf_del_ins(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """del [p, p+m) vs ins at q, text u."""
    p = a["del"]
    m = a["len"]
    q = b["ins"]
    u = b["text"]
    len_u = len(u)

    # a' (del past ins): q <= p shifts right, q >= p+m unchanged, inside -> split
    if q <= p:
        a_prime = [{"del": p + len_u, "len": m}]
    elif q >= p + m:
        a_prime = [a]
    else:
        a_prime = [
            {"del": p, "len": q - p},
            {"del": p + len_u, "len": p + m - q},
        ]

    # b' (earlier ins past later del): use ins-vs-del rules with b=ins, a=del
    # ins at q vs del [p, p+m):
    #   q <= p: unchanged
    #   q >= p + m: ins at q - m
    #   otherwise (inside): ins at p
    if q <= p:
        b_prime = [b]
    elif q >= p + m:
        b_prime = [{"ins": q - m, "text": u}]
    else:
        b_prime = [{"ins": p, "text": u}]

    return a_prime, b_prime


def _xf_del_del(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """del [p, p+m) vs del [q, q+k)."""
    p = a["del"]
    m = a["len"]
    q = b["del"]
    k = b["len"]

    # a' (a's del past b's del): a deletes only chars b hasn't deleted
    if q + k <= p or p + m <= q:
        # No overlap
        a_prime = [a]
    else:
        overlap = min(p + m, q + k) - max(p, q)
        remaining = m - overlap
        if remaining > 0:
            a_prime = [{"del": p, "len": remaining}]
        else:
            a_prime = []

    # b' (earlier del past later del): same logic, b deletes only chars a hasn't deleted
    if p + m <= q or q + k <= p:
        b_prime = [b]
    else:
        overlap = min(p + m, q + k) - max(p, q)
        remaining = k - overlap
        if remaining > 0:
            b_prime = [{"del": q, "len": remaining}]
        else:
            b_prime = []

    return a_prime, b_prime


def _op_transform(A: list[dict], B: list[dict]) -> tuple[list[dict], list[dict]]:
    """Transform operation A against earlier-committed operation B.

    Returns (A', B') where A' applies after B and B' applies after A.
    """
    if not A or not B:
        return (list(A), list(B))

    if len(A) == 1 and len(B) == 1:
        a_prime, b_prime = _edit_transform(A[0], B[0])
        return (a_prime, b_prime)

    if len(A) > 1:
        # (a1', B1) = xf([a1], B); (rest', B2) = xf(A[1:], B1); return (a1' + rest', B2)
        a1_prime, B1 = _op_transform([A[0]], B)
        rest_prime, B2 = _op_transform(A[1:], B1)
        return (a1_prime + rest_prime, B2)

    if len(B) > 1:
        # (A1, b1') = xf(A, [b1]); (A2, rest') = xf(A1, B[1:]); return (A2, b1' + rest')
        A1, b1_prime = _op_transform(A, [B[0]])
        A2, rest_prime = _op_transform(A1, B[1:])
        return (A2, b1_prime + rest_prime)

    return (list(A), list(B))


def apply_op(text: str, op: list[dict]) -> str:
    """Apply an operation to text, returning the new text."""
    chars = list(text)
    for edit in op:
        if "ins" in edit:
            pos = edit["ins"]
            ins_text = edit["text"]
            chars = chars[:pos] + list(ins_text) + chars[pos:]
        elif "del" in edit:
            pos = edit["del"]
            length = edit["len"]
            chars = chars[:pos] + chars[pos + length:]
    return "".join(chars)


def validate_op(op: list[dict], text: str) -> str | None:
    """Validate an operation against text. Returns error message or None."""
    if not isinstance(op, list):
        return "op must be a list"
    for edit in op:
        if not isinstance(edit, dict):
            return "each edit must be a dict"
        if "ins" in edit:
            pos = edit["ins"]
            ins_text = edit["text"]
            if not isinstance(pos, int):
                return "ins position must be int"
            if pos < 0 or pos > len(text):
                return f"ins position {pos} out of range [0, {len(text)}]"
            if not isinstance(ins_text, str) or len(ins_text) == 0:
                return "ins text must be a non-empty string"
        elif "del" in edit:
            pos = edit["del"]
            length = edit["len"]
            if not isinstance(pos, int):
                return "del position must be int"
            if not isinstance(length, int):
                return "del length must be int"
            if length < 1:
                return "del length must be >= 1"
            if pos < 0 or pos + length > len(text):
                return f"del [{pos}, {pos+length}) out of range [0, {len(text)}]"
        else:
            return f"unknown edit type: {list(edit.keys())}"
    return None


# ─── Server state ───────────────────────────────────────────────────────────

class ServerState:
    """Shared mutable state for the server."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        raw = _load_state(data_dir)
        self.documents: dict[str, dict] = {}
        self.seen_ops: dict[str, set[tuple[str, str]]] = {}

        for doc_name, doc_data in raw.get("documents", {}).items():
            self.documents[doc_name] = doc_data
            self.seen_ops[doc_name] = set()
            for entry in doc_data.get("seen_ops", []):
                self.seen_ops[doc_name].add((entry["client"], entry["id"]))

        self._lock = threading.Lock()

    def get_doc(self, name: str) -> dict:
        if name not in self.documents:
            self.documents[name] = {
                "text": "",
                "rev": 0,
                "history": [],
                "seen_ops": [],
            }
            self.seen_ops[name] = set()
        return self.documents[name]

    def persist(self):
        """Save state to disk."""
        raw = {"documents": {}}
        for name, doc in self.documents.items():
            doc_copy = dict(doc)
            doc_copy["seen_ops"] = list(self.seen_ops.get(name, set()))
            raw["documents"][name] = doc_copy
        _save_state(self.data_dir, raw)


# ─── Client connection handling ─────────────────────────────────────────────

class Connection:
    """Represents a single TCP client connection."""

    def __init__(self, conn: socket.socket, addr, state: ServerState,
                 broadcast_fn):
        self.conn = conn
        self.addr = addr
        self.state = state
        self.broadcast = broadcast_fn
        self.client_name: str | None = None
        self.joined_docs: set[str] = set()
        self._buffer = b""
        self._lock = threading.Lock()
        self._closed = False

    def run(self):
        """Main loop: read lines, dispatch."""
        try:
            while not self._closed:
                data = self.conn.recv(4096)
                if not data:
                    break
                self._buffer += data
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    self._handle_line(line)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            self._close()

    def _close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.conn.close()
        except OSError:
            pass

    def _handle_line(self, raw_line: bytes):
        line_str = raw_line.decode("utf-8", errors="replace").strip()
        if not line_str:
            return

        try:
            msg = json.loads(line_str)
        except (json.JSONDecodeError, ValueError):
            self._send({"type": "error", "code": "bad_json",
                        "message": "Invalid JSON"})
            return

        if not isinstance(msg, dict):
            self._send({"type": "error", "code": "bad_json",
                        "message": "JSON must be an object"})
            return

        msg_type = msg.get("type")
        if msg_type == "join":
            self._handle_join(msg)
        elif msg_type == "op":
            self._handle_op(msg)
        else:
            self._send({"type": "error", "code": "bad_request",
                        "message": f"Unknown type: {msg_type}"})

    def _handle_join(self, msg: dict):
        doc = msg.get("doc")
        client_name = msg.get("client")

        if not doc or not isinstance(doc, str):
            self._send({"type": "error", "code": "bad_request",
                        "message": "Missing or invalid 'doc'"})
            return
        if not client_name or not isinstance(client_name, str):
            self._send({"type": "error", "code": "bad_request",
                        "message": "Missing or invalid 'client'"})
            return
        if len(doc) > 100:
            self._send({"type": "error", "code": "bad_request",
                        "message": "Document name too long"})
            return

        since = msg.get("since")
        if since is not None:
            if not isinstance(since, int) or since < 0:
                self._send({"type": "error", "code": "bad_rev",
                            "message": "since must be non-negative int"})
                return

        with self.state._lock:
            doc_data = self.state.get_doc(doc)
            current_rev = doc_data["rev"]

            if since is not None and since > current_rev:
                self._send({"type": "error", "code": "bad_rev",
                            "message": f"since {since} > current rev {current_rev}"})
                return

            self.joined_docs.add(doc)
            self.client_name = client_name

            if since is None:
                reply = {
                    "type": "joined",
                    "doc": doc,
                    "rev": current_rev,
                    "text": doc_data["text"],
                }
            else:
                ops = []
                for entry in doc_data["history"]:
                    if entry["rev"] > since:
                        ops.append(entry)
                reply = {
                    "type": "joined",
                    "doc": doc,
                    "rev": current_rev,
                    "ops": ops,
                }

        self._send(reply)

    def _handle_op(self, msg: dict):
        doc = msg.get("doc")
        rev = msg.get("rev")
        op_id = msg.get("id")
        op = msg.get("op")

        if not doc or not isinstance(doc, str):
            self._send({"type": "error", "code": "bad_request",
                        "message": "Missing or invalid 'doc'"})
            return
        if not isinstance(rev, int):
            self._send({"type": "error", "code": "bad_rev",
                        "message": "rev must be an integer"})
            return
        if not isinstance(op_id, str):
            self._send({"type": "error", "code": "bad_request",
                        "message": "id must be a string"})
            return

        # Check if joined
        if doc not in self.joined_docs:
            self._send({"type": "error", "code": "not_joined",
                        "message": f"Not joined to document '{doc}'"})
            return

        client_name = self.client_name

        # Validate rev
        with self.state._lock:
            doc_data = self.state.get_doc(doc)
            current_rev = doc_data["rev"]

        if rev < 0 or rev > current_rev:
            self._send({"type": "error", "code": "bad_rev",
                        "message": f"rev {rev} out of range [0, {current_rev}]"})
            return

        # Validate op type
        if not isinstance(op, list):
            self._send({"type": "error", "code": "bad_request",
                        "message": "op must be a list"})
            return

        # Get text at the base revision and validate
        with self.state._lock:
            doc_data = self.state.get_doc(doc)
            text = ""
            for entry in doc_data["history"]:
                if entry["rev"] > rev:
                    break
                text = apply_op(text, entry["op"])

        error = validate_op(op, text)
        if error:
            self._send({"type": "error", "code": "out_of_range",
                        "message": error, "id": op_id})
            return

        # Check duplicate
        key = (client_name, op_id)
        with self.state._lock:
            if key in self.state.seen_ops.get(doc, set()):
                self._send({"type": "ack", "doc": doc, "id": op_id,
                            "rev": doc_data["rev"]})
                return

            # Transform op against all ops committed after `rev`
            transformed_op = list(op)
            for entry in doc_data["history"]:
                if entry["rev"] <= rev:
                    continue
                transformed_op, _ = _op_transform(transformed_op, entry["op"])

            # Apply transformed op
            new_text = apply_op(text, transformed_op)
            new_rev = current_rev + 1

            # Store
            history_entry = {
                "rev": new_rev,
                "client": client_name,
                "id": op_id,
                "op": transformed_op,
            }
            doc_data["text"] = new_text
            doc_data["rev"] = new_rev
            doc_data["history"].append(history_entry)
            doc_data["seen_ops"].append({"client": client_name, "id": op_id})
            self.state.seen_ops.setdefault(doc, set()).add(key)
            self.state.persist()

        # Send ack to sender
        self._send({"type": "ack", "doc": doc, "id": op_id, "rev": new_rev})

        # Broadcast to other connections joined to this doc
        broadcast_msg = {
            "type": "op",
            "doc": doc,
            "rev": new_rev,
            "client": client_name,
            "id": op_id,
            "op": transformed_op,
        }
        self.broadcast(doc, broadcast_msg, exclude_conn=self)

    def _send(self, msg: dict):
        try:
            line = json.dumps(msg) + "\n"
            self.conn.sendall(line.encode("utf-8"))
        except OSError:
            pass


def broadcast_to_all(all_connections: list[Connection], doc_name: str,
                     msg: dict, exclude_conn: Connection | None = None):
    """Send msg to all connections joined to doc_name."""
    for conn in all_connections:
        if conn is not exclude_conn and doc_name in conn.joined_docs:
            conn._send(msg)


# ─── Main server loop ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Livedoc server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=str, default="livedoc_data")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    state = ServerState(data_dir)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", args.port))
    server_sock.listen(128)
    server_sock.settimeout(0.5)

    all_connections: list[Connection] = []
    all_lock = threading.Lock()

    def accept_loop():
        while True:
            try:
                conn, addr = server_sock.accept()
                conn.settimeout(None)
                client = Connection(conn, addr, state,
                                    lambda doc, msg, exclude_conn=None:
                                        broadcast_to_all(all_connections, doc, msg, exclude_conn))
                with all_lock:
                    all_connections.append(client)
                t = threading.Thread(target=client.run, daemon=True)
                t.start()
            except OSError:
                continue

    acceptor = threading.Thread(target=accept_loop, daemon=True)
    acceptor.start()

    def shutdown(signum, frame):
        with all_lock:
            for conn in all_connections:
                conn._close()
        server_sock.close()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
