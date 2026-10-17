"""Livedoc client library, plus the operational-transformation core shared with the server.

An operation is a list of edits applied left to right; each edit is either
``{"ins": P, "text": T}`` or ``{"del": P, "len": N}``.
"""

import json
import socket
import threading
import uuid

__all__ = ["Client", "OpError", "validate_op", "check_op", "apply_op", "move_cursor", "xf", "transform_edit"]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

class OpError(ValueError):
    """An operation is malformed (``code == "bad_request"``) or does not fit the text
    (``code == "out_of_range"``)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_op(op):
    """Check the structure of a wire operation and return it in canonical form.

    Raises OpError("bad_request") for structural problems. Range problems (including
    empty insert text and len < 1) are left to check_op.
    """
    if not isinstance(op, list):
        raise OpError("bad_request", "op must be a list of edits")
    out = []
    for e in op:
        if not isinstance(e, dict):
            raise OpError("bad_request", "each edit must be an object")
        keys = set(e)
        if keys == {"ins", "text"}:
            if not _is_int(e["ins"]) or not isinstance(e["text"], str):
                raise OpError("bad_request", "insert needs integer 'ins' and string 'text'")
            out.append({"ins": e["ins"], "text": e["text"]})
        elif keys == {"del", "len"}:
            if not _is_int(e["del"]) or not _is_int(e["len"]):
                raise OpError("bad_request", "delete needs integer 'del' and 'len'")
            out.append({"del": e["del"], "len": e["len"]})
        else:
            raise OpError("bad_request", "edit must be {ins, text} or {del, len}")
    return out


def check_op(op, length):
    """Check that a structurally valid op applies to a text of `length` code points.

    Returns the length of the resulting text. Raises OpError("out_of_range").
    """
    for e in op:
        if "ins" in e:
            if not e["text"]:
                raise OpError("out_of_range", "empty insert text")
            if not 0 <= e["ins"] <= length:
                raise OpError("out_of_range", "insert position %d outside text of length %d" % (e["ins"], length))
            length += len(e["text"])
        else:
            if e["len"] < 1:
                raise OpError("out_of_range", "delete length must be >= 1")
            if e["del"] < 0 or e["del"] + e["len"] > length:
                raise OpError("out_of_range", "delete [%d, %d) outside text of length %d"
                              % (e["del"], e["del"] + e["len"], length))
            length -= e["len"]
    return length


def apply_op(text, op):
    """Apply a (valid) op to text and return the new text."""
    for e in op:
        if "ins" in e:
            p = e["ins"]
            text = text[:p] + e["text"] + text[p:]
        else:
            p = e["del"]
            text = text[:p] + text[p + e["len"]:]
    return text


def move_cursor(c, op, own):
    """Move cursor position `c` through `op`. `own` is True when the cursor's owner made `op`,
    in which case an insert exactly at the cursor pushes it forward."""
    for e in op:
        if "ins" in e:
            p = e["ins"]
            if p < c or (p == c and own):
                c += len(e["text"])
        elif c > e["del"]:
            c = max(e["del"], c - e["len"])
    return c


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------

def transform_edit(x, y, x_first):
    """Transform edit `x` so it applies after edit `y`; both were written against the same text.

    `x_first` is True when `x` was committed before `y` (it then wins insert ties).
    Returns a list of zero, one or two edits.
    """
    if "ins" in x:
        p = x["ins"]
        if "ins" in y:
            q = y["ins"]
            if p < q or (p == q and x_first):
                return [x]
            return [{"ins": p + len(y["text"]), "text": x["text"]}]
        q, k = y["del"], y["len"]
        if p <= q:
            return [x]
        if p >= q + k:
            return [{"ins": p - k, "text": x["text"]}]
        return [{"ins": q, "text": x["text"]}]

    p, m = x["del"], x["len"]
    if "ins" in y:
        q, n = y["ins"], len(y["text"])
        if q <= p:
            return [{"del": p + n, "len": m}]
        if q >= p + m:
            return [x]
        return [{"del": p, "len": q - p}, {"del": p + n, "len": p + m - q}]
    q, k = y["del"], y["len"]
    overlap = max(0, min(p + m, q + k) - max(p, q))
    remaining = m - overlap
    if remaining == 0:
        return []
    start = p - max(0, min(p, q + k) - q)  # minus the chars y deleted before p
    if start == p and remaining == m:
        return [x]
    return [{"del": start, "len": remaining}]


def xf(A, B):
    """Transform op A against op B, where B was committed earlier and both share a base.

    Returns (A2, B2): A2 applies after B, B2 applies after A, and both orders give the
    same text. This follows the recursive definition in the spec, with its tail
    recursions written as loops so that long operations cannot exhaust the stack.
    """
    if not A or not B:
        return A, B
    if len(A) == 1 and len(B) == 1:
        return transform_edit(A[0], B[0], False), transform_edit(B[0], A[0], True)
    if len(A) > 1:
        out = []
        for a in A:
            a2, B = xf([a], B)
            out += a2
        return out, B
    out = []
    for b in B:
        A, b2 = xf(A, [b])
        out += b2
    return A, out


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Client:
    """A participant editing one document.

    Local edits are applied to `text` immediately. At most one operation is in flight;
    edits made meanwhile are buffered and sent as one operation once the ack arrives.
    Remote operations are transformed past the in-flight and buffered edits.

    `cursors` maps other participants' names to their cursor positions in `text`.
    """

    def __init__(self, host, port, doc, name, timeout=5.0):
        self.doc = doc
        self.name = name
        self._lock = threading.Condition()
        self._send_lock = threading.Lock()
        self._text = ""
        self._rev = 0
        self._inflight = None      # (id, op) awaiting ack
        self._buffer = []          # edits not yet sent, applying after the in-flight op
        self._remote_cursors = {}  # name -> position in the server text at self._rev
        self._cursors = {}         # the same cursors, moved through unacknowledged local edits
        self._cursors_loaded = False
        self._pending_cursor = None  # own cursor (in local text) to send once synced
        self._joined = False
        self._closed = False
        self.error = None          # last error message received from the server, if any

        self._sock = socket.create_connection((host, port), timeout=timeout)
        try:
            self._sock.settimeout(None)
            self._thread = threading.Thread(target=self._reader, name="livedoc-reader-%s" % name, daemon=True)
            self._thread.start()
            self._send({"type": "join", "doc": doc, "client": name})
            self._send({"type": "cursors", "doc": doc})
            with self._lock:
                if not self._lock.wait_for(lambda: (self._joined and self._cursors_loaded) or self._closed, timeout):
                    raise TimeoutError("no 'joined' reply within %.1f seconds" % timeout)
                if not self._joined:
                    raise ConnectionError("connection closed before 'joined': %s" % (self.error,))
        except BaseException:
            self.close()
            raise

    # -- public API ---------------------------------------------------------

    @property
    def text(self):
        with self._lock:
            return self._text

    @property
    def rev(self):
        with self._lock:
            return self._rev

    @property
    def cursors(self):
        with self._lock:
            return dict(self._cursors)

    def state(self):
        """Return (text, rev, cursors) read together, so they are consistent with each other."""
        with self._lock:
            return self._text, self._rev, dict(self._cursors)

    def set_cursor(self, pos):
        """Publish this client's cursor, a position in the local `text`.

        If local edits are unacknowledged, the cursor is kept in step with `text` and sent once
        they are, since only then is it a position at a known server revision.
        """
        with self._lock:
            if not _is_int(pos) or not 0 <= pos <= len(self._text):
                raise ValueError("cursor position outside the text")
            self._pending_cursor = pos
            self._flush_cursor()

    def insert(self, pos, text):
        if not _is_int(pos) or not isinstance(text, str):
            raise ValueError("insert needs an integer position and a string")
        self._edit({"ins": pos, "text": text})

    def delete(self, pos, length):
        if not _is_int(pos) or not _is_int(length):
            raise ValueError("delete needs integer position and length")
        self._edit({"del": pos, "len": length})

    def wait_synced(self, timeout=5.0):
        with self._lock:
            return self._lock.wait_for(
                lambda: self._closed or (self._inflight is None and not self._buffer), timeout
            ) and not self._closed

    def close(self):
        with self._lock:
            self._closed = True
            self._lock.notify_all()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        t = getattr(self, "_thread", None)
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals ----------------------------------------------------------

    def _edit(self, edit):
        with self._lock:
            if self._closed:
                raise ConnectionError("client is closed")
            try:
                check_op([edit], len(self._text))
            except OpError as e:
                raise ValueError(str(e)) from None
            self._text = apply_op(self._text, [edit])
            self._buffer.append(edit)
            if self._pending_cursor is not None:
                self._pending_cursor = move_cursor(self._pending_cursor, [edit], True)
            self._flush()
            self._update_cursors()

    def _update_cursors(self):
        """Recompute the local view of remote cursors. Caller holds the lock."""
        local = (self._inflight[1] if self._inflight else []) + self._buffer
        self._cursors = {name: move_cursor(c, local, False) for name, c in self._remote_cursors.items()}

    def _flush_cursor(self):
        """Send the pending own cursor if everything is acknowledged. Caller holds the lock."""
        if self._pending_cursor is None or self._inflight is not None or self._buffer:
            return
        pos, self._pending_cursor = self._pending_cursor, None
        self._send({"type": "cursor", "doc": self.doc, "rev": self._rev, "pos": pos})

    def _flush(self):
        """Send the buffer if nothing is in flight. Caller holds the lock."""
        if self._inflight is not None or not self._buffer:
            return
        op, self._buffer = self._buffer, []
        op_id = uuid.uuid4().hex
        self._inflight = (op_id, op)
        self._send({"type": "op", "doc": self.doc, "rev": self._rev, "id": op_id, "op": op})

    def _send(self, msg):
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            try:
                self._sock.sendall(data)
            except OSError:
                pass  # the reader thread notices the broken connection

    def _reader(self):
        buf = b""
        try:
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    i = buf.find(b"\n")
                    if i < 0:
                        break
                    line, buf = buf[:i], buf[i + 1:]
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        try:
                            self._handle(msg)
                        except (KeyError, TypeError, ValueError, AttributeError):
                            pass  # malformed message: ignore it rather than stop reading
        except OSError:
            pass
        finally:
            with self._lock:
                self._closed = True
                self._lock.notify_all()

    def _handle(self, msg):
        kind = msg.get("type")
        with self._lock:
            if kind == "error":
                self.error = msg.get("message", msg.get("code"))
                self._lock.notify_all()
                return
            if msg.get("doc") != self.doc:
                return
            if kind == "joined" and not self._joined:
                self._text = msg.get("text", "")
                self._rev = msg["rev"]
                self._joined = True
            elif kind == "ack":
                if self._inflight is not None and msg.get("id") == self._inflight[0]:
                    mine = self._inflight[1]   # as committed: already transformed past every remote op
                    for name, c in self._remote_cursors.items():
                        self._remote_cursors[name] = move_cursor(c, mine, False)
                    self._inflight = None
                    self._rev = msg["rev"]
                    self._flush()
                    self._flush_cursor()
            elif kind == "op" and self._joined:
                remote = msg["op"]
                author = msg.get("client")
                for name, c in self._remote_cursors.items():
                    self._remote_cursors[name] = move_cursor(c, remote, name == author)
                if self._inflight is not None:
                    op_id, mine = self._inflight
                    mine, remote = xf(mine, remote)
                    self._inflight = (op_id, mine)
                if self._buffer:
                    self._buffer, remote = xf(self._buffer, remote)
                self._text = apply_op(self._text, remote)
                self._rev = msg["rev"]
                if self._pending_cursor is not None:
                    self._pending_cursor = move_cursor(self._pending_cursor, remote, False)
            elif kind == "cursors":
                # Positions at the current server revision, which equals self._rev here because
                # the reply is ordered after every broadcast and ack for earlier revisions.
                self._remote_cursors = {n: c for n, c in msg.get("cursors", {}).items() if n != self.name}
                self._cursors_loaded = True
            elif kind == "cursor":
                if msg.get("client") != self.name:
                    self._remote_cursors[msg["client"]] = msg["pos"]
            elif kind == "leave":
                self._remote_cursors.pop(msg.get("client"), None)
            self._update_cursors()
            self._lock.notify_all()
