"""Client library for the Livedoc server.

Example::

    client = Client("127.0.0.1", 9000, "notes", "alice")
    client.insert(0, "hello")
    client.wait_synced()
    print(client.text)
"""

import json
import socket
import threading
import time
import uuid

from livedoc_ot import DEL, INS, apply, edits_from_wire, edits_to_wire, validate, xf


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class Client:
    """A replica of one document, kept in sync with the server.

    ``text`` is the server's text plus this client's unacknowledged edits.
    ``rev`` is the last server revision this client has incorporated.

    At most one operation is in flight at a time (``_inflight``). Local edits
    made meanwhile are composed into ``_buffer`` and sent once the in-flight
    operation is acknowledged. ``_server_text`` is the text at ``_rev``.
    """

    def __init__(self, host, port, doc, name, timeout=5.0):
        self.doc = doc
        self.name = name
        self._lock = threading.Condition()
        self._closed = False  # close() was called
        self._lost = False  # the connection ended
        self._error = None  # a server error that stopped synchronisation
        self._inflight = []  # edits of the in-flight op, against _server_text
        self._inflight_id = None  # None when nothing is in flight
        self._buffer = []  # local edits made while an op is in flight

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._file = self._sock.makefile("rb")
        try:
            self._join(timeout)
        except BaseException:
            self._file.close()
            self._sock.close()
            raise
        self._sock.settimeout(None)
        self._thread = threading.Thread(target=self._read_loop, name=f"livedoc-{doc}", daemon=True)
        self._thread.start()

    @property
    def text(self):
        with self._lock:
            return self._text

    @property
    def rev(self):
        with self._lock:
            return self._rev

    def insert(self, pos, text):
        if not _is_int(pos) or not isinstance(text, str) or not text:
            raise ValueError("insert needs an integer position and non-empty text")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("insert text is not valid Unicode") from None
        self._local((INS, pos, text))

    def delete(self, pos, length):
        if not _is_int(pos) or not _is_int(length):
            raise ValueError("delete needs integer position and length")
        self._local((DEL, pos, length))

    def wait_synced(self, timeout=5.0):
        """Block until every local edit is acknowledged. False on timeout or failure."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while not self._synced():
                if self._error is not None or self._lost or self._closed:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock.wait(remaining)
            return True

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            self._lock.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # Internals. Apart from _join (which runs before the reader thread starts), the
    # methods below expect self._lock to be held.

    def _join(self, timeout):
        self._send({"type": "join", "doc": self.doc, "client": self.name})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"join did not complete within {timeout} seconds")
            self._sock.settimeout(remaining)
            line = self._file.readline()
            if not line:
                raise ConnectionError("server closed the connection")
            msg = json.loads(line)
            if msg.get("type") == "error":
                raise RuntimeError(f"join failed: {msg.get('code')}: {msg.get('message')}")
            if msg.get("type") == "joined" and msg.get("doc") == self.doc:
                self._rev = msg["rev"]
                self._server_text = msg["text"]
                self._text = msg["text"]
                return

    def _read_loop(self):
        try:
            for line in self._file:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                with self._lock:
                    self._handle(msg)
                    self._lock.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._lost = True
                self._lock.notify_all()

    def _handle(self, msg):
        if self._error is not None:
            return
        kind = msg.get("type")
        try:
            if kind == "error":
                if self._inflight_id is not None and msg.get("id") == self._inflight_id:
                    self._error = f"server rejected an edit: {msg.get('code')}: {msg.get('message')}"
            elif msg.get("doc") != self.doc:
                return
            elif kind == "ack":
                self._on_ack(msg)
            elif kind == "op":
                self._on_remote(msg)
        except (KeyError, TypeError, ValueError) as e:
            self._error = f"malformed message from server: {e}"

    def _on_ack(self, msg):
        if self._inflight_id is None or msg.get("id") != self._inflight_id:
            return
        if msg.get("rev") != self._rev + 1:
            self._error = "acknowledgement for an unexpected revision"
            return
        self._server_text = apply(self._server_text, self._inflight)
        self._rev += 1
        self._inflight = []
        self._inflight_id = None
        if self._buffer:
            self._inflight, self._buffer = self._buffer, []
            self._inflight_id = uuid.uuid4().hex
            self._send_op()
        self._refresh()

    def _on_remote(self, msg):
        if msg.get("rev") != self._rev + 1:
            self._error = "operation received out of sequence"
            return
        edits = edits_from_wire(msg["op"])
        validate(edits, len(self._server_text))
        self._server_text = apply(self._server_text, edits)
        # The remote op was committed first. Move our pending edits past it.
        self._inflight, after_inflight = xf(self._inflight, edits)
        self._buffer, _ = xf(self._buffer, after_inflight)
        self._rev += 1
        self._refresh()

    def _refresh(self):
        self._text = apply(apply(self._server_text, self._inflight), self._buffer)

    def _synced(self):
        return self._inflight_id is None and not self._buffer

    def _local(self, edit):
        with self._lock:
            if self._closed:
                raise RuntimeError("client is closed")
            if self._error is not None:
                raise RuntimeError(self._error)
            if self._lost:
                raise ConnectionError("connection to the server was lost")
            validate([edit], len(self._text))
            if self._inflight_id is None:
                self._inflight = [edit]
                self._inflight_id = uuid.uuid4().hex
                self._send_op()
            else:
                self._buffer.append(edit)
            self._refresh()

    def _send_op(self):
        self._send({
            "type": "op",
            "doc": self.doc,
            "rev": self._rev,
            "id": self._inflight_id,
            "op": edits_to_wire(self._inflight),
        })

    def _send(self, obj):
        self._sock.sendall((json.dumps(obj) + "\n").encode())
