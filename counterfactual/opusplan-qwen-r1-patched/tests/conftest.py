"""
conftest.py — pytest configuration for Livedoc tests.

- No bytecode written (pytest.ini sets -p no:cacheprovider, sys.dont_write_bytecode).
- Server fixture: starts `python3 -B server.py` on a free port with a temp data dir.
- Waits until the server accepts a connection, then tears down the server process.
- Provides a raw-socket helper for sending/receiving line-JSON messages.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Prevent bytecode from being written anywhere
sys.dont_write_bytecode = True

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Return a port number that is likely free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    """Block until the server accepts a TCP connection or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.connect((host, port))
            return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    raise RuntimeError(f"Server did not start on {host}:{port} within {timeout}s")


def _send_line(sock: socket.socket, msg: dict) -> None:
    """Send a JSON line to the server."""
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
    sock.sendall(data)


def _recv_line(sock: socket.socket, timeout: float = 5.0) -> dict:
    """Receive a JSON line from the server. Returns the parsed dict."""
    sock.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("Server closed connection")
        buf += chunk
    line = buf.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def livedoc_server():
    """Start a Livedoc server on a free port with a temp data dir.

    Yields (host, port, data_dir) and kills the server on teardown.
    """
    port = _find_free_port()
    host = "127.0.0.1"
    with tempfile.TemporaryDirectory() as tmp_dir:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(__file__).parent.parent / "server.py"),
                "--port",
                str(port),
                "--data-dir",
                tmp_dir,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for_server(host, port)
        except RuntimeError:
            proc.kill()
            raise
        yield host, port, tmp_dir
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture()
def livedoc_socket(livedoc_server):
    """Return a raw socket connected to the server, plus a helper to send/recv."""
    host, port, _ = livedoc_server
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect((host, port))
    yield sock
    try:
        sock.close()
    except Exception:
        pass


@pytest.fixture()
def livedoc_client(livedoc_server):
    """Start a Livedoc client connected to the test server.

    Yields a Client instance and closes it on teardown.
    """
    from livedoc_client import Client

    host, port, _ = livedoc_server
    client = Client(host, port, doc="test", name="test-client")
    yield client
    client.close()
