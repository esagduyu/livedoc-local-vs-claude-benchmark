import glob
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.dont_write_bytecode = True  # leave nothing behind in the project



def pytest_sessionfinish(session):
    # conftest.py itself is compiled before the flag above takes effect
    d = os.path.join(ROOT, "tests", "__pycache__")
    for f in glob.glob(os.path.join(d, "conftest.*.pyc")):
        os.remove(f)
    try:
        os.rmdir(d)
    except OSError:
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProcess:
    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.port = None
        self.proc = None

    def start(self):
        self.port = free_port() if self.port is None else self.port
        self.proc = subprocess.Popen(
            [sys.executable, "-B", os.path.join(ROOT, "server.py"),
             "--port", str(self.port), "--data-dir", self.data_dir],
            cwd=os.path.dirname(self.data_dir))
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("server exited early")
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()

    def restart(self):
        self.kill()
        return self.start()


class Conn:
    """A raw protocol connection for tests."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.file = self.sock.makefile("rb")

    def send(self, msg):
        self.send_raw((json.dumps(msg) + "\n").encode())

    def send_raw(self, data):
        self.sock.sendall(data)

    def recv(self):
        line = self.file.readline()
        if not line:
            raise EOFError
        return json.loads(line)

    def request(self, msg):
        self.send(msg)
        return self.recv()

    def join(self, doc, client, **extra):
        return self.request({"type": "join", "doc": doc, "client": client, **extra})

    def op(self, doc, rev, op_id, op):
        return self.request({"type": "op", "doc": doc, "rev": rev, "id": op_id, "op": op})

    def expect_silence(self, seconds=0.2):
        self.sock.settimeout(seconds)
        try:
            data = self.sock.recv(1, socket.MSG_PEEK)
        except (socket.timeout, TimeoutError):
            return
        finally:
            self.sock.settimeout(5)
        raise AssertionError(f"unexpected data: {data!r}")

    def close(self):
        self.file.close()
        self.sock.close()


@pytest.fixture
def server(tmp_path):
    srv = ServerProcess(tmp_path / "data").start()
    yield srv
    srv.kill()


@pytest.fixture
def connect(server):
    conns = []

    def make():
        c = Conn(server.port)
        conns.append(c)
        return c

    yield make
    for c in conns:
        c.close()
