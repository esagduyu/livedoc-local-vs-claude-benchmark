"""
Livedoc client library and shared transformation primitives.

OT functions are here so both server.py and this client import the same
implementation, guaranteeing character-perfect agreement.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Edit = Dict[str, Any]  # {"ins": int, "text": str}  or  {"del": int, "len": int}
Op = List[Edit]


# ---------------------------------------------------------------------------
# Validation helpers (used by server for bad_request / out_of_range)
# ---------------------------------------------------------------------------

def _is_int(v: Any) -> bool:
    """Return True only for int values that are *not* bools."""
    return isinstance(v, int) and not isinstance(v, bool)


def _is_edit(v: Any) -> bool:
    if not isinstance(v, dict):
        return False
    keys = set(v.keys())
    if keys == {"ins", "text"} and _is_int(v.get("ins")) and isinstance(v.get("text"), str) and len(v.get("text", "")) > 0:
        return True
    if keys == {"del", "len"} and _is_int(v.get("del")) and _is_int(v.get("len")) and v.get("len", 0) >= 1:
        return True
    return False


def validate_op(op: Any) -> bool:
    """Structural check of an operation. Returns True if valid."""
    if not isinstance(op, list):
        return False
    for edit in op:
        if not _is_edit(edit):
            return False
    return True


def apply_op(text: str, op: Op) -> str:
    """Apply edits left-to-right, returning the new text.

    Raises ValueError on out-of-range positions, len < 1, or empty insert.
    """
    chars = list(text)  # work with code points
    for edit in op:
        if "ins" in edit:
            pos = edit["ins"]
            ins_text = edit["text"]
            if not _is_int(pos) or isinstance(pos, bool):
                raise ValueError("bad insert position")
            if pos < 0 or pos > len(chars):
                raise ValueError(f"insert position {pos} out of range [0, {len(chars)}]")
            chars[pos:pos] = list(ins_text)
        elif "del" in edit:
            pos = edit["del"]
            length = edit["len"]
            if not _is_int(pos) or isinstance(pos, bool):
                raise ValueError("bad delete position")
            if not _is_int(length) or isinstance(length, bool):
                raise ValueError("bad delete length")
            if pos < 0 or pos + length > len(chars):
                raise ValueError(f"delete [{pos}, {pos+length}) out of range")
            if length < 1:
                raise ValueError("delete length must be >= 1")
            del chars[pos:pos + length]
    return "".join(chars)


# ---------------------------------------------------------------------------
# Edit-vs-edit transformation  xf_edit(a, b) -> (a', b')
#
# b committed earlier, a committed later.
# a' applies after b, b' applies after a.
# ---------------------------------------------------------------------------

def _xf_edit_ins_ins(a: Edit, b: Edit) -> Tuple[List[Edit], List[Edit]]:
    """a=ins, b=ins. b committed earlier.

    On a tie (p == q), the earlier-committed insert (b) stays to the left,
    meaning a shifts past b (p >= q: shift).
    """
    p = a["ins"]
    q = b["ins"]
    t = a["text"]
    u = b["text"]
    # a' = a moving past b (ins past ins)
    # p < q: unchanged. p >= q: shift by len(u). Tie: a shifts, b stays left.
    if p < q:
        a_prime = [{"ins": p, "text": t}]
    else:  # p >= q
        a_prime = [{"ins": p + len(u), "text": t}]
    # b' = b moving past a (roles reversed)
    # Per spec: moving earlier insert at q past later insert at p:
    #   q <= p: b unchanged. q > p: b shifts by len(t).
    if q <= p:
        b_prime = [{"ins": q, "text": u}]
    else:  # q > p
        b_prime = [{"ins": q + len(t), "text": u}]
    return a_prime, b_prime


def _xf_edit_ins_del(a: Edit, b: Edit) -> Tuple[List[Edit], List[Edit]]:
    """a=ins at p with text t, b=del [q, q+k). b committed earlier."""
    p = a["ins"]
    t = a["text"]
    q = b["del"]
    k = b["len"]
    # a' = a moving past b (ins past del)
    if p <= q:
        a_prime = [{"ins": p, "text": t}]
    elif p >= q + k:
        a_prime = [{"ins": p - k, "text": t}]
    else:
        # p strictly inside [q, q+k)
        a_prime = [{"ins": q, "text": t}]
    # b' = b moving past a (del past ins), roles reversed
    # del at q, len k vs ins at p, text t
    if p <= q:
        # ins before or at start of del range
        b_prime = [{"del": q + len(t), "len": k}]
    elif p >= q + k:
        # ins after del range
        b_prime = [{"del": q, "len": k}]
    else:
        # ins strictly inside del range: split
        b_prime = [
            {"del": q, "len": p - q},
            {"del": q + len(t), "len": q + k - p},
        ]
    return a_prime, b_prime


def _xf_edit_del_ins(a: Edit, b: Edit) -> Tuple[List[Edit], List[Edit]]:
    """a=del [p, p+m), b=ins at q with text u. b committed earlier."""
    p = a["del"]
    m = a["len"]
    q = b["ins"]
    u = b["text"]
    # a' = a moving past b (del past ins)
    if q <= p:
        a_prime = [{"del": p + len(u), "len": m}]
    elif q >= p + m:
        a_prime = [{"del": p, "len": m}]
    else:
        # q strictly inside [p, p+m): split
        a_prime = [
            {"del": p, "len": q - p},
            {"del": p + len(u), "len": p + m - q},
        ]
    # b' = b moving past a (ins past del), roles reversed
    # ins at q, text u vs del at p, len m
    if q <= p:
        b_prime = [{"ins": q, "text": u}]
    elif q >= p + m:
        b_prime = [{"ins": q - m, "text": u}]
    else:
        # q strictly inside [p, p+m)
        b_prime = [{"ins": p, "text": u}]
    return a_prime, b_prime


def _xf_edit_del_del(a: Edit, b: Edit) -> Tuple[List[Edit], List[Edit]]:
    """a=del [p, p+m), b=del [q, q+k). b committed earlier."""
    p = a["del"]
    m = a["len"]
    q = b["del"]
    k = b["len"]
    # a' = a moving past b: what remains of [p, p+m) after [q, q+k) is gone
    a_prime = _del_after_del(start=p, length=m, other_start=q, other_length=k)
    # b' = b moving past a: what remains of [q, q+k) after [p, p+m) is gone
    b_prime = _del_after_del(start=q, length=k, other_start=p, other_length=m)
    return a_prime, b_prime


def _del_after_del(start: int, length: int, other_start: int, other_length: int) -> List[Edit]:
    """What remains of del[start, start+length) after del[other_start, other_start+other_length) was applied first.

    The result operates on the text *after* the other deletion. Returns a single
    del edit (or empty list if nothing remains).
    """
    s_end = start + length
    o_end = other_start + other_length

    # Determine which parts of [start, s_end) survive after [other_start, o_end) is deleted
    before_other = max(0, min(s_end, other_start) - start)
    after_other_start = max(start, o_end)
    after_other_len = max(0, s_end - after_other_start)

    total_surviving = before_other + after_other_len
    if total_surviving < 1:
        return []

    # The surviving parts form one contiguous range in the post-other text
    if before_other > 0 and after_other_len > 0:
        # Both parts survive and become adjacent after other is deleted
        new_start = start  # before part starts at original position
        new_len = total_surviving
    elif before_other > 0:
        # Only before part survives
        new_start = start
        new_len = before_other
    else:
        # Only after part survives: shift left by other_length
        new_start = after_other_start - other_length
        new_len = after_other_len

    return [{"del": new_start, "len": new_len}]


def xf_edit(a: Edit, b: Edit) -> Tuple[List[Edit], List[Edit]]:
    """Transform edit a against edit b (b committed earlier).

    Returns (a', b') where a' applies after b and b' applies after a.
    """
    a_kind = "ins" if "ins" in a else "del"
    b_kind = "ins" if "ins" in b else "del"
    if a_kind == "ins" and b_kind == "ins":
        return _xf_edit_ins_ins(a, b)
    elif a_kind == "ins" and b_kind == "del":
        return _xf_edit_ins_del(a, b)
    elif a_kind == "del" and b_kind == "ins":
        return _xf_edit_del_ins(a, b)
    else:
        return _xf_edit_del_del(a, b)


# ---------------------------------------------------------------------------
# Operation-vs-operation transformation  xf(A, B) -> (A', B')
# ---------------------------------------------------------------------------

def xf(A: Op, B: Op) -> Tuple[Op, Op]:
    """Transform operations A and B.

    Returns (A', B') such that applying B then A' gives the same text as
    applying A then B'.  Empty ops are passed through unchanged.

    Implements the recursive definition from the spec exactly.
    """
    if not A:
        return list(A), list(B)
    if not B:
        return list(A), list(B)
    if len(A) == 1 and len(B) == 1:
        a_list, b_list = xf_edit(A[0], B[0])
        return a_list, b_list
    if len(A) > 1:
        # (a1', B1) = xf([a1], B); (rest', B2) = xf(A[1:], B1)
        a1_list, B1 = xf([A[0]], B)
        rest_list, B2 = xf(A[1:], B1)
        return a1_list + rest_list, B2
    # len(A) == 1 and len(B) > 1
    # (A1, b1') = xf(A, [b1]); (A2, rest') = xf(A1, B[1:])
    A1, b1_prime = xf(A, [B[0]])
    A2, rest_prime = xf(A1, B[1:])
    return A2, b1_prime + rest_prime


# ---------------------------------------------------------------------------
# Client class
# ---------------------------------------------------------------------------

class Client:
    """Livedoc client with optimistic editing and OT."""

    def __init__(
        self,
        host: str,
        port: int,
        doc: str,
        name: str,
        timeout: float = 5.0,
    ) -> None:
        import socket

        self._host = host
        self._port = port
        self._doc = doc
        self._name = name
        self._timeout = timeout

        self._lock = threading.Lock()
        self._text = ""
        self._rev = 0
        self._inflight: Optional[Op] = None
        self._buffer: List[Edit] = []
        self._synced_cv = threading.Condition(self._lock)
        self._joined = False

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(timeout)
        try:
            self._socket.connect((host, port))
        except Exception:
            self._socket.close()
            raise ConnectionError(f"Cannot connect to {host}:{port}")
        self._socket.settimeout(None)

        # Send join
        self._send({"type": "join", "doc": doc, "client": name})

        # Start background reader
        self._reader_stop = threading.Event()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

        # Wait for joined
        with self._lock:
            deadline = time.monotonic() + timeout
            while not self._joined and not self._reader_stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._socket.close()
                    self._reader_stop.set()
                    raise TimeoutError("Timed out waiting for joined")
                self._synced_cv.wait(remaining)
            if not self._joined:
                raise TimeoutError("Timed out waiting for joined")

    def _send(self, msg: Dict[str, Any]) -> None:
        import json
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        self._socket.sendall(data)

    def _reader_loop(self) -> None:
        import json

        buf = b""
        try:
            while not self._reader_stop.is_set():
                data = self._socket.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    self._handle_msg(msg)
        except Exception:
            pass
        with self._lock:
            self._reader_stop.set()
            self._synced_cv.notify_all()

    def _handle_msg(self, msg: Dict[str, Any]) -> None:
        if msg.get("type") == "joined":
            with self._lock:
                self._joined = True
                self._rev = msg["rev"]
                if "text" in msg:
                    self._text = msg["text"]
                elif "ops" in msg:
                    for op_entry in msg["ops"]:
                        self._text = apply_op(self._text, op_entry["op"])
                        self._rev = op_entry["rev"]
                self._synced_cv.notify_all()

        elif msg.get("type") == "ack":
            with self._lock:
                self._rev = msg["rev"]
                old_inflight = self._inflight
                self._inflight = None
                buffer_edits = list(self._buffer)
                self._buffer = []
                self._synced_cv.notify_all()
                # Transform buffer against the acked inflight op
                if old_inflight and buffer_edits:
                    transformed_buffer, _ = xf(buffer_edits, old_inflight)
                else:
                    transformed_buffer = buffer_edits
                if transformed_buffer:
                    self._inflight = transformed_buffer

        elif msg.get("type") == "op" and msg.get("doc") == self._doc:
            with self._lock:
                remote_op = msg["op"]
                # (inflight', r1) = xf(inflight, remote_op)
                if self._inflight:
                    transformed_inflight, transformed_remote = xf(
                        self._inflight, remote_op
                    )
                    self._inflight = transformed_inflight
                else:
                    transformed_remote = remote_op
                # (buffer', r2) = xf(buffer, r1)
                if self._buffer:
                    transformed_buffer, r2 = xf(self._buffer, transformed_remote)
                    self._buffer = transformed_buffer
                else:
                    r2 = transformed_remote
                # Apply r2 to text
                self._text = apply_op(self._text, r2)
                self._rev = msg["rev"]
                self._synced_cv.notify_all()

        elif msg.get("type") == "error":
            pass  # Could raise or log

    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    @property
    def rev(self) -> int:
        with self._lock:
            return self._rev

    def insert(self, pos: int, text: str) -> None:
        if not isinstance(text, str) or len(text) == 0:
            raise ValueError("insert text must be a non-empty string")
        # Validate against local text
        if pos < 0 or pos > len(self._text):
            raise ValueError(
                f"insert position {pos} out of range [0, {len(self._text)}]"
            )
        edit: Edit = {"ins": pos, "text": text}
        with self._lock:
            self._text = apply_op(self._text, [edit])
            self._buffer.append(edit)
            # Try to send
            self._try_send_buffer()

    def delete(self, pos: int, length: int) -> None:
        if not isinstance(length, int) or isinstance(length, bool) or length < 1:
            raise ValueError("delete length must be a positive integer")
        if pos < 0 or pos + length > len(self._text):
            raise ValueError(
                f"delete [{pos}, {pos+length}) out of range [0, {len(self._text)}]"
            )
        edit: Edit = {"del": pos, "len": length}
        with self._lock:
            self._text = apply_op(self._text, [edit])
            self._buffer.append(edit)
            self._try_send_buffer()

    def _try_send_buffer(self) -> None:
        """Send the buffer as an operation if nothing is in flight.
        Must be called with self._lock held.
        """
        if self._inflight is not None:
            return
        if not self._buffer:
            return
        op_to_send = list(self._buffer)
        self._buffer = []
        self._inflight = op_to_send
        import json

        self._send(
            {
                "type": "op",
                "doc": self._doc,
                "rev": self._rev,
                "id": str(uuid.uuid4()),
                "op": op_to_send,
            }
        )

    def wait_synced(self, timeout: float = 5.0) -> bool:
        with self._lock:
            deadline = time.monotonic() + timeout
            while self._inflight is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._synced_cv.wait(remaining)
            return True

    def close(self) -> None:
        self._reader_stop.set()
        with self._lock:
            self._synced_cv.notify_all()
        try:
            self._socket.close()
        except Exception:
            pass
        self._reader.join(timeout=2)
