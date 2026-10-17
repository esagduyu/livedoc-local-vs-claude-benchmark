"""Livedoc client library.

Also holds the operational transformation rules, which the server imports so
that both sides compute exactly the same merge.
"""

import json
import socket
import threading
import uuid


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def apply_op(text, op):
    """Apply an operation (a list of edits) to text. Raise ValueError if it does not fit."""
    for edit in op:
        if "ins" in edit:
            pos, ins = edit["ins"], edit["text"]
            if not _is_int(pos) or not 0 <= pos <= len(text):
                raise ValueError(f"insert position {pos!r} is outside the text")
            if not isinstance(ins, str) or not ins:
                raise ValueError("insert text must be a non-empty string")
            text = text[:pos] + ins + text[pos:]
        else:
            pos, length = edit["del"], edit["len"]
            if not _is_int(pos) or not _is_int(length):
                raise ValueError("delete position and length must be integers")
            if pos < 0 or length < 1 or pos + length > len(text):
                raise ValueError(f"delete [{pos}, {pos + length}) is outside the text")
            text = text[:pos] + text[pos + length:]
    return text


def move_cursor(cursor, op, author_is_owner):
    """Move a cursor at `cursor` through the edits of `op`.

    `author_is_owner` says whether the operation was made by the cursor's owner.
    An insert exactly at the cursor pushes it only for the owner's own typing.
    """
    for edit in op:
        if "ins" in edit:
            pos, length = edit["ins"], len(edit["text"])
            if pos < cursor or (pos == cursor and author_is_owner):
                cursor += length
        elif cursor > edit["del"]:
            start = edit["del"]
            cursor = max(start, cursor - edit["len"])
    return cursor


def _undo_move(pos, edit):
    """Map a position in the text after `edit` back to the text before it."""
    if "ins" in edit:
        start, length = edit["ins"], len(edit["text"])
        if pos < start:
            return pos
        if pos >= start + length:
            return pos - length
        return start
    start, length = edit["del"], edit["len"]
    return pos if pos < start else pos + length


def _ins(pos, text):
    return {"ins": pos, "text": text}


def _del(pos, length):
    return {"del": pos, "len": length}


def _xf_edit(a, b):
    """Transform one edit `a` against one earlier-committed edit `b`.

    Both were written against the same text. Returns (a2, b2), where a2 applies
    after b and b2 applies after a. Each is a list of 0, 1 or 2 edits.
    """
    if "ins" in a and "ins" in b:
        p, t = a["ins"], a["text"]
        q, u = b["ins"], b["text"]
        # On a tie the earlier-committed insert (b) stays to the left.
        a2 = [_ins(p + len(u) if p >= q else p, t)]
        b2 = [_ins(q + len(t) if q > p else q, u)]
        return a2, b2

    if "ins" in a:
        # a is an insert at p, b deletes [q, q + k).
        p, t = a["ins"], a["text"]
        q, k = b["del"], b["len"]
        if p <= q:
            a2 = [_ins(p, t)]
            b2 = [_del(q + len(t), k)]
        elif p >= q + k:
            a2 = [_ins(p - k, t)]
            b2 = [_del(q, k)]
        else:
            a2 = [_ins(q, t)]
            b2 = [_del(q, p - q), _del(q + len(t), q + k - p)]
        return a2, b2

    if "ins" in b:
        # a deletes [p, p + m), b inserts u at q.
        p, m = a["del"], a["len"]
        q, u = b["ins"], b["text"]
        if q <= p:
            a2 = [_del(p + len(u), m)]
            b2 = [_ins(q, u)]
        elif q >= p + m:
            a2 = [_del(p, m)]
            b2 = [_ins(q - m, u)]
        else:
            a2 = [_del(p, q - p), _del(p + len(u), p + m - q)]
            b2 = [_ins(p, u)]
        return a2, b2

    # Both delete. Each keeps only the characters the other has not removed.
    p, m = a["del"], a["len"]
    q, k = b["del"], b["len"]
    overlap = max(0, min(p + m, q + k) - max(p, q))
    a_len = m - overlap
    a2 = [] if a_len == 0 else [_del(p - max(0, min(q + k, p) - q), a_len)]
    b_len = k - overlap
    b2 = [] if b_len == 0 else [_del(q - max(0, min(p + m, q) - p), b_len)]
    return a2, b2


def transform(a, b):
    """Transform operation `a` against earlier-committed operation `b`.

    Returns (a2, b2): a2 applies after b, and b2 applies after a. Both give the
    same text.
    """
    if not a or not b:
        return a, b
    if len(a) == 1 and len(b) == 1:
        return _xf_edit(a[0], b[0])
    if len(a) > 1:
        first, b1 = transform(a[:1], b)
        rest, b2 = transform(a[1:], b1)
        return first + rest, b2
    a1, first = transform(a, b[:1])
    a2, rest = transform(a1, b[1:])
    return a2, first + rest


class Client:
    """One participant in a document.

    `text` is the server state plus this client's edits that are not yet
    acknowledged. Edits are sent one operation at a time: the one in flight, and
    then everything buffered behind it as a single operation.

    `cursors` maps the other participants to cursor positions in `text`.
    """

    def __init__(self, host, port, doc, name, timeout=5.0):
        self._doc = doc
        self._name = name
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._text = ""
        self._rev = 0
        self._inflight = None   # (id, op) sent but not yet acknowledged
        self._buffer = []       # local edits not yet sent
        self._cursors = {}      # other participant -> position in self._text
        self._closed = False
        self._error = None
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._rfile = self._sock.makefile("rb")
        try:
            self._send({"type": "join", "doc": doc, "client": name})
            reply = self._read()
            if reply.get("type") != "joined" or reply.get("doc") != doc:
                raise RuntimeError(f"join failed: {reply.get('message', reply)}")
            self._text = reply["text"]
            self._rev = reply["rev"]
            # Fetch the cursors. Anything the server sends before the reply is
            # handled in order, so the reply is at the revision we are at.
            self._send({"type": "cursors", "doc": doc})
            while True:
                msg = self._read()
                if msg.get("type") == "cursors" and msg.get("doc") == doc:
                    self._cursors = {n: p for n, p in msg["cursors"].items() if n != name}
                    break
                self._handle(msg)
        except BaseException:
            self._sock.close()
            raise
        self._sock.settimeout(None)
        self._reader = threading.Thread(target=self._receive, daemon=True)
        self._reader.start()

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

    def insert(self, pos, text):
        self._local({"ins": pos, "text": text})

    def delete(self, pos, length):
        self._local({"del": pos, "len": length})

    def set_cursor(self, pos):
        """Publish this client's cursor at `pos`, a position in `text`."""
        with self._lock:
            self._check_open()
            if not _is_int(pos) or not 0 <= pos <= len(self._text):
                raise ValueError(f"cursor position {pos!r} is outside the text")
            # The server takes positions at a revision, so express pos in the server's text.
            server_pos = pos
            for edit in reversed(self._pending_edits()):
                server_pos = _undo_move(server_pos, edit)
            self._send({"type": "cursor", "doc": self._doc, "rev": self._rev, "pos": server_pos})

    def wait_synced(self, timeout=5.0):
        with self._lock:
            self._idle.wait_for(lambda: self._is_idle() or self._closed, timeout)
            return self._is_idle()

    def close(self):
        with self._lock:
            self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._reader.join(timeout=1.0)
        self._rfile.close()
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _is_idle(self):
        return self._inflight is None and not self._buffer

    def _check_open(self):
        if self._closed:
            raise RuntimeError("client is closed")

    def _pending_edits(self):
        """Local edits not yet acknowledged, in order, applied on top of the server state."""
        inflight = self._inflight[1] if self._inflight else []
        return inflight + self._buffer

    def _to_local(self, server_pos):
        """Move a position at the server revision into `text`, through pending local edits."""
        for edit in self._pending_edits():
            server_pos = move_cursor(server_pos, [edit], False)
        return server_pos

    def _local(self, edit):
        with self._lock:
            self._check_open()
            self._text = apply_op(self._text, [edit])  # raises ValueError if invalid
            # Our own edits never push someone else's cursor past it on a tie.
            for name in self._cursors:
                self._cursors[name] = move_cursor(self._cursors[name], [edit], False)
            self._buffer.append(edit)
            if self._inflight is None:
                self._send_buffer()

    def _send_buffer(self):
        op_id = uuid.uuid4().hex
        op, self._buffer = self._buffer, []
        self._inflight = (op_id, op)
        self._send({"type": "op", "doc": self._doc, "rev": self._rev, "id": op_id, "op": op})

    def _send(self, msg):
        self._sock.sendall(json.dumps(msg).encode("utf-8") + b"\n")

    def _read(self):
        line = self._rfile.readline()
        if not line:
            raise ConnectionError("server closed the connection")
        msg = json.loads(line)
        if not isinstance(msg, dict):
            raise RuntimeError(f"unexpected message from server: {msg!r}")
        return msg

    def _receive(self):
        try:
            for line in self._rfile:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                with self._lock:
                    self._handle(msg)
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._closed = True
                self._idle.notify_all()

    def _handle(self, msg):
        kind = msg.get("type")
        if kind == "error":
            if self._inflight is not None and msg.get("id") == self._inflight[0]:
                self._error = msg.get("message", "server rejected an edit")
                self._idle.notify_all()
            return
        if msg.get("doc") != self._doc:
            return
        if kind == "op":
            self._apply_remote(msg["rev"], msg["client"], msg["op"])
        elif kind == "ack":
            self._acknowledged(msg)
        elif kind == "cursor":
            if msg["client"] != self._name:
                self._cursors[msg["client"]] = self._to_local(msg["pos"])
        elif kind == "leave":
            self._cursors.pop(msg["client"], None)

    def _apply_remote(self, rev, client, op):
        # The remote operation was committed before anything we have in flight or buffered.
        if self._inflight is not None:
            op_id, mine = self._inflight
            mine, op = transform(mine, op)
            self._inflight = (op_id, mine)
        if self._buffer:
            self._buffer, op = transform(self._buffer, op)
        self._text = apply_op(self._text, op)
        self._rev = rev
        for name in self._cursors:
            self._cursors[name] = move_cursor(self._cursors[name], op, name == client)

    def _acknowledged(self, msg):
        if self._inflight is None or msg.get("id") != self._inflight[0]:
            return
        self._inflight = None
        self._rev = msg["rev"]
        if self._buffer:
            self._send_buffer()
        else:
            self._idle.notify_all()
