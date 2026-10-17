"""Tests for the Livedoc client library."""

import asyncio
import json
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from livedoc_client import Client


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 5.0) -> None:
    import socket
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"Server did not start on port {port}")


@pytest.fixture()
def server_and_port():
    """Start a Livedoc server. Yields (port, data_dir)."""
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="livedoc_client_test_")

    proc = subprocess.Popen(
        [sys.executable, "/home/user/work/server.py", "--port", str(port), "--data-dir", data_dir],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    _wait_for_server(port, timeout=5.0)

    yield port, data_dir

    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    import shutil
    shutil.rmtree(data_dir, ignore_errors=True)


class TestSingleClient:
    def test_single_client_edits_sync(self, server_and_port):
        port, _ = server_and_port
        c = Client("127.0.0.1", port, "test1", "alice")
        assert c.text == ""
        assert c.rev == 0

        c.insert(0, "hello")
        assert c.wait_synced(timeout=5.0)
        assert c.text == "hello"
        assert c.rev == 1

        c.delete(0, 5)
        assert c.wait_synced(timeout=5.0)
        assert c.text == ""
        assert c.rev == 2

        c.close()

    def test_multiple_edits_sync(self, server_and_port):
        port, _ = server_and_port
        c = Client("127.0.0.1", port, "test2", "alice")
        c.insert(0, "hello")
        c.wait_synced()
        c.insert(5, " world")
        c.wait_synced()
        assert c.text == "hello world"
        c.close()

    def test_value_error_on_invalid_edit(self, server_and_port):
        port, _ = server_and_port
        c = Client("127.0.0.1", port, "test3", "alice")
        c.insert(0, "abc")
        c.wait_synced()
        with pytest.raises(ValueError):
            c.delete(0, 10)  # out of range
        with pytest.raises(ValueError):
            c.insert(10, "x")  # out of range
        with pytest.raises(ValueError):
            c.delete(0, 0)  # len < 1
        c.close()


class TestConcurrentClients:
    def test_concurrent_edits_converge(self, server_and_port):
        port, _ = server_and_port
        docs = []

        def make_client(name):
            c = Client("127.0.0.1", port, "concurrent1", name)
            docs.append(c)
            return c

        c1 = make_client("alice")
        c2 = make_client("bob")

        # Both insert at position 0
        c1.insert(0, "A")
        c2.insert(0, "B")

        c1.wait_synced(timeout=5.0)
        c2.wait_synced(timeout=5.0)

        # Both should have the same text (order may vary)
        assert c1.text == c2.text, f"{c1.text!r} != {c2.text!r}"
        assert len(c1.text) == 2

        for c in docs:
            c.close()


class TestClientErrors:
    def test_join_timeout(self):
        port = _free_port()  # unused port
        with pytest.raises((ConnectionError, TimeoutError, OSError)):
            Client("127.0.0.1", port, "test", "alice", timeout=0.5)
