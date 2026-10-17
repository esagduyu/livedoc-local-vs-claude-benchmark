"""Livedoc client library – collaborative plain-text editing client."""

import json
import socket
import threading
import time
from collections import deque
from uuid import uuid4


class LivedocError(Exception):
    """Base exception for livedoc client errors."""
    pass


class ConnectionError(LivedocError):
    pass


class TimeoutError(LivedocError):
    pass


# ---------------------------------------------------------------------------
# Transformation engine (same as server)
# ---------------------------------------------------------------------------

def _xf_a_past_b(a, b):
    ai = "ins" in a; ad = "del" in a; bi = "ins" in b; bd = "del" in b
    if ai and bi:
        pa, ta = a["ins"], a["text"]; qb, ub = b["ins"], b["text"]
        return a if pa < qb else {"ins": pa + len(ub), "text": ta}
    if ai and bd:
        pa, ta = a["ins"], a["text"]; qb, kb = b["del"], b["len"]
        if pa <= qb: return a
        elif pa >= qb + kb: return {"ins": pa - kb, "text": ta}
        else: return {"ins": qb, "text": ta}
    if ad and bi:
        pa, ma = a["del"], a["len"]; qb, ub = b["ins"], b["text"]
        if qb <= pa: return {"del": pa + len(ub), "len": ma}
        elif qb >= pa + ma: return a
        else: return [{"del": pa, "len": qb - pa}, {"del": pa + len(ub), "len": pa + ma - qb}]
    if ad and bd:
        pa, ma = a["del"], a["len"]; qb, kb = b["del"], b["len"]
        if pa < qb:
            before_end = min(pa + ma, qb); before_len = before_end - pa
            if pa + ma > qb + kb: return [{"del": pa, "len": before_len + (pa + ma - qb - kb)}]
            else: return [{"del": pa, "len": before_len}]
        else:
            rem_start = max(pa, qb + kb); rem_end = pa + ma
            if rem_end > rem_start: return [{"del": rem_start - kb, "len": rem_end - rem_start}]
            return []
    return a


def _xf_b_past_a(a, b):
    ai = "ins" in a; ad = "del" in a; bi = "ins" in b; bd = "del" in b
    if ai and bi:
        pa, ta = a["ins"], a["text"]; qb, ub = b["ins"], b["text"]
        return b if qb <= pa else {"ins": qb + len(ta), "text": ub}
    if ai and bd:
        pa, ta = a["ins"], a["text"]; qb, kb = b["del"], b["len"]
        if pa <= qb: return {"del": qb + len(ta), "len": kb}
        elif pa >= qb + kb: return b
        else:
            return [{"del": qb, "len": pa - qb}, {"del": qb + len(ta), "len": kb - len(ta)}]
    if ad and bi:
        pa, ma = a["del"], a["len"]; qb, ub = b["ins"], b["text"]
        if qb <= pa: return b
        elif qb >= pa + ma: return {"ins": qb - ma, "text": ub}
        else: return {"ins": pa, "text": ub}
    if ad and bd:
        pa, ma = a["del"], a["len"]; qb, kb = b["del"], b["len"]
        if qb < pa:
            before_end = min(qb + kb, pa); before_len = before_end - qb
            rem_start = max(qb, pa + ma); rem_end = qb + kb
            if rem_end > rem_start: return [{"del": qb, "len": before_len + (rem_end - rem_start)}]
            else: return [{"del": qb, "len": before_len}]
        else:
            rem_start = max(qb, pa + ma); rem_end = qb + kb
            if rem_end > rem_start: return [{"del": rem_start - ma, "len": rem_end - rem_start}]
            return []
    return b


def _flatten(e):
    if isinstance(e, list): return [x for x in e if x]
    elif isinstance(e, dict): return [e] if e else []
    return []


def transform_op(A, B):
    """Transform operation A past earlier-committed operation B.

    Returns (A_prime, B_prime) where:
    - A_prime applies after B
    - B_prime applies after A
    """
    if not A and not B: return ([], [])
    if not A: return ([], list(B))
    if not B: return (list(A), [])

    A_list = list(A)
    B_list = list(B)

    def xf(A_l, B_l):
        if not A_l or not B_l:
            return (list(A_l), list(B_l))
        if len(A_l) == 1 and len(B_l) == 1:
            a1 = A_l[0]; b1 = B_l[0]
            return (_flatten(_xf_a_past_b(a1, b1)), _flatten(_xf_b_past_a(a1, b1)))
        if len(A_l) > 1:
            a1 = A_l[0]; A_rest = A_l[1:]
            a1p, B1 = xf([a1], B_l)
            A_rest_p, B2 = xf(A_rest, B1)
            return (a1p + A_rest_p, B2)
        A1, b1p = xf(A_l, [B_l[0]])
        A2, B_rest = xf(A1, B_l[1:])
        return (A2, _flatten(b1p) + B_rest)

    A_prime, B_prime = xf(A_list, B_list)
    return ([e for e in A_prime if e], [e for e in B_prime if e])


def _move_cursor(cursor_pos, op, owner_name):
    """Move a cursor position through an operation."""
    for edit in op:
        if "ins" in edit:
            p = edit["ins"]
            k = len(edit["text"])
            if p < cursor_pos or (p == cursor_pos and owner_name is not None):
                cursor_pos = cursor_pos + k
        elif "del" in edit:
            p = edit["del"]
            n = edit["len"]
            if cursor_pos > p:
                cursor_pos = max(p, cursor_pos - n)
    return cursor_pos


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Client:
    """A collaborative text editing client.

    Connects to a Livedoc server, joins a document, and allows optimistic
    local edits that are synced with the server.
    """

    def __init__(self, host: str, port: int, doc: str, name: str, timeout: float = 5.0):
        self._host = host
        self._port = port
        self._doc = doc
        self._name = name
        self._timeout = timeout

        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
            self._sock.settimeout(None)
        except OSError as e:
            raise ConnectionError(f"Cannot connect to {host}:{port}: {e}")

        self._text = ""
        self._rev = 0
        self._text_lock = threading.Lock()

        # Cursors: other client names -> positions (in current text coords)
        self.cursors: dict[str, int] = {}
        self._cursors_lock = threading.Lock()

        # Buffered local edits (not yet sent due to in-flight op)
        self._buffer: deque[list] = deque()
        self._buffer_lock = threading.Lock()

        # In-flight operation
        self._in_flight = None
        self._in_flight_lock = threading.Lock()

        # Condition to signal when in-flight op is done
        self._sync_event = threading.Event()

        # Send join BEFORE starting recv thread to avoid race
        join_msg = {"type": "join", "doc": doc, "client": name}
        self._send(join_msg)

        # Wait for join response on the main socket (before recv thread starts)
        self._sock.settimeout(timeout)
        try:
            buf = b""
            joined = None
            while True:
                try:
                    data = self._sock.recv(65536)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        raw = line.decode("utf-8", errors="replace").strip()
                        if not raw:
                            continue
                        joined = json.loads(raw)
                        break
                    if joined:
                        break
                except socket.timeout:
                    break
        finally:
            self._sock.settimeout(None)

        if joined is None:
            self._sock.close()
            raise TimeoutError("No joined response from server")
        if joined.get("type") == "error":
            self._sock.close()
            raise LivedocError(f"Join error: {joined.get('message')}")
        if joined.get("type") != "joined":
            self._sock.close()
            raise LivedocError(f"Unexpected response: {joined}")

        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

        if "text" in joined:
            self._text = joined["text"]
            self._rev = joined["rev"]
        elif "ops" in joined:
            self._text = ""
            for h in joined["ops"]:
                for edit in h["op"]:
                    if "ins" in edit:
                        p = edit["ins"]; t = edit["text"]
                        self._text = self._text[:p] + t + self._text[p:]
                    elif "del" in edit:
                        p = edit["del"]; n = edit["len"]
                        self._text = self._text[:p] + self._text[p + n:]
            self._rev = joined["rev"]

    @property
    def text(self) -> str:
        with self._text_lock:
            return self._text

    @property
    def rev(self) -> int:
        with self._text_lock:
            return self._rev

    def insert(self, pos: int, text: str) -> None:
        with self._text_lock:
            if pos < 0 or pos > len(self._text):
                raise ValueError(f"insert position {pos} out of range [0, {len(self._text)}]")
            if not text:
                raise ValueError("insert text must be non-empty")
            self._text = self._text[:pos] + text + self._text[pos:]
        self._send_op([{"ins": pos, "text": text}])

    def delete(self, pos: int, length: int) -> None:
        with self._text_lock:
            if pos < 0 or pos + length > len(self._text):
                raise ValueError(f"delete [{pos},{pos+length}) out of range [0, {len(self._text)}]")
            if length < 1:
                raise ValueError("delete length must be >= 1")
            self._text = self._text[:pos] + self._text[pos + length:]
        self._send_op([{"del": pos, "len": length}])

    def wait_synced(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while True:
            with self._in_flight_lock:
                if self._in_flight is None:
                    with self._buffer_lock:
                        if not self._buffer:
                            return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            threading.Event().wait(min(remaining, 0.05))

    def close(self) -> None:
        self._running = False
        try:
            self._sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._recv_thread.join(timeout=2)

    def _send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self._sock.sendall(data)
        except OSError as e:
            raise ConnectionError(f"Send failed: {e}")

    def _send_op(self, op: list) -> None:
        op_id = str(uuid4())
        with self._text_lock:
            base_rev = self._rev
            op_copy = list(op)

        with self._in_flight_lock:
            if self._in_flight is not None:
                with self._buffer_lock:
                    self._buffer.append(op_copy)
                return
            self._in_flight = {"op": op_copy, "id": op_id, "rev": base_rev}
            self._sync_event.clear()

        self._send({"type": "op", "doc": self._doc, "rev": base_rev, "id": op_id, "op": op_copy})

    def _recv_loop(self):
        buf = b""
        while self._running:
            try:
                data = self._sock.recv(65536)
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
                        continue
                    msg_type = msg.get("type")
                    if msg_type == "ack":
                        self._handle_ack(msg)
                    elif msg_type == "op":
                        self._handle_remote_op(msg)
                    elif msg_type == "cursor":
                        self._handle_cursor_msg(msg)
                    elif msg_type == "leave":
                        self._handle_leave_msg(msg)
            except OSError:
                if self._running:
                    break
                break

    def _handle_ack(self, msg):
        new_rev = msg.get("rev", self._rev)
        with self._in_flight_lock:
            if self._in_flight and self._in_flight["id"] == msg.get("id"):
                in_flight = self._in_flight
                self._in_flight = None
                self._sync_event.set()
        with self._text_lock:
            self._rev = new_rev
        if in_flight:
            # Transform buffered ops against the acknowledged op
            acked_op = in_flight["op"]
            with self._buffer_lock:
                buffered = list(self._buffer)
                self._buffer.clear()
            for buffered_op in buffered:
                # Transform buffered op past the acked op
                _, transformed_buffered = transform_op(buffered_op, acked_op)
                # Also transform against any remote ops that came in
                # (they're already applied to text, so we transform the buffered op)
                self._send_op(transformed_buffered)

    def _handle_remote_op(self, msg):
        remote_op = msg.get("op", [])
        new_rev = msg.get("rev", self._rev)
        remote_client = msg.get("client", "")

        # Transform in-flight local op past remote op (remote was committed earlier).
        # This gives: local_op_prime (for when local op is committed) and
        # remote_op_prime (to apply to local text which includes local op).
        with self._in_flight_lock:
            if self._in_flight:
                self._in_flight["op"], remote_op = transform_op(
                    self._in_flight["op"], remote_op)

        # Apply remote op to local text
        with self._text_lock:
            chars = list(self._text)
            for edit in remote_op:
                if "ins" in edit:
                    p = edit["ins"]; t = edit["text"]
                    chars[p:p] = list(t)
                elif "del" in edit:
                    p = edit["del"]; n = edit["len"]
                    del chars[p : p + n]
            self._text = "".join(chars)
            self._rev = new_rev

        # Update cursors
        self._update_cursors_on_op(remote_op, remote_client)

    def _handle_cursor_msg(self, msg):
        client = msg.get("client", "")
        pos = msg.get("pos")
        if client == self._name:
            return
        with self._cursors_lock:
            if pos is not None:
                self.cursors[client] = pos

    def _handle_leave_msg(self, msg):
        client = msg.get("client", "")
        with self._cursors_lock:
            self.cursors.pop(client, None)

    def _update_cursors_on_op(self, op, owner_name):
        """Update all cursor positions after a remote op is applied."""
        with self._cursors_lock:
            for name in list(self.cursors.keys()):
                if name == self._name:
                    continue
                self.cursors[name] = _move_cursor(self.cursors[name], op, name)

    def _wait_for_msg(self, timeout):
        self._sock.settimeout(timeout)
        try:
            buf = b""
            while True:
                try:
                    data = self._sock.recv(65536)
                    if not data:
                        return None
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        raw = line.decode("utf-8", errors="replace").strip()
                        if not raw:
                            continue
                        try:
                            return json.loads(raw)
                        except (json.JSONDecodeError, ValueError):
                            continue
                except socket.timeout:
                    return None
        finally:
            self._sock.settimeout(None)
