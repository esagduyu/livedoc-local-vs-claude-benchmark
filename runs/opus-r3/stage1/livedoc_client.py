"""Livedoc client library.

This module also holds the operational-transformation (OT) core, which the
server imports, so that client and server share one implementation of the
transformation rules.

An *edit* is a dict ``{"ins": P, "text": T}`` or ``{"del": P, "len": N}``.
An *operation* is a list of edits applied left to right.
"""

import json
import socket
import threading
import uuid

__all__ = ["Client", "transform", "apply_op", "check_op", "OpError"]


# --------------------------------------------------------------------------
# Operational transformation
# --------------------------------------------------------------------------

class OpError(ValueError):
    """An operation is malformed (``kind == "bad_request"``) or does not fit
    the text it is applied to (``kind == "out_of_range"``)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_op(op, length):
    """Validate `op` against a text of length `length`.

    Returns a normalized copy of the operation (fresh dicts holding only the
    edit keys) and the length of the text after applying it. Raises OpError.
    """
    if not isinstance(op, list):
        raise OpError("bad_request", "op must be a list of edits")
    out = []
    for e in op:
        if not isinstance(e, dict):
            raise OpError("bad_request", "edit must be an object")
        keys = set(e)
        if keys == {"ins", "text"}:
            p, t = e["ins"], e["text"]
            if not _is_int(p) or not isinstance(t, str):
                raise OpError("bad_request", "ins needs integer 'ins' and string 'text'")
            if t == "":
                raise OpError("out_of_range", "insert text must be non-empty")
            if p < 0 or p > length:
                raise OpError("out_of_range", f"insert at {p} outside text of length {length}")
            length += len(t)
            out.append({"ins": p, "text": t})
        elif keys == {"del", "len"}:
            p, n = e["del"], e["len"]
            if not _is_int(p) or not _is_int(n):
                raise OpError("bad_request", "del needs integer 'del' and 'len'")
            if n < 1:
                raise OpError("out_of_range", "delete length must be >= 1")
            if p < 0 or p + n > length:
                raise OpError("out_of_range", f"delete [{p}, {p + n}) outside text of length {length}")
            length -= n
            out.append({"del": p, "len": n})
        else:
            raise OpError("bad_request", "edit must be {ins, text} or {del, len}")
    return out, length


def apply_op(text, op):
    """Apply a (valid) operation to `text` and return the new text."""
    if not op:
        return text
    if len(op) == 1:
        e = op[0]
        if "ins" in e:
            p = e["ins"]
            return text[:p] + e["text"] + text[p:]
        p = e["del"]
        return text[:p] + text[p + e["len"]:]
    parts = list(text)  # many edits: avoid quadratic string rebuilding
    for e in op:
        p = e["ins"] if "ins" in e else e["del"]
        if "ins" in e:
            parts[p:p] = e["text"]
        else:
            del parts[p:p + e["len"]]
    return "".join(parts)


def _ins_past_del(p, t, q, k):
    """Insert at p, moved past a delete of [q, q+k)."""
    if p <= q:
        return [{"ins": p, "text": t}]
    if p >= q + k:
        return [{"ins": p - k, "text": t}]
    return [{"ins": q, "text": t}]


def _del_past_ins(p, m, q, u):
    """Delete of [p, p+m), moved past an insert of u at q."""
    if q <= p:
        return [{"del": p + len(u), "len": m}]
    if q >= p + m:
        return [{"del": p, "len": m}]
    return [{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]


def _del_past_del(p, m, q, k):
    """Delete of [p, p+m), moved past a delete of [q, q+k)."""
    overlap = max(0, min(p + m, q + k) - max(p, q))
    n = m - overlap
    if n == 0:
        return []
    return [{"del": p if p <= q else max(q, p - k), "len": n}]


def transform_edit(a, b):
    """Edit-vs-edit transform. `b` was committed earlier; both share a base.

    Returns (a', b'): a' applies after b, b' applies after a. Each is a list
    of zero, one or two edits.
    """
    if "ins" in a:
        p, t = a["ins"], a["text"]
        if "ins" in b:
            q, u = b["ins"], b["text"]
            # The earlier-committed insert (b) wins ties and stays left.
            a2 = [{"ins": p, "text": t}] if p < q else [{"ins": p + len(u), "text": t}]
            b2 = [{"ins": q, "text": u}] if q <= p else [{"ins": q + len(t), "text": u}]
            return a2, b2
        q, k = b["del"], b["len"]
        return _ins_past_del(p, t, q, k), _del_past_ins(q, k, p, t)
    p, m = a["del"], a["len"]
    if "ins" in b:
        q, u = b["ins"], b["text"]
        return _del_past_ins(p, m, q, u), _ins_past_del(q, u, p, m)
    q, k = b["del"], b["len"]
    return _del_past_del(p, m, q, k), _del_past_del(q, k, p, m)


def transform(A, B):
    """xf(A, B) -> (A', B') where B was committed before A, same base.

    A' applies after B and B' applies after A. This is the recursive
    definition from the spec with its tail recursion unrolled into loops.
    """
    if not A or not B:
        return list(A), list(B)
    if len(A) > 1:
        out = []
        cur = B
        for a in A:
            a2, cur = transform([a], cur)
            out.extend(a2)
        return out, cur
    if len(B) == 1:
        return transform_edit(A[0], B[0])
    b_out = []
    cur = A
    for i, b in enumerate(B):
        if len(cur) != 1:
            a2, rest = transform(cur, B[i:])
            b_out.extend(rest)
            return a2, b_out
        cur, b2 = transform_edit(cur[0], b)
        b_out.extend(b2)
    return cur, b_out


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class Client:
    """A participant editing one document.

    State follows the classic OT client model: at most one operation is in
    flight (sent, not yet acknowledged); edits made meanwhile accumulate in a
    buffer that is sent as a single operation once the ack arrives.
    """

    def __init__(self, host, port, doc, name, timeout=5.0):
        self.doc = doc
        self.name = name
        self.text = ""
        self.rev = 0
        self.errors = []  # error messages received from the server
        self._inflight = None  # (id, op) or None
        self._buffer = []  # edits not yet sent
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._joined = threading.Event()
        self._join_error = None

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, name=f"livedoc-{name}", daemon=True)
        self._reader.start()
        self._send({"type": "join", "doc": doc, "client": name})
        if not self._joined.wait(timeout):
            self.close()
            raise TimeoutError(f"did not join {doc!r} within {timeout} seconds")
        if self._join_error is not None:
            self.close()
            raise ConnectionError(f"join rejected: {self._join_error.get('message')}")

    # -- public API ---------------------------------------------------------

    def insert(self, pos, text):
        if not isinstance(text, str) or text == "":
            raise ValueError("insert text must be a non-empty string")
        self._edit({"ins": pos, "text": text})

    def delete(self, pos, length):
        self._edit({"del": pos, "len": length})

    def wait_synced(self, timeout=5.0):
        with self._cond:
            return self._cond.wait_for(
                lambda: self._inflight is None and not self._buffer, timeout)

    def close(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals ----------------------------------------------------------

    def _edit(self, edit):
        with self._cond:
            try:
                (edit,), _ = check_op([edit], len(self.text))
            except OpError as e:
                raise ValueError(str(e)) from None
            self.text = apply_op(self.text, [edit])
            self._buffer.append(edit)
            if self._inflight is None:
                self._flush()

    def _flush(self):
        """Send the buffer as one operation. Caller holds self._cond."""
        op_id = uuid.uuid4().hex
        self._inflight = (op_id, self._buffer)
        self._buffer = []
        self._send({"type": "op", "doc": self.doc, "rev": self.rev,
                    "id": op_id, "op": self._inflight[1]})

    def _send(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self._send_lock:
            self._sock.sendall(data)

    def _read_loop(self):
        f = self._sock.makefile("rb")
        try:
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._handle(msg)
        except OSError:
            pass
        finally:
            with self._cond:
                self._cond.notify_all()

    def _handle(self, msg):
        kind = msg.get("type")
        if kind == "error":
            with self._cond:
                self.errors.append(msg)
                if not self._joined.is_set() and "id" not in msg:
                    self._join_error = msg
                    self._joined.set()
            return
        if msg.get("doc") != self.doc:
            return
        with self._cond:
            if kind == "joined":
                if not self._joined.is_set():
                    self.text = msg.get("text", "")
                    self.rev = msg["rev"]
                    self._joined.set()
            elif kind == "ack":
                if self._inflight is None or msg.get("id") != self._inflight[0]:
                    return
                self.rev = msg["rev"]
                self._inflight = None
                if self._buffer:
                    self._flush()
                self._cond.notify_all()
            elif kind == "op":
                # The server committed this before our pending edits, so it
                # has priority: move it past the in-flight op and the buffer.
                remote = msg["op"]
                if self._inflight is not None:
                    op_id, pending = self._inflight
                    pending, remote = transform(pending, remote)
                    self._inflight = (op_id, pending)
                if self._buffer:
                    self._buffer, remote = transform(self._buffer, remote)
                self.text = apply_op(self.text, remote)
                self.rev = msg["rev"]
