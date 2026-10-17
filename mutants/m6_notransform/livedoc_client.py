"""Reference Livedoc client (stage 1 + stage 2 cursors)."""
import json
import os
import socket
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ot import OpError, apply, cursor_xf, xf  # noqa: E402


class Client:
    def __init__(self, host, port, doc, name, timeout=5.0):
        self.doc, self.name = doc, name
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._text, self._rev = "", 0
        self._inflight = None       # (id, op)
        self._buffer = []           # edits not yet sent
        self._cursors = {}
        self._joined = False
        self._closed = False
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(None)
        self._rfile = self._sock.makefile("rb")
        self._wlock = threading.Lock()
        self._send({"type": "join", "doc": doc, "client": name})
        threading.Thread(target=self._reader, daemon=True).start()
        with self._cv:
            if not self._cv.wait_for(lambda: self._joined or self._closed, timeout):
                self.close()
                raise TimeoutError("join timed out")
            if not self._joined:
                raise ConnectionError("connection closed before join")

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

    def _send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._wlock:
            self._sock.sendall(data)

    def _local(self, edit):
        with self._lock:
            try:
                self._text = apply(self._text, [edit])
            except OpError as e:
                raise ValueError(str(e)) from None
            for n in self._cursors:
                self._cursors[n] = cursor_xf(self._cursors[n], [edit], own=False)
            self._buffer.append(edit)
            self._flush()

    def insert(self, pos, text):
        self._local({"ins": pos, "text": text})

    def delete(self, pos, length):
        self._local({"del": pos, "len": length})

    def _flush(self):
        if self._inflight is None and self._buffer:
            op, self._buffer = self._buffer, []
            self._inflight = (uuid.uuid4().hex, op)
            self._send({"type": "op", "doc": self.doc, "rev": self._rev, "id": self._inflight[0], "op": op})

    def _pending_ops(self):
        return ([self._inflight[1]] if self._inflight else []) + ([self._buffer] if self._buffer else [])

    def _reader(self):
        try:
            for line in self._rfile:
                msg = json.loads(line)
                if msg.get("doc") != self.doc:
                    continue
                with self._cv:
                    self._handle(msg)
                    self._cv.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cv:
                self._closed = True
                self._cv.notify_all()

    def _handle(self, msg):
        t = msg.get("type")
        if t == "joined":
            self._text, self._rev, self._joined = msg["text"], msg["rev"], True
        elif t == "ack":
            if self._inflight and msg["id"] == self._inflight[0]:
                self._inflight = None
                self._rev = msg["rev"]
                self._flush()
        elif t == "op":
            op = msg["op"]
            if False:
                new_inflight, op = xf(self._inflight[1], op)
                self._inflight = (self._inflight[0], new_inflight)
            if False:
                self._buffer, op = xf(self._buffer, op)
            self._text = apply(self._text, op)
            self._rev = msg["rev"]
            for n in self._cursors:
                self._cursors[n] = cursor_xf(self._cursors[n], op, own=(n == msg["client"]))
        elif t == "cursor" and msg.get("client") != self.name:
            pos = msg["pos"]
            for p in self._pending_ops():
                pos = cursor_xf(pos, p, own=False)
            self._cursors[msg["client"]] = pos
        elif t == "leave":
            self._cursors.pop(msg.get("client"), None)

    def wait_synced(self, timeout=5.0):
        with self._cv:
            return self._cv.wait_for(lambda: (self._inflight is None and not self._buffer) or self._closed,
                                     timeout) and not (self._inflight or self._buffer)

    def close(self):
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
