"""Livedoc client library with OT transformation core and client state machine."""

import json
import socket
import threading
import uuid
import time


def validate_op(op, length):
    """Validate an operation against the current text length.

    Returns (error, resulting_length) where error is "bad_request"/"out_of_range"/None
    and resulting_length is the new text length after applying the op.
    """
    if not isinstance(op, list):
        return "bad_request", length
    for edit in op:
        if not isinstance(edit, dict):
            return "bad_request", length
        has_ins = "ins" in edit
        has_del = "del" in edit
        if not has_ins and not has_del:
            return "bad_request", length
        if has_ins and has_del:
            return "bad_request", length
        if has_ins:
            if not isinstance(edit.get("ins"), int):
                return "bad_request", length
            if not isinstance(edit.get("text"), str):
                return "bad_request", length
            if edit["ins"] < 0 or edit["ins"] > length:
                return "out_of_range", length
            if len(edit["text"]) == 0:
                return "bad_request", length
        else:  # has_del
            if not isinstance(edit.get("len"), int):
                return "bad_request", length
            if edit["len"] < 1:
                return "bad_request", length
            # For delete, we also need to check the position is valid
            # We'll check against length after knowing the full context
    return None, length


def apply_op(text, op):
    """Apply a list of edits (operation) to text, left to right.

    Each edit is either {"ins": P, "text": T} or {"del": P, "len": N}.
    Returns the resulting text.
    """
    result = text
    for edit in op:
        if "ins" in edit:
            pos = edit["ins"]
            txt = edit["text"]
            result = result[:pos] + txt + result[pos:]
        elif "del" in edit:
            pos = edit["del"]
            n = edit["len"]
            result = result[:pos] + result[pos + n:]
    return result


def xf_edit(a, b):
    """Transform two edits against each other.

    a is the edit being transformed, b is an earlier-committed edit.
    Both were written against the same text.
    Returns (a_list, b_list) where a_list applies after b, and b_list applies after a.
    The earlier-committed edit (b) wins insert ties.
    """
    a_is_ins = "ins" in a
    b_is_ins = "ins" in b

    if a_is_ins and b_is_ins:
        a_pos_val = a["ins"]
        b_pos_val = b["ins"]
        a_text = a["text"]
        b_text = b["text"]
        if a_pos_val < b_pos_val:
            # a unchanged, b unchanged
            return ([a], [b])
        elif a_pos_val > b_pos_val:
            # a shifts right by len(b_text)
            new_a = {"ins": a_pos_val + len(b_text), "text": a_text}
            return ([new_a], [b])
        else:  # tie, a_pos_val == b_pos_val
            # Earlier-committed stays left. b was committed earlier, so b stays left.
            # a is placed after b: at b_pos_val + len(b_text)
            new_a = {"ins": b_pos_val + len(b_text), "text": a_text}
            return ([new_a], [b])

    elif a_is_ins and not b_is_ins:
        # a is ins, b is del
        a_pos_val = a["ins"]
        a_text = a["text"]
        b_pos_val = b["del"]
        b_len = b["len"]
        if a_pos_val <= b_pos_val:
            # a unchanged, left of delete
            return ([a], [b])
        elif a_pos_val >= b_pos_val + b_len:
            # a is after the delete range; shift left by b_len
            new_a = {"ins": a_pos_val - b_len, "text": a_text}
            return ([new_a], [b])
        else:
            # a is strictly inside the delete range [b_pos_val, b_pos_val + b_len)
            # a moves to b_pos_val (position where delete starts)
            new_a = {"ins": b_pos_val, "text": a_text}
            return ([new_a], [b])

    elif not a_is_ins and b_is_ins:
        # a is del, b is ins
        a_pos_val = a["del"]
        a_len = a["len"]
        b_pos_val = b["ins"]
        b_text = b["text"]
        if b_pos_val <= a_pos_val:
            # b lands before or at a's position; a shifts right by len(b_text)
            new_a = {"del": a_pos_val + len(b_text), "len": a_len}
            return ([new_a], [b])
        elif b_pos_val >= a_pos_val + a_len:
            # b lands after the delete range; a unchanged
            return ([a], [b])
        else:
            # b lands strictly inside the delete range, so a splits
            left_len = b_pos_val - a_pos_val
            right_len = a_pos_val + a_len - b_pos_val
            result_edits = []
            if left_len > 0:
                result_edits.append({"del": a_pos_val, "len": left_len})
            if right_len > 0:
                result_edits.append({"del": a_pos_val + len(b_text), "len": right_len})
            # b is unchanged
            return (result_edits, [b])

    else:
        # Both are deletes
        a_pos_val = a["del"]
        a_len = a["len"]
        b_pos_val = b["del"]
        b_len = b["len"]
        # a deletes only the characters b has not already deleted
        if a_pos_val + a_len <= b_pos_val:
            # a is entirely before b; unchanged
            return ([a], [b])
        elif a_pos_val >= b_pos_val + b_len:
            # a is entirely after b; unchanged
            return ([a], [b])
        else:
            # Overlapping: a's remainder after b's deletion
            remaining_start = max(a_pos_val, b_pos_val + b_len)
            remaining_len = (a_pos_val + a_len) - remaining_start
            if remaining_len <= 0:
                # Nothing remains
                return ([], [])
            else:
                return ([{"del": remaining_start, "len": remaining_len}], [b])


def xf(A, B):
    """Recursive transform of operation A against earlier operation B.

    Returns (A', B') where A' applies after B, B' applies after A.
    Exactly as the pseudocode in the spec.
    """
    if not A:
        return ([A], [B])
    if not B:
        return ([A], [B])

    if len(A) == 1 and len(B) == 1:
        return xf_edit(A[0], B[0])
    elif len(A) > 1:
        # A has more than one edit
        (a1_prime, B1) = xf([A[0]], B)
        (rest_prime, B2) = xf(A[1:], B1)
        combined = a1_prime + rest_prime
        return (combined, B2)
    elif len(B) > 1:
        # B has more than one edit
        (A1, b1_prime) = xf(A, [B[0]])
        (A2, rest_prime) = xf(A1, B[1:])
        combined_b = b1_prime + rest_prime
        return (A2, combined_b)
    else:
        return ([A], [B])


def transform_against_history(op, ops):
    """Transform an operation against a list of committed operations, in order.

    ops is a list of operations (each operation is a list of edits), committed in order.
    Returns the transformed operation.
    """
    current_op = list(op)
    for committed_op in ops:
        if not committed_op:  # empty operation
            continue
        current_op, _ = xf(current_op, committed_op)
    return current_op


class Client:
    """A livedoc client that connects to a server and participates in collaborative editing."""

    def __init__(self, host: str, port: int, doc: str, name: str, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.doc = doc
        self.name = name
        self.timeout = timeout
        self._lock = threading.Lock()
        self._socket = None
        self._reader_thread = None
        self._running = False
        self._connected = False
        self._joined = False
        self._rev = 0
        self._text = ""
        self._inflight_id = None
        self._inflight_op = None
        self._buffer = []  # pending operations to send
        self._error_records = []
        self._cond = threading.Condition(self._lock)

        # Connect
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(timeout)
        self._socket.connect((self.host, self.port))
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # Send join message
        join_msg = {"type": "join", "doc": self.doc, "client": self.name}
        self._send(json.dumps(join_msg))

        # Wait for joined response
        with self._cond:
            if not self._joined:
                self._cond.wait(timeout=self.timeout)

        if not self._joined:
            self.close()
            raise TimeoutError(f"Did not join doc '{self.doc}' within {timeout} seconds")

    def _send(self, msg):
        """Send a message to the server."""
        try:
            self._socket.sendall((msg + "\n").encode("utf-8"))
        except Exception:
            pass

    def _read_loop(self):
        """Background thread that reads messages from the server."""
        buf = ""
        try:
            while self._running:
                try:
                    data = self._socket.recv(4096)
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        self._process_message(line)
                except socket.timeout:
                    continue
        except Exception:
            pass
        finally:
            self._running = False
            try:
                self._socket.close()
            except Exception:
                pass

    def _process_message(self, line):
        """Process a single JSON message from the server."""
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return

        msg_type = msg.get("type")

        with self._lock:
            if msg_type == "joined":
                self._joined = True
                self._rev = msg.get("rev", 0)
                if "ops" in msg:
                    ops_list = msg["ops"]
                    for op_entry in ops_list:
                        self._apply_remote_op_from_entry(op_entry)
                else:
                    self._text = msg.get("text", "")
                self._cond.notify_all()

            elif msg_type == "ack":
                rev = msg.get("rev", 0)
                client_id = msg.get("id", "")
                with self._lock:
                    if client_id == self._inflight_id:
                        self._rev = rev
                        self._inflight_id = None
                        self._inflight_op = None
                        # Send buffered operations
                        self._send_buffer()
                self._cond.notify_all()

            elif msg_type == "op":
                client_name = msg.get("client", "")
                op_id = msg.get("id", "")
                op = msg.get("op", [])
                with self._lock:
                    # Transform remote op against our inflight operation
                    if op and self._inflight_op:
                        transformed_op, transformed_inflight = xf(op, self._inflight_op)
                        self._inflight_op = transformed_inflight if transformed_inflight else []
                    # Apply transformed op to our text
                    if op:
                        self._text = apply_op(self._text, op)
                        self._rev = msg.get("rev", self._rev)

            elif msg_type == "error":
                code = msg.get("code", "")
                message = msg.get("message", "")
                client_id = msg.get("id", "")
                with self._lock:
                    self._error_records.append({"code": code, "message": message, "id": client_id})

            # Ignore messages for other docs; other types are harmless

    def _apply_remote_op_from_entry(self, op_entry):
        """Apply a remote operation entry (from 'since' or broadcast on join)."""
        op = op_entry.get("op", [])
        rev = op_entry.get("rev", 0)
        # Transform against our inflight op
        if op and self._inflight_op:
            transformed_op, transformed_inflight = xf(op, self._inflight_op)
            self._inflight_op = transformed_inflight if transformed_inflight else []
        if op:
            self._text = apply_op(self._text, op)
        self._rev = rev

    def _send_buffer(self):
        """Send all buffered operations."""
        with self._lock:
            while self._buffer:
                item = self._buffer.pop(0)
                op = item.get("op", [])
                based_rev = item.get("based_rev", self._rev)
                msg_id = str(uuid.uuid4())
                msg = {"type": "op", "doc": self.doc, "rev": based_rev, "id": msg_id, "op": op}
                self._send(json.dumps(msg))
                # After sending, update inflight state
                self._inflight_id = msg_id
                self._inflight_op = list(op) if op else None

    def insert(self, pos: int, text: str) -> None:
        """Insert text at position pos in the local text."""
        with self._lock:
            if pos < 0 or pos > len(self._text):
                raise ValueError(f"Position {pos} out of range for text of length {len(self._text)}")
            if not text or not isinstance(text, str):
                raise ValueError("Insert text must be non-empty string")

            # Apply optimistically to local text
            self._text = self._text[:pos] + text + self._text[pos:]

            op = [{"ins": pos, "text": text}]

            if self._inflight_id is not None:
                # Buffer this edit; will be sent after ack
                self._buffer.append({"op": op, "based_rev": self._rev})
            else:
                self._send_op(op, self._rev + 1)

    def delete(self, pos: int, length: int) -> None:
        """Delete length characters starting at pos in the local text."""
        with self._lock:
            if pos < 0 or pos >= len(self._text):
                raise ValueError(f"Position {pos} out of range for text of length {len(self._text)}")
            if length < 1 or pos + length > len(self._text):
                raise ValueError(
                    f"Delete length {length} out of range at position {pos}"
                )

            # Apply optimistically
            self._text = self._text[:pos] + self._text[pos + length :]

            op = [{"del": pos, "len": length}]

            if self._inflight_id is not None:
                self._buffer.append({"op": op, "based_rev": self._rev})
            else:
                self._send_op(op, self._rev + 1)

    def _send_op(self, op, rev):
        """Send an operation to the server."""
        msg_id = str(uuid.uuid4())
        msg = {"type": "op", "doc": self.doc, "rev": rev, "id": msg_id, "op": op}
        self._send(json.dumps(msg))
        self._inflight_id = msg_id
        self._inflight_op = list(op) if op else None

    def wait_synced(self, timeout: float = 5.0) -> bool:
        """Block until every local edit has been acknowledged.

        Returns True when synced, or False on timeout.
        """
        with self._lock:
            start = time.time()
            while self._inflight_id is not None:
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            return True

    def close(self):
        """Close the client connection."""
        self._running = False
        with self._lock:
            self._inflight_id = None
            self._inflight_op = None
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None