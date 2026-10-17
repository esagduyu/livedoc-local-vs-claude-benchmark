import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

# Keep the project directory clean: no bytecode caches from tests or from the servers they start.
sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def pytest_unconfigure(config):
    # The rewritten conftest itself is cached before the line above runs; remove it.
    for d in (os.path.join(ROOT, "tests", "__pycache__"), os.path.join(ROOT, "__pycache__")):
        shutil.rmtree(d, ignore_errors=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProcess:
    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.port = free_port()
        self.cwd = os.path.dirname(self.data_dir)
        self.proc = None

    def start(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server.py"), "--port", str(self.port), "--data-dir", self.data_dir],
            env=env, cwd=self.cwd,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("server exited with %s" % self.proc.returncode)
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
        self.buf = b""

    def send(self, msg):
        self.send_raw((json.dumps(msg) + "\n").encode())

    def send_raw(self, data):
        self.sock.sendall(data)

    def recv(self, timeout=5):
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def no_message(self, wait=0.2):
        try:
            msg = self.recv(timeout=wait)
        except socket.timeout:
            return True
        raise AssertionError("unexpected message %r" % (msg,))

    def request(self, msg):
        self.send(msg)
        return self.recv()

    def join(self, doc, client, **kw):
        reply = self.request(dict(type="join", doc=doc, client=client, **kw))
        assert reply["type"] == "joined", reply
        return reply

    def op(self, doc, rev, op_id, op):
        return self.request(dict(type="op", doc=doc, rev=rev, id=op_id, op=op))

    def close(self):
        self.sock.close()


@pytest.fixture
def server(tmp_path):
    s = ServerProcess(tmp_path / "data").start()
    yield s
    s.kill()


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
