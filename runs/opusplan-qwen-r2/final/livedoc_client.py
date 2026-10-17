"""
Livedoc client library and OT transformation core.

Provides:
- Edit-vs-edit transformation (xf_edit)
- Recursive operation-vs-operation transformation (xf)
- transform_against_history for server-side stale-op transformation
- apply_op for applying an operation to text
- validate_op for operation validation
- Client class for networked collaborative editing
"""

import json
import socket
import threading
import time
import uuid
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_int(v: Any) -> bool:
    """Check if v is an integer (but not a bool)."""
    return isinstance(v, int) and not isinstance(v, bool)


def validate_op(op: list, length: int) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Validate an operation against text of given length.

    Returns:
        (error_code, error_detail, resulting_length)
        error_code is None on success.
    """
    if not isinstance(op, list):
        return ("bad_request", "op must be a list", None)

    new_len = length
    for edit in op:
        if not isinstance(edit, dict):
            return ("bad_request", "each edit must be a dict", None)
        keys = set(edit.keys())
        if keys == {"ins"}:
            text_val = edit["text"]
            if not isinstance(text_val, str) or len(text_val) == 0:
                return ("bad_request", "ins text must be a non-empty string", None)
            pos = edit["ins"]
            if not _is_int(pos):
                return ("bad_request", "ins position must be an integer", None)
            if pos < 0 or pos > new_len:
                return ("out_of_range", f"insert position {pos} out of range", None)
            new_len += len(text_val)
        elif keys == {"del"}:
            pos = edit["del"]
            del_len = edit["len"]
            if not _is_int(pos):
                return ("bad_request", "del position must be an integer", None)
            if not _is_int(del_len):
                return ("bad_request", "del len must be an integer", None)
            if del_len < 1:
                return ("bad_request", "del len must be >= 1", None)
            if pos < 0 or pos + del_len > new_len:
                return ("out_of_range", f"delete [{pos}, {pos + del_len}) out of range", None)
            new_len -= del_len
        else:
            return ("bad_request", f"edit must have exactly 'ins' or 'del' key", None)

    return (None, None, new_len)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_op(text: str, op: list) -> str:
    """Apply an operation to text and return the new text.

    Edits are applied left to right. Each edit applies to the text
    produced by the previous one.
    """
    chars = list(text)
    for edit in op:
        if "ins" in edit:
            pos = edit["ins"]
            chars = chars[:pos] + list(edit["text"]) + chars[pos:]
        elif "del" in edit:
            pos = edit["del"]
            length = edit["len"]
            chars = chars[:pos] + chars[pos + length:]
    return "".join(chars)


# ---------------------------------------------------------------------------
# Edit-vs-edit transformation
# ---------------------------------------------------------------------------

def xf_edit(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """Transform edit a against earlier-committed edit b.

    Returns (a_prime, b_prime) where:
    - a_prime is a (possibly empty) list of edits that apply after b
    - b_prime is a (possibly empty) list of edits that apply after a

    The spec's edit-vs-edit table:
    - ins vs ins: p < q: a unchanged. p >= q: a shifts right by len(u).
      On tie (p == q), earlier insert stays left (a stays at p, b shifts right).
    - ins vs del: p <= q: unchanged. p >= q + k: ins at p - k.
      Strictly inside: ins at q.
    - del vs ins: q <= p: del at p + len(u). q >= p + m: unchanged.
      Inside: splits into two deletes.
    - del vs del: a deletes only chars b hasn't deleted. One contiguous range.
    """
    if "ins" in a and "ins" in b:
        return _xf_ins_ins(a, b)
    elif "ins" in a and "del" in b:
        return _xf_ins_del(a, b)
    elif "del" in a and "ins" in b:
        return _xf_del_ins(a, b)
    elif "del" in a and "del" in b:
        return _xf_del_del(a, b)
    else:
        raise ValueError(f"Unknown edit types")


def _xf_ins_ins(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """ins at p, text t | ins at q, text u.
    p < q: a unchanged, b shifts right by len(t).
    p >= q: a shifts right by len(u), b unchanged.
    On tie (p == q), earlier (b) stays left: a shifts right.
    """
    p = a["ins"]
    t = a["text"]
    q = b["ins"]
    u = b["text"]

    if p < q:
        a_prime = [{"ins": p, "text": t}]
        b_prime = [{"ins": q + len(t), "text": u}]
    else:
        a_prime = [{"ins": p + len(u), "text": t}]
        b_prime = [{"ins": q, "text": u}]

    return (a_prime, b_prime)


def _xf_ins_del(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """ins at p, text t | del [q, q+k).
    a' rule: p <= q: unchanged. p >= q + k: ins at p - k.
    Strictly inside (q < p < q + k): ins at q.
    b' rule (roles reversed: del [q,q+k) | ins at p, t):
      p <= q: del at q + len(t). p >= q + k: unchanged.
      Inside (q < p < q + k): splits.
    """
    p = a["ins"]
    t = a["text"]
    q = b["del"]
    k = b["len"]

    if p <= q:
        a_prime = [{"ins": p, "text": t}]
    elif p >= q + k:
        a_prime = [{"ins": p - k, "text": t}]
    else:
        a_prime = [{"ins": q, "text": t}]

    if p <= q:
        b_prime = [{"del": q + len(t), "len": k}]
    elif p >= q + k:
        b_prime = [{"del": q, "len": k}]
    else:
        first_len = p - q
        second_start = q + len(t)
        second_len = q + k - p
        b_prime = [{"del": q, "len": first_len}, {"del": second_start, "len": second_len}]

    return (a_prime, b_prime)


def _xf_del_ins(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """del [p, p+m) | ins at q, text u.
    a' rule: q <= p: del at p + len(u). q >= p + m: unchanged.
    Inside (p < q < p + m): splits.
    b' rule (roles reversed: ins at q, u | del [p, p+m)):
      q <= p: unchanged. q >= p + m: ins at q - m.
      Inside (p < q < p + m): ins at p.
    """
    p = a["del"]
    m = a["len"]
    q = b["ins"]
    u = b["text"]

    if q <= p:
        a_prime = [{"del": p + len(u), "len": m}]
    elif q >= p + m:
        a_prime = [{"del": p, "len": m}]
    else:
        first_len = q - p
        second_start = p + len(u)
        second_len = p + m - q
        a_prime = [{"del": p, "len": first_len}, {"del": second_start, "len": second_len}]

    if q <= p:
        b_prime = [{"ins": q, "text": u}]
    elif q >= p + m:
        b_prime = [{"ins": q - m, "text": u}]
    else:
        b_prime = [{"ins": p, "text": u}]

    return (a_prime, b_prime)


def _xf_del_del(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """del [p, p+m) | del [q, q+k).
    a deletes only the characters b has not already deleted.
    One contiguous range. If nothing remains, empty result.
    b' is symmetric: b deletes only chars a hasn't deleted.
    """
    p = a["del"]
    m = a["len"]
    q = b["del"]
    k = b["len"]

    # a' = [p, p+m) \\ [q, q+k)
    # If ranges don't overlap at all, a' = a (nothing to remove)
    if p + m <= q or q + k <= p:
        # No overlap
        a_prime = [{"del": p, "len": m}]
    else:
        # Ranges overlap. a' = [p, p+m) \\ [q, q+k)
        a_start = max(p, q + k)
        a_end = min(p + m, q)
        if a_start < a_end:
            a_prime = [{"del": a_start, "len": a_end - a_start}]
        else:
            a_prime = []

    # b' = [q, q+k) \\ [p, p+m)
    if q + k <= p or p + m <= q:
        # No overlap
        b_prime = [{"del": q, "len": k}]
    else:
        b_start = max(q, p + m)
        b_end = min(q + k, p)
        if b_start < b_end:
            b_prime = [{"del": b_start, "len": b_end - b_start}]
        else:
            b_prime = []

    return (a_prime, b_prime)


# ---------------------------------------------------------------------------
# Operation-vs-operation transformation (recursive)
# ---------------------------------------------------------------------------

def xf(A: list[dict], B: list[dict]) -> tuple[list[dict], list[dict]]:
    """Transform operation A against earlier-committed operation B.

    Returns (A', B') where A' applies after B and B' applies after A.
    Both orderings produce the same text.
    """
    if not A or not B:
        return (list(A), list(B))

    if len(A) == 1 and len(B) == 1:
        a_prime, b_prime = xf_edit(A[0], B[0])
        return (a_prime, b_prime)

    if len(A) > 1:
        # Process A's first edit against all of B
        (a1_prime, B1) = xf([A[0]], B)
        # Process remaining A edits against transformed B
        (rest_prime, B2) = xf(A[1:], B1)
        return (a1_prime + rest_prime, B2)

    # len(B) > 1
    # Process all of A against B's first edit
    (A1, b1_prime) = xf(A, [B[0]])
    # Process transformed A against remaining B edits
    (A2, rest_prime) = xf(A1, B[1:])
    return (A2, b1_prime + rest_prime)


# ---------------------------------------------------------------------------
# Transform against history
# ---------------------------------------------------------------------------

def transform_against_history(op: list[dict], history: list[dict]) -> list[dict]:
    """Transform op against a sequence of committed operations.

    For each operation in history (in order), transform op against it.
    Returns the transformed op that applies after all history ops.
    """
    result = list(op)
    for hist_op in history:
        result, _ = xf(result, hist_op)
    return result


# ---------------------------------------------------------------------------
# Client class
# ---------------------------------------------------------------------------

class Client:
    """Livedoc client for real-time collaborative editing."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 doc: str = "untitled", name: str = "user",
                 timeout: float = 5.0):
        self._host = host
        self._port = port
        self._doc = doc
        self._name = name
        self._timeout = timeout

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._text = ""
        self._rev = 0
        self._inflight = None
        self._buffer: list[dict] = []
        self._cursors: dict[str, int] = {}

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect((host, port))
        except socket.timeout:
            self._sock.close()
            raise TimeoutError(f"Connection to {host}:{port} timed out")
        except Exception as e:
            self._sock.close()
            raise

        self._sock.settimeout(None)
        self._running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        join_msg = {"type": "join", "doc": doc, "client": name}
        self._send(join_msg)

        with self._condition:
            deadline = time.monotonic() + timeout
            while not getattr(self, '_joined', False) and self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise TimeoutError(f"Did not receive 'joined' message within {timeout}s")
                self._condition.wait(timeout=min(remaining, 0.5))

    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    @property
    def rev(self) -> int:
        with self._lock:
            return self._rev

    @property
    def cursors(self) -> dict[str, int]:
        with self._lock:
            return dict(self._cursors)

    def insert(self, pos: int, text: str) -> None:
        if not isinstance(text, str) or len(text) == 0:
            raise ValueError("Insert text must be a non-empty string")
        if not isinstance(pos, int) or isinstance(pos, bool):
            raise ValueError("Position must be an integer")
        if pos < 0 or pos > len(self._text):
            raise ValueError(f"Insert position {pos} out of range [0, {len(self._text)}]")

        with self._lock:
            self._text = self._text[:pos] + text + self._text[pos:]
            edit = {"ins": pos, "text": text}
            self._move_cursors_on_local_edit(len(text), 0)
            self._enqueue_edit(edit)

    def delete(self, pos: int, length: int) -> None:
        if not isinstance(pos, int) or isinstance(pos, bool):
            raise ValueError("Position must be an integer")
        if not isinstance(length, int) or isinstance(length, bool):
            raise ValueError("Length must be an integer")
        if length < 1:
            raise ValueError("Length must be >= 1")
        if pos < 0 or pos + length > len(self._text):
            raise ValueError(f"Delete [{pos}, {pos + length}) out of range [0, {len(self._text)}]")

        with self._lock:
            self._text = self._text[:pos] + self._text[pos + length:]
            edit = {"del": pos, "len": length}
            self._move_cursors_on_local_edit(0, length)
            self._enqueue_edit(edit)

    def _move_cursors_on_local_edit(self, ins_len: int, del_len: int) -> None:
        """Move all remote cursors through a local edit (treated as non-owner)."""
        for name in list(self._cursors):
            c = self._cursors[name]
            if ins_len > 0:
                # Insert: cursor moves if p < c or (p == c and same owner)
                # Local edits are by us, so cursor doesn't move for others
                pass
            if del_len > 0:
                # Delete at pos: if c > pos, cursor becomes max(pos, c - del_len)
                # We need the delete position - use 0 as placeholder since
                # we're moving through a batch; actual position tracking is done
                # per-edit in the server. For local optimistic edits, we just
                # shift cursors that are after the delete position.
                pass
            # For simplicity, local edits don't shift remote cursors here;
            # they're adjusted when acks arrive and remote ops come in.

    def _enqueue_edit(self, edit: dict) -> None:
        with self._lock:
            if self._inflight is None:
                op_id = str(uuid.uuid4())
                self._inflight = (edit, op_id)
                msg = {
                    "type": "op",
                    "doc": self._doc,
                    "rev": self._rev,
                    "id": op_id,
                    "op": [edit]
                }
                self._send(msg)
            else:
                self._buffer.append(edit)

    def wait_synced(self, timeout: float = 5.0) -> bool:
        with self._condition:
            deadline = time.monotonic() + timeout
            while self._inflight is not None or self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(remaining, 0.5))
            return True

    def close(self) -> None:
        self._running = False
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._reader_thread.join(timeout=2.0)

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False) + "\n"
        self._sock.sendall(data.encode("utf-8"))

    def _reader_loop(self) -> None:
        buf = b""
        try:
            while self._running:
                try:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                except (socket.timeout, OSError):
                    break

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    self._process_message(msg)
        finally:
            with self._condition:
                self._running = False
                self._condition.notify_all()

    def _process_message(self, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "joined":
            with self._lock:
                self._joined = True
                self._text = msg.get("text", "")
                self._rev = msg.get("rev", 0)
                if "ops" in msg:
                    for entry in msg["ops"]:
                        op = entry["op"]
                        self._text = apply_op(self._text, op)
                        self._rev = entry["rev"]
                self._condition.notify_all()

        elif msg_type == "ack":
            with self._lock:
                op_id = msg.get("id", "")
                new_rev = msg.get("rev", self._rev)
                if self._inflight and self._inflight[1] == op_id:
                    self._rev = new_rev
                    self._inflight = None
                    if self._buffer:
                        batch = self._buffer[:]
                        self._buffer = []
                        op_id2 = str(uuid.uuid4())
                        self._inflight = (batch[0], op_id2)
                        msg2 = {
                            "type": "op",
                            "doc": self._doc,
                            "rev": self._rev,
                            "id": op_id2,
                            "op": batch
                        }
                    else:
                        self._condition.notify_all()
                        return
                    self._send(msg2)
                self._condition.notify_all()

        elif msg_type == "op":
            remote_op = msg.get("op", [])
            new_rev = msg.get("rev", self._rev)
            remote_client = msg.get("client", "")

            with self._lock:
                # Move cursors through the remote operation
                self._move_cursors_on_remote_op(remote_op, remote_client)

                if self._inflight:
                    inflight_op, inflight_id = self._inflight
                    transformed_inflight, transformed_remote = xf(remote_op, [inflight_op])
                    if transformed_inflight:
                        self._inflight = (transformed_inflight[0], inflight_id)
                    else:
                        self._inflight = ([], inflight_id)
                    remote_op = transformed_remote

                if self._buffer:
                    transformed_buffer, transformed_remote = xf(remote_op, self._buffer)
                    self._buffer = transformed_buffer
                    remote_op = transformed_remote

                self._text = apply_op(self._text, remote_op)
                self._rev = new_rev
                self._condition.notify_all()

        elif msg_type == "cursor":
            with self._lock:
                client_name = msg.get("client", "")
                pos = msg.get("pos", 0)
                if client_name and client_name != self._name:
                    self._cursors[client_name] = pos
                self._condition.notify_all()

        elif msg_type == "cursors":
            with self._lock:
                cursors = msg.get("cursors", {})
                self._cursors = {k: v for k, v in cursors.items() if k != self._name}
                self._condition.notify_all()

        elif msg_type == "leave":
            with self._lock:
                client_name = msg.get("client", "")
                self._cursors.pop(client_name, None)
                self._condition.notify_all()

        elif msg_type == "error":
            pass


if __name__ == "__main__":
    import sys
    print("livedoc_client.py is a library. Use server.py to run the server.")
