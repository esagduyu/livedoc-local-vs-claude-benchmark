"""Grader wire helpers: server process control, raw protocol client, scriptable fake server."""
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time

from lg_ot import cursor_xf


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProc:
    """Runs `python server.py --port P --data-dir D` from a project dir."""

    def __init__(self, project, data_dir, python, port=None):
        self.project, self.data_dir, self.python = project, data_dir, python
        self.port = port or free_port()
        self.proc = None
        self.log = os.path.join(os.path.dirname(data_dir), f"server-{os.path.basename(data_dir)}.log")

    def start(self, wait=10.0):
        with open(self.log, "ab") as lf:
            self.proc = subprocess.Popen(
                [self.python, "server.py", "--port", str(self.port), "--data-dir", self.data_dir],
                cwd=self.project, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < wait:
            if self.proc.poll() is not None:
                return False
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return True
            except OSError:
                time.sleep(0.05)
        return False

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def kill9(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(5)

    def stop(self):
        self.kill9()

    def tail(self, n=600):
        try:
            with open(self.log, "rb") as f:
                return f.read()[-n:].decode("utf-8", "replace")
        except OSError:
            return ""


class RawClient:
    """Line-JSON protocol client with a background reader."""

    def __init__(self, port, host="127.0.0.1"):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(None)
        self.q = queue.Queue()
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            self.q.put(json.loads(line))
                        except ValueError:
                            self.q.put({"type": "__unparseable__", "raw": line[:200].decode("utf-8", "replace")})
        except OSError:
            pass
        self.closed = True
        self.q.put(None)

    def send(self, obj):
        self.sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def send_raw(self, data):
        self.sock.sendall(data)

    def recv(self, timeout=5.0):
        """Next message, or raise TimeoutError / ConnectionError."""
        try:
            m = self.q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no message within %.1fs" % timeout) from None
        if m is None:
            self.q.put(None)
            raise ConnectionError("connection closed")
        return m

    def recv_type(self, typ, timeout=5.0, skip=("op", "cursor", "leave")):
        """Next message of type `typ`, skipping (and returning) broadcasts listed in `skip`."""
        skipped = []
        deadline = time.time() + timeout
        while True:
            m = self.recv(max(0.01, deadline - time.time()))
            if m.get("type") == typ:
                return m, skipped
            if m.get("type") in skip:
                skipped.append(m)
                continue
            raise AssertionError(f"expected {typ}, got {json.dumps(m)[:200]}")

    def quiet(self, wait=0.3):
        """Return all messages that arrive within `wait` seconds."""
        out = []
        deadline = time.time() + wait
        while True:
            try:
                m = self.q.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                return out
            if m is None:
                self.q.put(None)
                return out
            out.append(m)

    def join(self, doc, client, since=None):
        msg = {"type": "join", "doc": doc, "client": client}
        if since is not None:
            msg["since"] = since
        self.send(msg)
        m, _ = self.recv_type("joined")
        return m

    def op(self, doc, rev, op, id_):
        self.send({"type": "op", "doc": doc, "rev": rev, "id": id_, "op": op})
        m, skipped = self.recv_type("ack")
        if m.get("id") != id_:
            raise AssertionError(f"ack for wrong id: {m}")
        return m["rev"], skipped

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class FakeServer:
    """A one-connection scriptable server for testing a client library in isolation."""

    def __init__(self):
        self.lsock = socket.socket()
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(4)
        self.port = self.lsock.getsockname()[1]
        self.conn = None
        self.q = queue.Queue()
        self.rev = 0                      # latest revision this fake server has told the client about
        self.cursors = {}                 # cursors it has announced, moved through the ops it has broadcast
        self.send_lock = threading.RLock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.conn, _ = self.lsock.accept()
        buf = b""
        try:
            while True:
                chunk = self.conn.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        m = json.loads(line)
                        if isinstance(m, dict) and m.get("type") == "cursors":
                            # Stage-2 protocol query; answer it like a real server would, in order with what was sent.
                            # (Acked client ops aren't applied to these; no check queries after its own edits.)
                            with self.send_lock:
                                self.send({"type": "cursors", "doc": m.get("doc"), "rev": self.rev,
                                           "cursors": dict(self.cursors)})
                            continue
                        self.q.put(m)
        except (OSError, ValueError):
            pass
        self.q.put(None)

    def recv(self, timeout=5.0):
        try:
            m = self.q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("client sent nothing within %.1fs" % timeout) from None
        if m is None:
            raise ConnectionError("client disconnected")
        return m

    def no_message(self, wait=0.6):
        try:
            m = self.q.get(timeout=wait)
        except queue.Empty:
            return None
        return m if m is not None else None

    def send(self, obj):
        with self.send_lock:
            t = obj.get("type")
            if t in ("joined", "op", "ack") and isinstance(obj.get("rev"), int):
                self.rev = max(self.rev, obj["rev"])
            if t == "cursor":
                self.cursors[obj.get("client")] = obj.get("pos")
            elif t == "leave":
                self.cursors.pop(obj.get("client"), None)
            elif t == "op" and isinstance(obj.get("op"), list):
                try:
                    self.cursors = {n: cursor_xf(c, obj["op"], n == obj.get("client")) for n, c in self.cursors.items()}
                except (KeyError, TypeError):
                    pass
            self.conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def close(self):
        for s in (self.conn, self.lsock):
            try:
                s.close()
            except Exception:
                pass


def wait_until(pred, timeout=5.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step)
    try:
        return bool(pred())
    except Exception:
        return False
