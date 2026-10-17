"""Livedoc client library – real-time collaborative plain-text editing client."""

import json
import socket
import threading
import time
from collections import deque
from typing import Optional


class LivedocError(Exception):
    """Base exception for livedoc client errors."""
    pass


class LivedocTimeoutError(LivedocError):
    """Raised when an operation times out."""
    pass


class LivedocConnectionError(LivedocError):
    """Raised when the connection cannot be established."""
    pass


class Client:
    """A Livedoc client that connects to a server and collaboratively edits a document.

    Args:
        host: Server hostname.
        port: Server port.
        doc: Document name to join.
        name: Client name (identifier).
        timeout: Connection timeout in seconds.
    """

    def __init__(self, host: str, port: int, doc: str, name: str,
                 timeout: float = 5.0):
        self._host = host
        self._port = port
        self._doc = doc
        self._name = name
        self._timeout = timeout
        self._text = ""
        self._rev = 0
        self._lock = threading.Lock()
        self._pending_buffer: list[dict] = []
        self._in_flight: Optional[list[dict]] = None
        self._in_flight_id: Optional[str] = None
        self._synced_event = threading.Event()
        self._synced_event.set()

        # Message queue
        self._msg_queue: deque[dict] = deque()
        self._msg_cond = threading.Condition(threading.Lock())

        self._closed = False

        # Connect
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect((host, port))
        except socket.timeout:
            self._sock.close()
            raise LivedocConnectionError(
                f"Connection to {host}:{port} timed out after {timeout}s")
        except OSError as e:
            self._sock.close()
            raise LivedocConnectionError(str(e))

        self._sock.settimeout(None)

        # Join the document
        join_msg = {"type": "join", "doc": doc, "client": name}
        self._send_msg(join_msg)

        # Start receiver thread
        self._receiver_stop = threading.Event()
        self._receiver_thread = threading.Thread(target=self._receiver_loop,
                                                  daemon=True)
        self._receiver_thread.start()

        # Wait for joined response
        joined = self._wait_for_msg("joined", timeout)
        if joined is None:
            self._sock.close()
            raise LivedocTimeoutError(
                f"Did not receive 'joined' message within {timeout}s")

        self._rev = joined["rev"]
        self._text = joined.get("text", "")

        if "ops" in joined:
            for entry in joined["ops"]:
                self._apply_remote_op(entry["op"])
            self._rev = joined["rev"]

    @property
    def text(self) -> str:
        """Current local text (server state + not-yet-acknowledged edits)."""
        with self._lock:
            return self._text

    @property
    def rev(self) -> int:
        """Last server revision this client has incorporated."""
        with self._lock:
            return self._rev

    def insert(self, pos: int, text: str) -> None:
        """Insert text at position in the local text (optimistically).

        Args:
            pos: Position to insert before (0 <= pos <= len(self.text)).
            text: Non-empty string to insert.

        Raises:
            ValueError: If the edit is invalid for the current local text.
        """
        with self._lock:
            if not isinstance(text, str) or len(text) == 0:
                raise ValueError("insert text must be a non-empty string")
            if pos < 0 or pos > len(self._text):
                raise ValueError(
                    f"insert position {pos} out of range [0, {len(self._text)}]")
            self._text = self._text[:pos] + text + self._text[pos:]
            edit = {"ins": pos, "text": text}

        self._queue_edit(edit)

    def delete(self, pos: int, length: int) -> None:
        """Delete characters from the local text (optimistically).

        Args:
            pos: Start position (0 <= pos <= len(self.text)).
            length: Number of characters to delete (>= 1).

        Raises:
            ValueError: If the edit is invalid for the current local text.
        """
        with self._lock:
            if length < 1:
                raise ValueError("delete length must be >= 1")
            if pos < 0 or pos + length > len(self._text):
                raise ValueError(
                    f"delete [{pos}, {pos+length}) out of range "
                    f"[0, {len(self._text)}]")
            self._text = self._text[:pos] + self._text[pos + length:]
            edit = {"del": pos, "len": length}

        self._queue_edit(edit)

    def _queue_edit(self, edit: dict) -> None:
        """Queue an edit for sending to the server."""
        with self._lock:
            self._pending_buffer.append(edit)
            self._synced_event.clear()

        self._try_send_pending()

    def _try_send_pending(self) -> None:
        """Try to send a pending operation if none is in flight."""
        with self._lock:
            if self._in_flight is not None:
                return
            if not self._pending_buffer:
                self._synced_event.set()
                return

            edits = list(self._pending_buffer)
            self._pending_buffer.clear()
            self._in_flight = edits

        op_id = str(id(edits))
        self._in_flight_id = op_id
        op_msg = {
            "type": "op",
            "doc": self._doc,
            "rev": self._rev,
            "id": op_id,
            "op": edits,
        }
        self._send_msg(op_msg)

    def wait_synced(self, timeout: float = 5.0) -> bool:
        """Block until all local edits have been acknowledged.

        Args:
            timeout: Maximum time to wait in seconds.

        Returns:
            True if synced, False on timeout.
        """
        return self._synced_event.wait(timeout=timeout)

    def close(self) -> None:
        """Close the connection."""
        self._closed = True
        self._receiver_stop.set()
        with self._msg_cond:
            self._msg_cond.notify_all()
        try:
            self._sock.close()
        except OSError:
            pass
        self._receiver_thread.join(timeout=2.0)

    def _send_msg(self, msg: dict) -> None:
        """Send a JSON message to the server."""
        try:
            line = json.dumps(msg) + "\n"
            self._sock.sendall(line.encode("utf-8"))
        except OSError:
            self._receiver_stop.set()

    def _wait_for_msg(self, msg_type: str, timeout: float) -> Optional[dict]:
        """Wait for a specific message type from the server."""
        deadline = time.time() + timeout

        with self._msg_cond:
            while time.time() < deadline and not self._receiver_stop.is_set():
                while self._msg_queue:
                    msg = self._msg_queue.popleft()
                    if msg.get("type") == msg_type:
                        return msg
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._msg_cond.wait(timeout=min(remaining, 0.1))
        return None

    def _receiver_loop(self) -> None:
        """Background thread that reads messages from the server."""
        buffer = b""
        while not self._receiver_stop.is_set():
            try:
                data = self._sock.recv(4096)
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._handle_message(line)
            except OSError:
                break

    def _handle_message(self, raw_line: bytes) -> None:
        """Handle a received message from the server."""
        try:
            msg = json.loads(raw_line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        with self._msg_cond:
            self._msg_queue.append(msg)
            self._msg_cond.notify_all()

        if msg.get("type") not in ("ack", "op"):
            return
        if msg.get("doc") != self._doc:
            return

        if msg["type"] == "ack":
            self._handle_ack(msg)
        elif msg["type"] == "op":
            self._handle_remote_op(msg)

    def _handle_ack(self, msg: dict) -> None:
        """Handle an acknowledgment from the server."""
        new_rev = msg["rev"]
        with self._lock:
            self._in_flight = None
            self._in_flight_id = None
            self._rev = new_rev

        self._try_send_pending()

    def _handle_remote_op(self, msg: dict) -> None:
        """Handle a remote operation broadcast from the server."""
        op = msg["op"]
        op_id = msg.get("id")
        self._apply_remote_op(op, op_id)

    def _apply_remote_op(self, op: list[dict], op_id: str | None = None) -> None:
        """Apply a remote operation to local text, transforming pending edits.

        The key invariant: client_text = apply(server_text, in_flight + pending).
        When a remote op arrives:
        1. If it matches our in-flight op (same id), just update rev
        2. Otherwise, undo in-flight edits, apply remote op, re-apply transformed in-flight
        """
        with self._lock:
            if self._in_flight is not None:
                in_flight = self._in_flight
                self._in_flight = None

                # If the remote op is our in-flight op, it was already
                # accounted for in the text. Just update rev.
                if op_id == self._in_flight_id:
                    self._rev += 1
                    if not self._pending_buffer:
                        self._synced_event.set()
                    return

                # Undo in-flight edits from text
                text = undo_op(self._text, in_flight)

                # Transform in-flight edits against the remote op
                transformed = list(in_flight)
                for edit in op:
                    transformed = _transform_edits_against_edit(
                        transformed, edit)

                # Apply remote op then transformed in-flight
                text = apply_op(text, op)
                text = apply_op(text, transformed)

                self._in_flight = transformed
                self._pending_buffer = []
                self._text = text
                self._rev += 1

            else:
                # No in-flight edits, just apply the remote op
                self._text = apply_op(self._text, op)
                self._rev += 1

            if not self._in_flight and not self._pending_buffer:
                self._synced_event.set()


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


def undo_op(text: str, op: list[dict]) -> str:
    """Undo an operation, returning the text before the op was applied.

    This requires knowing what was inserted/deleted. We reverse the edits
    in reverse order.
    """
    chars = list(text)
    for edit in reversed(op):
        if "ins" in edit:
            pos = edit["ins"]
            ins_text = edit["text"]
            # Remove inserted text
            chars = chars[:pos] + chars[pos + len(ins_text):]
        elif "del" in edit:
            pos = edit["del"]
            length = edit["len"]
            # Can't undo a delete without knowing what was deleted.
            # This shouldn't happen in normal operation since we only
            # undo local edits (which we know).
            pass
    return "".join(chars)


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
    else:
        return _xf_del_del(a, b)


def _xf_ins_ins(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    p, q = a["ins"], b["ins"]
    t, u = a["text"], b["text"]
    len_u = len(u)
    if p < q:
        a_prime = [a]
    else:
        a_prime = [{"ins": p + len_u, "text": t}]
    if q <= p:
        b_prime = [b]
    else:
        b_prime = [{"ins": q + len(t), "text": u}]
    return a_prime, b_prime


def _xf_ins_del(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    p, t = a["ins"], a["text"]
    q, k = b["del"], b["len"]
    len_t = len(t)
    if p <= q:
        a_prime = [a]
    elif p >= q + k:
        a_prime = [{"ins": p - k, "text": t}]
    else:
        a_prime = [{"ins": q, "text": t}]
    if p <= q:
        b_prime = [{"del": q + len_t, "len": k}]
    elif p >= q + k:
        b_prime = [b]
    else:
        b_prime = [
            {"del": q, "len": p - q},
            {"del": p + len_t, "len": q + k - p},
        ]
    return a_prime, b_prime


def _xf_del_ins(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    p, m = a["del"], a["len"]
    q, u = b["ins"], b["text"]
    len_u = len(u)
    if q <= p:
        a_prime = [{"del": p + len_u, "len": m}]
    elif q >= p + m:
        a_prime = [a]
    else:
        a_prime = [
            {"del": p, "len": q - p},
            {"del": p + len_u, "len": p + m - q},
        ]
    if q <= p:
        b_prime = [b]
    elif q >= p + m:
        b_prime = [{"ins": q - m, "text": u}]
    else:
        b_prime = [{"ins": p, "text": u}]
    return a_prime, b_prime


def _xf_del_del(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    p, m = a["del"], a["len"]
    q, k = b["del"], b["len"]
    if q + k <= p or p + m <= q:
        a_prime = [a]
    else:
        overlap = min(p + m, q + k) - max(p, q)
        remaining = m - overlap
        if remaining > 0:
            a_prime = [{"del": p, "len": remaining}]
        else:
            a_prime = []
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


def _transform_edits_against_edit(
        edits: list[dict], remote_edit: dict) -> list[dict]:
    """Transform a list of edits against a single remote edit.

    The remote_edit was committed earlier, so we transform our edits
    to apply after it.
    """
    result = []
    for edit in edits:
        a_prime, _ = _edit_transform(edit, remote_edit)
        result.extend(a_prime)
    return result
