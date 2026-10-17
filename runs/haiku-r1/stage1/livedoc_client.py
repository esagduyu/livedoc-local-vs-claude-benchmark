"""Client library for the Livedoc server. Standard library only.

    with Client("127.0.0.1", 9000, "notes", "alice") as c:
        c.insert(0, "hello")
        c.wait_synced()
        print(c.text)

Edits apply to `text` at once. They reach the server one operation at a time:
at most one operation is unacknowledged, and edits made meanwhile are buffered
and sent after its ack. Remote operations are transformed against the in-flight
operation and the buffer, so `text` is always the server state plus local edits.
"""

import json
import socket
import threading
import time
import uuid

from livedoc_ot import apply_edit, apply_op, fits, is_wellformed, xf


class ProtocolError(Exception):
    """The server refused the join, or sent something the protocol does not allow."""


class Client:
    def __init__(self, host, port, doc, name, timeout=5.0):
        self._doc = doc
        self._cond = threading.Condition(threading.RLock())
        self._closed = False
        self._text = ""           # server state plus local edits
        self._rev = 0             # last server revision incorporated
        self._server_text = ""    # server state at self._rev
        self._inflight = None     # {"id", "op"}: sent, not yet acknowledged
        self._buffer = []         # local edits not yet sent
        self.error = None         # the server's error for our in-flight op, if any

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._rfile = self._sock.makefile("rb")
        try:
            self._send({"type": "join", "doc": doc, "client": name})
            joined = self._await_joined(timeout)
        except BaseException:
            self.close()
            raise
        self._text = self._server_text = joined["text"]
        self._rev = joined["rev"]
        self._sock.settimeout(None)
        threading.Thread(target=self._read_loop, daemon=True).start()

    # -- public API ---------------------------------------------------------

    @property
    def text(self):
        with self._cond:
            return self._text

    @property
    def rev(self):
        with self._cond:
            return self._rev

    def insert(self, pos, text):
        self._local_edit({"ins": pos, "text": text})

    def delete(self, pos, length):
        self._local_edit({"del": pos, "len": length})

    def wait_synced(self, timeout=5.0):
        """Block until every local edit is acknowledged. False on timeout or close."""
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._synced(), timeout)
            return self._synced()

    def close(self):
        """Disconnect. Call wait_synced() first if pending edits must reach the server."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._rfile.close()
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # -- local edits ----------------------------------------------------------

    def _local_edit(self, edit):
        with self._cond:
            if not (is_wellformed([edit]) and fits([edit], len(self._text))):
                raise ValueError(f"edit does not fit the current text: {edit}")
            self._text = apply_edit(self._text, edit)
            self._buffer.append(edit)
            self._send_buffer()

    def _synced(self):
        return self._inflight is None and not self._buffer

    def _send_buffer(self):
        """If nothing is in flight, send the buffered edits as one operation."""
        if self._inflight is not None or not self._buffer:
            return
        self._inflight = {"id": str(uuid.uuid4()), "op": self._buffer}
        self._buffer = []
        self._send({"type": "op", "doc": self._doc, "rev": self._rev,
                    "id": self._inflight["id"], "op": self._inflight["op"]})

    # -- network ------------------------------------------------------------

    def _send(self, message):
        data = json.dumps(message).encode("utf-8") + b"\n"
        try:
            self._sock.sendall(data)
        except OSError:
            self._closed = True

    def _await_joined(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no 'joined' reply within {timeout} seconds")
            self._sock.settimeout(remaining)
            line = self._rfile.readline()
            if not line:
                raise ConnectionError("server closed the connection during join")
            try:
                message = json.loads(line)
            except ValueError:
                raise ProtocolError(f"unparseable reply to join: {line!r}")
            if message.get("type") == "joined" and message.get("doc") == self._doc:
                if "text" not in message:
                    raise ProtocolError("joined reply has no text")
                return message
            if message.get("type") == "error":
                raise ProtocolError(f"join refused: {message.get('message')}")

    def _read_loop(self):
        try:
            for line in self._rfile:
                if not line.endswith(b"\n"):
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except OSError:
            pass
        finally:
            with self._cond:
                self._closed = True
                self._cond.notify_all()

    def _dispatch(self, message):
        kind = message.get("type")
        if kind == "error":
            self._on_error(message)
        elif message.get("doc") != self._doc:
            return
        elif kind == "op":
            self._on_remote(message)
        elif kind == "ack":
            self._on_ack(message)

    def _on_remote(self, message):
        """Another client's operation, committed before our in-flight one."""
        with self._cond:
            remote = message["op"]
            # Our in-flight op and buffer were written against the older state,
            # so the remote op goes first: it is the earlier one on ties.
            if self._inflight is not None:
                self._inflight["op"], remote = xf(self._inflight["op"], remote)
            self._buffer, remote = xf(self._buffer, remote)
            self._server_text = apply_op(self._server_text, message["op"])
            self._text = apply_op(self._text, remote)
            self._rev = message["rev"]
            self._cond.notify_all()

    def _on_ack(self, message):
        with self._cond:
            if self._inflight is None or message.get("id") != self._inflight["id"]:
                return
            # Every broadcast below the ack's revision has already arrived, so
            # the in-flight op, transformed to this point, is exactly the next revision.
            self._server_text = apply_op(self._server_text, self._inflight["op"])
            self._rev = message["rev"]
            self._inflight = None
            self._send_buffer()
            self._cond.notify_all()

    def _on_error(self, message):
        # A correct client never gets an error for an op it sent, because each op
        # is based on the latest revision and fits its text. If one happens, the
        # local text can no longer be reconciled with the server, so the in-flight op
        # stays put and `error` is set; wait_synced() then reports False.
        with self._cond:
            if self._inflight is not None and message.get("id") == self._inflight["id"]:
                self.error = message
                self._cond.notify_all()
