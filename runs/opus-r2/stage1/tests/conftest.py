import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Leave nothing behind in the project directory: no bytecode caches.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pytest_unconfigure(config):
    # conftest.py itself is compiled before the line above runs.
    shutil.rmtree(Path(__file__).resolve().parent / "__pycache__", ignore_errors=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProcess:
    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.port = free_port()
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "server.py"),
             "--port", str(self.port), "--data-dir", self.data_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited: " + self.proc.stderr.read().decode())
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()
        if self.proc:
            self.proc.stderr.close()

    def restart(self):
        self.kill()
        return self.start()


class Raw:
    """A bare protocol connection for driving the server directly."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def send(self, msg):
        self.send_raw((json.dumps(msg) + "\n").encode())

    def send_raw(self, data):
        self.sock.sendall(data)

    def _readline(self, timeout):
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            data = self.sock.recv(65536)
            if not data:
                raise EOFError("connection closed")
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def recv(self, timeout=5):
        return json.loads(self._readline(timeout))

    def request(self, msg):
        self.send(msg)
        return self.recv()

    def join(self, doc, client, **extra):
        reply = self.request({"type": "join", "doc": doc, "client": client, **extra})
        assert reply["type"] == "joined", reply
        return reply

    def assert_silent(self, timeout=0.3):
        try:
            line = self._readline(timeout)
        except TimeoutError:
            return
        raise AssertionError(f"unexpected message: {line!r}")

    def close(self):
        self.sock.close()


@pytest.fixture
def server(tmp_path):
    srv = ServerProcess(tmp_path / "data").start()
    yield srv
    srv.kill()


@pytest.fixture
def raw(server):
    conns = []

    def connect():
        c = Raw(server.port)
        conns.append(c)
        return c

    yield connect
    for c in conns:
        c.close()
