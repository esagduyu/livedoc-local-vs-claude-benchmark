import json
import pathlib
import socket
import subprocess
import sys
import time

import pytest

# Keep the project directory free of __pycache__ directories.
sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProcess:
    """A server.py subprocess on a free port with its data in a temp directory."""

    def __init__(self, data_dir, log_path):
        self.data_dir = data_dir
        self.log_path = log_path
        self.port = free_port()
        self.proc = None

    def start(self):
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "server.py"),
             "--port", str(self.port), "--data-dir", str(self.data_dir)],
            stdout=subprocess.DEVNULL, stderr=log, cwd=str(self.data_dir))
        log.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited: " + self.log_path.read_text())
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("server did not start listening")

    def kill(self):
        """SIGKILL, as a crash would."""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def restart(self):
        self.kill()
        return self.start()

    def stop(self):
        self.kill()


class Wire:
    """A raw protocol connection for tests that check exact messages."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.file = self.sock.makefile("rb")

    def send_raw(self, data):
        self.sock.sendall(data)

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def recv(self):
        line = self.file.readline()
        assert line, "connection closed by server"
        return json.loads(line)

    def recv_until(self, kind):
        """Receive messages until one of type ``kind`` arrives; return it."""
        while True:
            msg = self.recv()
            if msg.get("type") == kind:
                return msg

    def close(self):
        self.file.close()
        self.sock.close()


@pytest.fixture
def server(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    srv = ServerProcess(data, tmp_path / "server.err").start()
    yield srv
    srv.stop()


@pytest.fixture
def connect():
    """Factory for raw protocol connections; all are closed at teardown."""
    made = []

    def make(port):
        w = Wire(port)
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()
