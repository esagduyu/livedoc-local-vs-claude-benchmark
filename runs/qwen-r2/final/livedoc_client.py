"""Livedoc client library – real-time collaborative plain-text editing."""

import json
import socket
import threading
import uuid
import time


def _time():
    return time.monotonic()


def _apply_op(text, op):
    """Apply an operation (list of edits) to a text string."""
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


def _edit_to_list(e):
    """Convert a single edit dict to a one-element list, or return as-is if list."""
    if isinstance(e, list):
        return e
    if e is None:
        return []
    return [e]


def _transform_edit_a_against_b(a, b):
    """Transform edit a against earlier-committed edit b.
    Returns (a_prime, b_prime) where a_prime applies after b,
    and b_prime applies after a.
    a_prime is a single edit dict, None (vanished), or a list of 2 edits.
    b_prime is a single edit dict (never a list, since b is always a single edit)."""

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
        # b' is b (del) transformed to apply after a (ins)
        # Using del-vs-ins rule with roles reversed:
        #   del at qb, len kb | ins at pa, text ta
        #   pa <= qb: del at qb + len(ta)
        #   pa >= qb + kb: unchanged
        #   Otherwise: del at qb, len pa - qb
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
                overlap_start = max(qb, pa)
                overlap_end = min(qb + kb, pa + ma)
                overlap_len = overlap_end - overlap_start
                b_prime = {"del": qb, "len": kb - overlap_len}
            elif qb + kb <= pa:
                # b is entirely before a
                b_prime = {"del": qb, "len": kb}
            elif qb >= pa:
                # b starts at or after a, but b extends beyond a
                overlap_start = max(qb, pa)
                overlap_end = min(qb + kb, pa + ma)
                overlap_len = overlap_end - overlap_start
                b_prime = {"del": qb, "len": kb - overlap_len}
            else:
                # b is entirely inside a
                b_prime = None
        return a_prime, b_prime


def _transform_op_a_against_b(A, B):
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
        a_prime, b_prime = _transform_edit_a_against_b(A[0], B[0])
        A_prime = [] if a_prime is None else [a_prime]
        B_prime = [] if b_prime is None else [b_prime]
        return A_prime, B_prime

    if len(A) > 1:
        # (a1', B1) = xf([a1], B) where B1 is a list
        a1_prime, b1_prime = _transform_edit_a_against_b(A[0], B[0])
        A1 = [] if a1_prime is None else [a1_prime]
        B1_extended = [b1_prime] + B[1:]
        A_rest = A[1:]
        if not A_rest:
            return A1, B1_extended
        else:
            # (rest', B2) = xf(A[1:], B1)
            A_rest_prime, B_final = _transform_op_a_against_b(A_rest, B1_extended)
            return A1 + A_rest_prime, B_final

    # len(B) > 1, len(A) == 1
    # (A1, b1') = xf(A, [b1])
    A1, b1_prime = _transform_edit_a_against_b(A[0], B[0])
    if A1 is None:
        A1 = []
    else:
        A1 = [A1]
    # (A2, rest') = xf(A1, B[1:])
    if len(B) == 1:
        return A1, []
    A2, B_rest_prime = _transform_op_a_against_b(A1, B[1:])
    # return (A2, b1' + rest')
    B_prime = [b1_prime] + B_rest_prime
    return A2, B_prime


def _transform_op_against_history(op, history_ops):
    """Transform op against a sequence of earlier-committed operations."""
    for hist_op in history_ops:
        hist_list = hist_op if isinstance(hist_op, list) else [hist_op]
        op, _ = _transform_op_a_against_b(op, hist_list)
    return op


def _transform_remote_past_local(remote_op, local_edits):
    """Transform a remote operation to apply after local edits.
    Local edits are treated as the 'earlier committed' operations.
    This is the 'second half' of transform: moving the remote op past our edits."""
    result = list(remote_op)
    for local_edit in local_edits:
        if not result:
            break
        _, b_prime = _transform_edit_a_against_b(local_edit, result[0])
        if isinstance(b_prime, list):
            result = b_prime + result[1:]
        else:
            result = [b_prime] + result[1:]
    return result


class Client:
    def __init__(self, host, port, doc, name, timeout=5.0):
        self._host = host
        self._port = port
        self._doc = doc
        self._name = name
        self._timeout = timeout
        self._text = ""
        self._rev = 0
        self._local_edits = []  # list of (edits_list, base_rev)
        self._in_flight = None  # (rev, id_, op) or None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._running = True
        self._messages = []
        self._sock = None
        self._recv_thread = None
        self._buffer = b""
        self._joined = False
        self._connect_and_join()

    @property
    def text(self):
        with self._lock:
            return self._text

    @property
    def rev(self):
        with self._lock:
            return self._rev

    def _connect_and_join(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self._timeout)
        deadline = _time() + self._timeout

        while True:
            try:
                self._sock.connect((self._host, self._port))
                break
            except (ConnectionRefusedError, OSError):
                if _time() >= deadline:
                    raise
                time.sleep(0.05)

        self._sock.settimeout(None)
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

        join_msg = {"type": "join", "doc": self._doc, "client": self._name}
        self._send(join_msg)

        with self._cond:
            deadline = _time() + self._timeout
            while self._running and not self._closed:
                self._process_messages_unlocked()
                if self._joined:
                    return
                remaining = max(0, deadline - _time())
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for join response")
                self._cond.wait(timeout=remaining)

    def _recv_loop(self):
        try:
            while self._running and not self._closed:
                data = self._sock.recv(4096)
                if not data:
                    break
                self._buffer += data
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        msg = json.loads(line_str)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(msg, dict):
                        continue
                    self._process_message(msg)
        except OSError:
            pass
        finally:
            with self._cond:
                self._running = False
                self._cond.notify_all()

    def _send(self, obj):
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self._sock.sendall(line.encode("utf-8"))

    def _process_message(self, msg):
        """Process a single incoming message. Thread-safe."""
        msg_type = msg.get("type")
        if msg_type == "joined":
            if "text" in msg:
                self._text = msg["text"]
                self._rev = msg["rev"]
            elif "ops" in msg:
                for h in msg["ops"]:
                    self._text = _apply_op(self._text, h["op"])
                self._rev = msg["rev"]
            with self._cond:
                self._joined = True
                self._cond.notify_all()
        elif msg_type == "ack":
            with self._cond:
                if self._in_flight and msg.get("id") == self._in_flight[1]:
                    self._rev = msg["rev"]
                    self._in_flight = None
                    self._flush_buffered_unlocked()
                    self._cond.notify_all()
        elif msg_type == "op":
            if msg.get("doc") != self._doc:
                return
            remote_op = msg["op"]
            remote_rev = msg["rev"]
            with self._cond:
                # Transform remote op against our local edits
                local_edits = []
                for e, _ in self._local_edits:
                    local_edits.extend(e)
                if self._in_flight:
                    local_edits.extend(self._in_flight[2])
                if local_edits:
                    transformed = _transform_remote_past_local(remote_op, local_edits)
                    self._text = _apply_op(self._text, transformed)
                else:
                    self._text = _apply_op(self._text, remote_op)
                self._rev = remote_rev
                self._cond.notify_all()
        elif msg_type == "error":
            pass

    def _process_messages_unlocked(self):
        """Process all pending messages. Must be called with lock held."""
        while self._messages:
            msg = self._messages.pop(0)
            self._process_message(msg)

    def _flush_buffered_unlocked(self):
        """Send buffered edits one at a time. Must be called with lock held."""
        while self._local_edits and not self._in_flight:
            edits, base_rev = self._local_edits.pop(0)
            op_id = str(uuid.uuid4())
            self._in_flight = (base_rev, op_id, edits)
            msg = {
                "type": "op",
                "doc": self._doc,
                "rev": base_rev,
                "id": op_id,
                "op": edits,
            }
            self._send(msg)
            break

    def insert(self, pos, text):
        if not isinstance(text, str) or len(text) == 0:
            raise ValueError("Insert text must be a non-empty string")
        with self._lock:
            if pos < 0 or pos > len(self._text):
                raise ValueError(
                    f"Insert position {pos} out of range for text of length {len(self._text)}")
            edit = {"ins": pos, "text": text}
            self._text = _apply_op(self._text, [edit])
            if self._in_flight is not None:
                self._local_edits.append(([edit], self._rev))
            else:
                op_id = str(uuid.uuid4())
                self._in_flight = (self._rev, op_id, [edit])
                msg = {
                    "type": "op",
                    "doc": self._doc,
                    "rev": self._rev,
                    "id": op_id,
                    "op": [edit],
                }
            self._cond.notify_all()
        self._send(msg)

    def delete(self, pos, length):
        if length < 1:
            raise ValueError("Delete length must be >= 1")
        with self._lock:
            if pos < 0 or pos + length > len(self._text):
                raise ValueError(
                    f"Delete [{pos}, {pos+length}) out of range for text of length {len(self._text)}")
            edit = {"del": pos, "len": length}
            self._text = _apply_op(self._text, [edit])
            if self._in_flight is not None:
                self._local_edits.append(([edit], self._rev))
            else:
                op_id = str(uuid.uuid4())
                self._in_flight = (self._rev, op_id, [edit])
                msg = {
                    "type": "op",
                    "doc": self._doc,
                    "rev": self._rev,
                    "id": op_id,
                    "op": [edit],
                }
            self._cond.notify_all()
        self._send(msg)

    def wait_synced(self, timeout=5.0):
        deadline = _time() + timeout
        with self._cond:
            while self._running and not self._closed:
                self._process_messages_unlocked()
                if self._in_flight is None and not self._local_edits:
                    return True
                remaining = max(0, deadline - _time())
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
        return False

    def close(self):
        with self._cond:
            self._closed = True
            self._running = False
            self._cond.notify_all()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
