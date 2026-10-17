import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server as server_module  # noqa: E402
from helpers import Wire, free_port  # noqa: E402


@pytest.fixture
def running_server(tmp_path):
    """An in-process server on a free port, using a temp data directory."""
    data_dir = tmp_path / "data"
    server = server_module.make_server(0, str(data_dir))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield {"port": port, "data_dir": str(data_dir), "server": server}
    server.shutdown()
    server.server_close()


@pytest.fixture
def wire(running_server):
    connections = []

    def connect():
        conn = Wire(running_server["port"])
        connections.append(conn)
        return conn

    yield connect
    for conn in connections:
        conn.close()


class ServerProcess:
    """server.py as a real subprocess, so it can be killed with SIGKILL."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.port = free_port()
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-B", os.path.join(ROOT, "server.py"),
             "--port", str(self.port), "--data-dir", self.data_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.kill()


@pytest.fixture
def server_process(tmp_path):
    procs = []

    def make(data_dir=None):
        proc = ServerProcess(str(data_dir or tmp_path / "data"))
        procs.append(proc)
        return proc.start()

    yield make
    for proc in procs:
        proc.stop()
