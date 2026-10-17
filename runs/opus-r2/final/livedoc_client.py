"""Livedoc client library and the shared operational-transformation core.

The transformation functions in this module (`transform`, `apply_op`) are also
used by the server, so client and server agree on every result by construction.
"""

import collections
import json
import queue
import socket
import threading
import time
import uuid

__all__ = ["Client", "transform", "apply_op", "transform_edit", "transform_cursor"]


# ---------------------------------------------------------------------------
# Operational transformation
# ---------------------------------------------------------------------------

def transform_edit(x, y, x_wins_tie):
    """Transform edit `x` past edit `y`; both were written against the same text.

    Returns the list (0, 1 or 2 edits) that has the effect of `x` when applied
    after `y`. `x_wins_tie` decides which insert stays left when both insert at
    the same position: True means `x` stays left (i.e. `x` was committed earlier).
    """
    if "ins" in x:
        p, t = x["ins"], x["text"]
        if "ins" in y:
            q = y["ins"]
            if p < q or (p == q and x_wins_tie):
                return [{"ins": p, "text": t}]
            return [{"ins": p + len(y["text"]), "text": t}]
        q, k = y["del"], y["len"]
        if p <= q:
            return [{"ins": p, "text": t}]
        if p >= q + k:
            return [{"ins": p - k, "text": t}]
        return [{"ins": q, "text": t}]

    p, m = x["del"], x["len"]
    if "ins" in y:
        q, n = y["ins"], len(y["text"])
        if q <= p:
            return [{"del": p + n, "len": m}]
        if q >= p + m:
            return [{"del": p, "len": m}]
        return [{"del": p, "len": q - p}, {"del": p + n, "len": p + m - q}]

    q, k = y["del"], y["len"]
    overlap = max(0, min(p + m, q + k) - max(p, q))
    remaining = m - overlap
    if remaining == 0:
        return []
    if p <= q:
        start = p
    elif p >= q + k:
        start = p - k
    else:
        start = q
    return [{"del": start, "len": remaining}]


def transform(a, b):
    """xf(A, B) -> (A', B').

    `b` was committed earlier than `a`; both apply to the same text. `A'` applies
    after `B` and `B'` applies after `A`, and both orders give the same text.
    """
    if not a or not b:
        return list(a), list(b)
    if len(a) == 1 and len(b) == 1:
        return (transform_edit(a[0], b[0], False),
                transform_edit(b[0], a[0], True))
    if len(a) > 1:
        out = []
        for edit in a:
            edit_t, b = transform([edit], b)
            out.extend(edit_t)
        return out, b
    out_b = []
    for edit in b:
        a, edit_t = transform(a, [edit])
        out_b.extend(edit_t)
    return a, out_b


def transform_cursor(c, op, own):
    """Move cursor position `c` through operation `op`.

    `own` is True when `op` was made by the cursor's owner: their own insert
    exactly at the cursor pushes it forward, anyone else's leaves it in place.
    """
    for edit in op:
        if "ins" in edit:
            p = edit["ins"]
            if p < c or (p == c and own):
                c += len(edit["text"])
        else:
            p, n = edit["del"], edit["len"]
            if c > p:
                c = max(p, c - n)
    return c


def apply_op(text, op):
    """Apply operation `op` to `text`; raise ValueError if any edit is out of range."""
    for edit in op:
        if "ins" in edit:
            p, t = edit["ins"], edit["text"]
            if not t or p < 0 or p > len(text):
                raise ValueError(f"insert out of range: {edit!r}")
            text = text[:p] + t + text[p:]
        else:
            p, n = edit["del"], edit["len"]
            if n < 1 or p < 0 or p + n > len(text):
                raise ValueError(f"delete out of range: {edit!r}")
            text = text[:p] + text[p + n:]
    return text


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Client:
    """A participant editing one document.

    State (guarded by `self._cond`):
      _rev       last server revision incorporated
      _text      server text at _rev + in-flight op + buffered edits
      _inflight  the single unacknowledged op sent to the server (or None)
      _buffer    edits made after _inflight was sent, not yet sent
      _remote_cursors  other clients' cursors at server revision _rev
      _cursors   the same cursors moved through _inflight and _buffer, i.e.
                 positions in _text; recomputed whenever _text changes
      _my_cursor own cursor (position in _text) waiting to be sent, or None
    """

    def __init__(self, host, port, doc, name, timeout=5.0):
        self.doc = doc
        self.name = name
        self.errors = []  # error messages received from the server
        self._cond = threading.Condition()
        self._outq = queue.Queue()
        self._text = ""
        self._rev = 0
        self._inflight = None
        self._inflight_id = None
        self._buffer = []
        self._remote_cursors = {}
        self._cursors = {}
        self._my_cursor = None
        self._snapshots = collections.deque()  # pending snapshot requests, in order
        self._joined = False
        self._join_error = None
        self._closed = False

        deadline = time.monotonic() + timeout
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(None)
        self._thread = threading.Thread(target=self._reader, name=f"livedoc-{name}",
                                        daemon=True)
        self._writer = threading.Thread(target=self._write_loop,
                                        name=f"livedoc-{name}-w", daemon=True)
        self._thread.start()
        self._writer.start()
        self._send({"type": "join", "doc": doc, "client": name})

        with self._cond:
            self._cond.wait_for(
                lambda: self._joined or self._join_error or self._closed,
                max(0.0, deadline - time.monotonic()))
            joined, error = self._joined, self._join_error
        if not joined:
            self.close()
            if error:
                raise ConnectionError(f"join failed: {error}")
            raise TimeoutError(f"did not join {doc!r} within {timeout} seconds")

    # -- public state ------------------------------------------------------

    @property
    def text(self):
        with self._cond:
            return self._text

    @property
    def rev(self):
        with self._cond:
            return self._rev

    @property
    def cursors(self):
        """Other clients' cursors, as positions in the current `text`."""
        with self._cond:
            return dict(self._cursors)

    # -- editing -----------------------------------------------------------

    def insert(self, pos, text):
        if not isinstance(text, str) or not text:
            raise ValueError("insert text must be a non-empty string")
        if not isinstance(pos, int) or isinstance(pos, bool):
            raise ValueError("position must be an integer")
        with self._cond:
            if not 0 <= pos <= len(self._text):
                raise ValueError(f"insert position {pos} outside text of length {len(self._text)}")
            self._local_edit({"ins": pos, "text": text})

    def delete(self, pos, length):
        for v in (pos, length):
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("position and length must be integers")
        with self._cond:
            if length < 1 or pos < 0 or pos + length > len(self._text):
                raise ValueError(f"delete [{pos}, {pos + length}) outside text of length {len(self._text)}")
            self._local_edit({"del": pos, "len": length})

    def set_cursor(self, pos):
        """Publish this client's cursor, a position in the current `text`.

        The server needs a position at a known revision, so the cursor is sent
        once every local edit is acknowledged. Until then it moves along with
        local and remote edits.
        """
        if not isinstance(pos, int) or isinstance(pos, bool):
            raise ValueError("position must be an integer")
        with self._cond:
            if not 0 <= pos <= len(self._text):
                raise ValueError(f"cursor position {pos} outside text of length {len(self._text)}")
            self._my_cursor = pos
            self._send_cursor_if_synced()

    def snapshot(self, rev, timeout=5.0):
        """Return this document's text at revision `rev` (raises on error or timeout)."""
        slot = {"id": uuid.uuid4().hex, "done": False}
        with self._cond:
            self._snapshots.append(slot)
            self._send({"type": "snapshot", "doc": self.doc, "rev": rev, "id": slot["id"]})
            self._cond.wait_for(lambda: slot["done"] or self._closed, timeout)
            if slot in self._snapshots:
                self._snapshots.remove(slot)
        if not slot["done"]:
            raise TimeoutError("no snapshot reply")
        if "error" in slot:
            err = slot["error"]
            raise LookupError(f"{err.get('code')}: {err.get('message')}")
        return slot["text"]

    def wait_synced(self, timeout=5.0):
        with self._cond:
            self._cond.wait_for(lambda: self._synced() or self._closed, timeout)
            return self._synced()

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._outq.put(None)
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        for t in (self._thread, self._writer):
            if t is not threading.current_thread():
                t.join(timeout=1.0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals (call with self._cond held) ------------------------------

    def _synced(self):
        return self._inflight is None and not self._buffer

    def _local_edit(self, edit):
        self._text = apply_op(self._text, [edit])
        self._buffer.append(edit)
        if self._my_cursor is not None:
            self._my_cursor = transform_cursor(self._my_cursor, [edit], True)
        self._update_cursors()
        self._flush()

    def _update_cursors(self):
        """Recompute `_cursors` from `_remote_cursors`; call whenever _text changes."""
        local = (self._inflight or []) + self._buffer
        self._cursors = {name: transform_cursor(pos, local, False)
                         for name, pos in self._remote_cursors.items()}

    def _send_cursor_if_synced(self):
        if self._my_cursor is not None and self._synced() and not self._closed:
            # Synced: _text is exactly the server text at _rev.
            self._send({"type": "cursor", "doc": self.doc, "rev": self._rev,
                        "pos": self._my_cursor})
            self._my_cursor = None

    def _flush(self):
        """Send the buffered edits if nothing is in flight."""
        if self._inflight is not None or not self._buffer or self._closed:
            return
        self._inflight, self._buffer = self._buffer, []
        self._inflight_id = uuid.uuid4().hex
        self._send({"type": "op", "doc": self.doc, "rev": self._rev,
                    "id": self._inflight_id, "op": self._inflight})

    def _send(self, msg):
        # Sending happens on the writer thread so that a full socket buffer can
        # never block a thread holding the state lock.
        self._outq.put((json.dumps(msg) + "\n").encode("utf-8"))

    def _write_loop(self):
        while True:
            data = self._outq.get()
            if data is None:
                return
            try:
                self._sock.sendall(data)
            except OSError:
                with self._cond:
                    self._closed = True
                    self._cond.notify_all()
                return

    def _on_remote_op(self, msg):
        rev, op = msg["rev"], msg["op"]
        if rev <= self._rev:
            return  # already incorporated
        author = msg.get("client")
        for name, pos in self._remote_cursors.items():
            self._remote_cursors[name] = transform_cursor(pos, op, author == name)
        # The server's op was committed before our in-flight and buffered edits.
        if self._inflight is not None:
            self._inflight, op = transform(self._inflight, op)
        if self._buffer:
            self._buffer, op = transform(self._buffer, op)
        self._text = apply_op(self._text, op)
        self._rev = rev
        if self._my_cursor is not None:
            self._my_cursor = transform_cursor(self._my_cursor, op, False)
        self._update_cursors()

    def _on_ack(self, msg):
        if self._inflight is None or msg.get("id") != self._inflight_id:
            return
        # The server committed _inflight exactly as we hold it, and moved every
        # cursor through it; none of those cursors is ours.
        for name, pos in self._remote_cursors.items():
            self._remote_cursors[name] = transform_cursor(pos, self._inflight, False)
        self._inflight = None
        self._inflight_id = None
        self._rev = msg["rev"]
        self._flush()
        self._send_cursor_if_synced()

    def _set_remote_cursor(self, name, pos):
        if name == self.name:
            return
        self._remote_cursors[name] = pos
        self._update_cursors()

    def _dispatch(self, msg):
        kind = msg.get("type")
        if kind == "error":
            for slot in self._snapshots:
                if "id" in msg and slot["id"] == msg["id"]:
                    slot.update(done=True, error=msg)
                    self._snapshots.remove(slot)
                    return
            self.errors.append(msg)
            if not self._joined:
                self._join_error = msg.get("message") or msg.get("code")
            return
        if msg.get("doc") != self.doc:
            return
        if kind == "joined" and not self._joined and "text" in msg:
            self._text = msg["text"]
            self._rev = msg["rev"]
            self._joined = True
            self._send({"type": "cursors", "doc": self.doc})
        elif kind == "snapshot":
            # Replies arrive in request order; errors were matched by id above.
            if self._snapshots:
                self._snapshots.popleft().update(done=True, text=msg.get("text"))
        elif not self._joined:
            return
        elif kind == "cursors":
            # Positions at server revision msg["rev"], which equals _rev here:
            # every op and ack before this reply has already been processed.
            self._remote_cursors = {name: pos for name, pos in msg["cursors"].items()
                                    if name != self.name}
            self._update_cursors()
        elif kind == "cursor":
            self._set_remote_cursor(msg["client"], msg["pos"])
        elif kind == "leave":
            self._remote_cursors.pop(msg.get("client"), None)
            self._update_cursors()
        elif kind == "op":
            self._on_remote_op(msg)
        elif kind == "ack":
            self._on_ack(msg)

    def _reader(self):
        try:
            stream = self._sock.makefile("rb")
            for line in stream:
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                with self._cond:
                    self._dispatch(msg)
                    self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()
