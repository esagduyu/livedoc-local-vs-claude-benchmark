import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True  # keep __pycache__ out of the project directory
sys.path.insert(0, PROJECT)

ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(data_dir):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(PROJECT, "server.py"), "--port", str(port), "--data-dir", str(data_dir)],
        cwd=PROJECT, env=ENV,
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc, port
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.05)


def stop_server(proc, sig=signal.SIGKILL):
    proc.send_signal(sig)
    proc.wait(timeout=10)


class Wire:
    """Raw line-protocol connection, for protocol-level tests."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.rfile = self.sock.makefile("rb")

    def send(self, msg):
        data = msg if isinstance(msg, bytes) else json.dumps(msg).encode("utf-8")
        self.sock.sendall(data + b"\n")

    def recv(self):
        line = self.rfile.readline()
        assert line, "connection closed"
        return json.loads(line)

    def close(self):
        self.rfile.close()  # the file object keeps the socket open otherwise
        self.sock.close()


def pytest_sessionfinish(session):
    # Pytest caches conftest.py's bytecode before the sys.dont_write_bytecode line above runs.
    shutil.rmtree(os.path.join(os.path.dirname(__file__), "__pycache__"), ignore_errors=True)


@pytest.fixture
def server(tmp_path):
    data_dir = tmp_path / "data"
    proc, port = start_server(data_dir)
    yield port, data_dir
    if proc.poll() is None:
        stop_server(proc)
