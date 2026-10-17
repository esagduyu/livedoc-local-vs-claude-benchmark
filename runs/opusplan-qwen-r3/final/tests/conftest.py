"""Shared pytest fixtures for Livedoc tests."""

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture()
def livedoc_server():
    """Start a Livedoc server subprocess on a free port with a temp data dir.

    Yields (port, data_dir). Kills the server on teardown.
    """
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="livedoc_test_")

    proc = subprocess.Popen(
        [sys.executable, "-m", "server_runner", "--port", str(port), "--data-dir", data_dir],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait until server accepts connections
    _wait_for_server(port, timeout=5.0)

    yield port, data_dir

    # Teardown: kill server
    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
    # Clean up data dir
    import shutil
    shutil.rmtree(data_dir, ignore_errors=True)


def _free_port() -> int:
    """Find a free TCP port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 5.0) -> None:
    """Wait until the server accepts TCP connections."""
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
    raise RuntimeError(f"Server did not start on port {port} within {timeout}s")
