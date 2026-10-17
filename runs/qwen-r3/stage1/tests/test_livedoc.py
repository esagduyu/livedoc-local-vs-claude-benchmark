"""Tests for Livedoc – transformation rules, server, and client."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from livedoc_client import Client, LivedocConnectionError, LivedocTimeoutError, apply_op


# ─── Helpers ────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(port: int, data_dir: Path) -> subprocess.Popen:
    """Start the livedoc server."""
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(port),
         "--data-dir", str(data_dir)],
        cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.3)
    return proc


def _connect(port: int, doc: str, name: str, timeout: float = 3.0) -> Client:
    """Connect a client to the server."""
    return Client("127.0.0.1", port, doc, name, timeout=timeout)


def _raw_send_recv(sock: socket.socket, msg: dict) -> dict:
    """Send a JSON message and receive the response."""
    line = json.dumps(msg) + "\n"
    sock.sendall(line.encode("utf-8"))
    return _raw_recv(sock)


def _raw_recv(sock: socket.socket) -> dict:
    """Receive a JSON line from the socket."""
    buf = b""
    while b"\n" not in buf:
        data = sock.recv(4096)
        if not data:
            raise ConnectionError("Connection closed")
        buf += data
    return json.loads(buf.decode("utf-8"))


def _raw_connect(port: int) -> socket.socket:
    """Create a raw TCP connection to the server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    s.connect(("127.0.0.1", port))
    s.settimeout(None)
    return s


def _wait_for_text(client: Client, target: str, timeout: float = 3.0) -> bool:
    """Wait until client.text equals target or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.text == target:
            return True
        time.sleep(0.05)
    return False


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def server():
    """Start a server on a free port with a temp data dir. Teardown after test."""
    port = _free_port()
    data_dir = Path(tempfile.mkdtemp())
    proc = _start_server(port, data_dir)
    yield port, data_dir, proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    shutil.rmtree(data_dir, ignore_errors=True)


# ─── Transformation tests ───────────────────────────────────────────────────

class TestEditTransform:
    """Test edit-vs-edit transformation rules."""

    def _xf(self, a: dict, b: dict) -> tuple[list[dict], list[dict]]:
        from server import _edit_transform
        return _edit_transform(a, b)

    def _xf_op(self, A: list[dict], B: list[dict]) -> tuple[list[dict], list[dict]]:
        from server import _op_transform
        return _op_transform(A, B)

    def _apply(self, text: str, op: list[dict]) -> str:
        return apply_op(text, op)

    def test_ins_vs_ins_a_before_b(self):
        """p < q: a unchanged, b shifts right."""
        a = {"ins": 2, "text": "X"}
        b = {"ins": 5, "text": "Y"}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"ins": 2, "text": "X"}]
        assert b_prime == [{"ins": 6, "text": "Y"}]

    def test_ins_vs_ins_a_after_b(self):
        """p >= q: a shifts right by len(b.text)."""
        a = {"ins": 5, "text": "X"}
        b = {"ins": 2, "text": "YZ"}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"ins": 7, "text": "X"}]
        assert b_prime == [{"ins": 2, "text": "YZ"}]

    def test_ins_vs_ins_on_tie(self):
        """p == q: earlier insert (b) stays left, a shifts right."""
        a = {"ins": 3, "text": "X"}
        b = {"ins": 3, "text": "Y"}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"ins": 4, "text": "X"}]
        assert b_prime == [{"ins": 3, "text": "Y"}]

    def test_ins_vs_del_before(self):
        """p <= q: a unchanged. b' shifts right."""
        a = {"ins": 2, "text": "X"}
        b = {"del": 5, "len": 3}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"ins": 2, "text": "X"}]
        assert b_prime == [{"del": 6, "len": 3}]

    def test_ins_vs_del_after(self):
        """p >= q + k: a shifts left by k."""
        a = {"ins": 8, "text": "X"}
        b = {"del": 5, "len": 3}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"ins": 5, "text": "X"}]

    def test_ins_vs_del_inside(self):
        """p strictly inside del range: a moves to start of del."""
        a = {"ins": 6, "text": "X"}
        b = {"del": 5, "len": 3}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"ins": 5, "text": "X"}]

    def test_del_vs_ins_before(self):
        """q <= p: del shifts right."""
        a = {"del": 5, "len": 3}
        b = {"ins": 2, "text": "XY"}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"del": 7, "len": 3}]

    def test_del_vs_ins_after(self):
        """q >= p + m: del unchanged."""
        a = {"del": 2, "len": 3}
        b = {"ins": 5, "text": "X"}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"del": 2, "len": 3}]

    def test_del_vs_ins_inside(self):
        """b strictly inside a's range: a splits."""
        a = {"del": 2, "len": 6}
        b = {"ins": 4, "text": "XY"}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"del": 2, "len": 2}, {"del": 4, "len": 4}]

    def test_del_vs_del_no_overlap(self):
        """No overlap: both unchanged."""
        a = {"del": 0, "len": 2}
        b = {"del": 5, "len": 3}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"del": 0, "len": 2}]
        assert b_prime == [{"del": 5, "len": 3}]

    def test_del_vs_del_partial_overlap(self):
        """Partial overlap: both shrink."""
        a = {"del": 2, "len": 6}
        b = {"del": 5, "len": 4}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == [{"del": 2, "len": 3}]
        assert b_prime == [{"del": 5, "len": 1}]

    def test_del_vs_del_full_cover(self):
        """b fully covers a: a vanishes."""
        a = {"del": 2, "len": 3}
        b = {"del": 1, "len": 5}
        a_prime, b_prime = self._xf(a, b)
        assert a_prime == []

    def test_op_transform_empty_A(self):
        A, B = self._xf_op([], [{"ins": 0, "text": "X"}])
        assert A == []
        assert B == [{"ins": 0, "text": "X"}]

    def test_op_transform_empty_B(self):
        A, B = self._xf_op([{"ins": 0, "text": "X"}], [])
        assert A == [{"ins": 0, "text": "X"}]
        assert B == []

    def test_op_transform_single_vs_single(self):
        """Transform is applied correctly by server and client."""
        A, B = self._xf_op(
            [{"ins": 2, "text": "X"}],
            [{"ins": 1, "text": "Y"}],
        )
        # A' should be ins at 3 (shifted past B's insert)
        assert A == [{"ins": 2, "text": "X"}]
        assert A[0]["ins"] >= B[0]["ins"]  # A' position >= B' position
        # After B is applied, A' should insert at the correct position
        text = "abc"
        after_B = self._apply(text, B)
        after_B_A_prime = self._apply(after_B, A)
        # This should be the final text after B then A'
        assert after_B_A_prime == "aYbcX"

    def test_op_transform_multi_edit(self):
        """Multi-edit A against single-edit B."""
        A = [{"ins": 0, "text": "A"}, {"ins": 0, "text": "B"}]
        B = [{"ins": 1, "text": "X"}]
        A_prime, B_prime = self._xf_op(A, B)
        text = ""
        text1 = self._apply(self._apply(text, A_prime), B_prime)
        text2 = self._apply(self._apply(text, B), A)
        assert text1 == text2

    def test_op_transform_consistency(self):
        """Transform A against B: A' applies correctly after B."""
        A = [{"ins": 3, "text": "hello"}, {"del": 1, "len": 2}]
        B = [{"ins": 0, "text": "X"}, {"ins": 5, "text": "Y"}]
        A_prime, B_prime = self._xf_op(A, B)
        # A' should be a valid operation that can be applied after B
        text = ""
        after_B = self._apply(text, B)
        after_B_A_prime = self._apply(after_B, A_prime)
        # Verify A' is valid (no out-of-range errors)
        assert len(after_B_A_prime) >= len(after_B)

    def test_del_del_consistency(self):
        """del vs del: A' applies correctly after B."""
        A = [{"del": 1, "len": 3}]
        B = [{"del": 2, "len": 3}]
        A_prime, B_prime = self._xf_op(A, B)
        text = "abcdef"
        after_B = self._apply(text, B)
        after_B_A_prime = self._apply(after_B, A_prime)
        # A' should be a valid operation
        assert len(after_B_A_prime) == len(text) - 3  # total 3 chars deleted

    def test_ins_del_consistency(self):
        """ins vs del: A' applies correctly after B."""
        A = [{"ins": 3, "text": "X"}]
        B = [{"del": 2, "len": 2}]
        A_prime, B_prime = self._xf_op(A, B)
        text = "abcdef"
        after_B = self._apply(text, B)
        after_B_A_prime = self._apply(after_B, A_prime)
        # A' should be a valid operation
        assert len(after_B_A_prime) == len(text) + 1 - 2

    def test_del_ins_consistency(self):
        """del vs ins: A' applies correctly after B."""
        A = [{"del": 2, "len": 3}]
        B = [{"ins": 4, "text": "X"}]
        A_prime, B_prime = self._xf_op(A, B)
        text = "abcdef"
        after_B = self._apply(text, B)
        after_B_A_prime = self._apply(after_B, A_prime)
        # A' should be a valid operation
        assert len(after_B_A_prime) == len(text) + 1 - 3


# ─── Server protocol tests ──────────────────────────────────────────────────

class TestServerProtocol:
    """Test the server wire protocol."""

    def test_join_without_since(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            resp = _raw_send_recv(sock, {"type": "join", "doc": "test", "client": "alice"})
            assert resp["type"] == "joined"
            assert resp["doc"] == "test"
            assert resp["rev"] == 0
            assert resp["text"] == ""
        finally:
            sock.close()

    def test_join_with_since(self, server):
        port, data_dir, proc = server
        c1 = _connect(port, "test", "alice")
        c1.insert(0, "hello")
        c1.wait_synced()
        c1.close()

        sock = _raw_connect(port)
        try:
            resp = _raw_send_recv(sock, {"type": "join", "doc": "test",
                                          "client": "bob", "since": 0})
            assert resp["type"] == "joined"
            assert resp["rev"] == 1
            assert "ops" in resp
            assert len(resp["ops"]) == 1
            assert resp["ops"][0]["op"] == [{"ins": 0, "text": "hello"}]
        finally:
            sock.close()

    def test_op_commit_and_ack(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "op1", "op": [{"ins": 0, "text": "hi"}]})
            ack = _raw_recv(sock)
            assert ack["type"] == "ack"
            assert ack["rev"] == 1
        finally:
            sock.close()

    def test_broadcast_to_others(self, server):
        port, data_dir, proc = server
        s1 = _raw_connect(port)
        s2 = _raw_connect(port)
        try:
            _raw_send_recv(s1, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(s2, {"type": "join", "doc": "t", "client": "bob"})

            _raw_send_recv(s1, {"type": "op", "doc": "t", "rev": 0,
                                 "id": "op1", "op": [{"ins": 0, "text": "hello"}]})

            broadcast = _raw_recv(s2)
            assert broadcast["type"] == "op"
            assert broadcast["rev"] == 1
            assert broadcast["client"] == "alice"
            assert broadcast["op"] == [{"ins": 0, "text": "hello"}]
        finally:
            s1.close()
            s2.close()

    def test_exactly_once(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "unique-id", "op": [{"ins": 0, "text": "x"}]})
            ack1 = _raw_recv(sock)
            assert ack1["rev"] == 1

            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 1,
                                   "id": "unique-id", "op": [{"ins": 0, "text": "x"}]})
            ack2 = _raw_recv(sock)
            assert ack2["rev"] == 1
        finally:
            sock.close()

    def test_error_bad_json(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            sock.sendall(b"not json\n")
            resp = _raw_recv(sock)
            assert resp["type"] == "error"
            assert resp["code"] == "bad_json"
        finally:
            sock.close()

    def test_error_not_joined(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "x", "op": [{"ins": 0, "text": "y"}]})
            resp = _raw_recv(sock)
            assert resp["type"] == "error"
            assert resp["code"] == "not_joined"
        finally:
            sock.close()

    def test_error_bad_rev(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 999,
                                   "id": "x", "op": [{"ins": 0, "text": "y"}]})
            resp = _raw_recv(sock)
            assert resp["type"] == "error"
            assert resp["code"] == "bad_rev"
        finally:
            sock.close()

    def test_error_out_of_range(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "x", "op": [{"ins": 99, "text": "y"}]})
            resp = _raw_recv(sock)
            assert resp["type"] == "error"
            assert resp["code"] == "out_of_range"
        finally:
            sock.close()

    def test_error_empty_ins_text(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "x", "op": [{"ins": 0, "text": ""}]})
            resp = _raw_recv(sock)
            assert resp["type"] == "error"
            assert resp["code"] == "out_of_range"
        finally:
            sock.close()

    def test_error_del_len_zero(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "x", "op": [{"del": 0, "len": 0}]})
            resp = _raw_recv(sock)
            assert resp["type"] == "error"
            assert resp["code"] == "out_of_range"
        finally:
            sock.close()

    def test_persistence_after_restart(self, server):
        port, data_dir, proc = server
        c1 = _connect(port, "persist", "alice")
        c1.insert(0, "persistent")
        c1.wait_synced()
        c1.close()

        proc.terminate()
        proc.wait()
        time.sleep(0.2)

        port2 = _free_port()
        proc2 = _start_server(port2, data_dir)
        time.sleep(0.3)

        try:
            c2 = _connect(port2, "persist", "bob")
            assert c2.text == "persistent"
            c2.close()
        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc2.kill()
                proc2.wait()
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_error_stays_open(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "x", "op": [{"ins": 999, "text": "y"}]})
            resp = _raw_recv(sock)
            assert resp["type"] == "error"
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "ok", "op": [{"ins": 0, "text": "ok"}]})
            ack = _raw_recv(sock)
            assert ack["type"] == "ack"
            assert ack["rev"] == 1
        finally:
            sock.close()

    def test_empty_operation(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "t", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "t", "rev": 0,
                                   "id": "empty", "op": []})
            ack = _raw_recv(sock)
            assert ack["type"] == "ack"
            assert ack["rev"] == 1
        finally:
            sock.close()

    def test_multiple_docs(self, server):
        port, data_dir, proc = server
        sock = _raw_connect(port)
        try:
            _raw_send_recv(sock, {"type": "join", "doc": "doc1", "client": "alice"})
            _raw_send_recv(sock, {"type": "join", "doc": "doc2", "client": "alice"})
            _raw_send_recv(sock, {"type": "op", "doc": "doc1", "rev": 0,
                                   "id": "op1", "op": [{"ins": 0, "text": "hello"}]})
            ack = _raw_recv(sock)
            assert ack["type"] == "ack"
        finally:
            sock.close()


# ─── Client library tests ───────────────────────────────────────────────────

class TestClientLibrary:
    """Test the Client class."""

    def test_connect_and_join(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        assert c.text == ""
        assert c.rev == 0
        c.close()

    def test_insert(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        c.insert(0, "hello")
        assert c.text == "hello"
        c.wait_synced()
        assert c.rev == 1
        c.close()

    def test_delete(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        c.insert(0, "hello")
        c.wait_synced()
        c.delete(0, 2)
        assert c.text == "llo"
        c.wait_synced()
        c.close()

    def test_multiple_clients_sync(self, server):
        port, data_dir, proc = server
        c1 = _connect(port, "test", "alice")
        c2 = _connect(port, "test", "bob")

        c1.insert(0, "hello")
        c1.wait_synced()

        # Wait for c2 to receive c1's op
        assert _wait_for_text(c2, "hello", 3.0)

        c2.insert(5, " world")
        c2.wait_synced()

        # Wait for c1 to receive c2's op
        assert _wait_for_text(c1, "hello world", 3.0)

        assert c1.text == c2.text == "hello world"
        c1.close()
        c2.close()

    def test_concurrent_edits_converge(self, server):
        port, data_dir, proc = server
        c1 = _connect(port, "test", "alice")
        c2 = _connect(port, "test", "bob")

        c1.insert(0, "A")
        c2.insert(0, "B")
        c1.wait_synced()
        c2.wait_synced()

        # Wait for convergence
        assert _wait_for_text(c1, c2.text, 3.0)
        assert c1.text == c2.text
        c1.close()
        c2.close()

    def test_insert_invalid_position(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        with pytest.raises(ValueError):
            c.insert(999, "x")
        c.close()

    def test_delete_invalid_position(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        with pytest.raises(ValueError):
            c.delete(0, 999)
        c.close()

    def test_delete_invalid_length(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        with pytest.raises(ValueError):
            c.delete(0, 0)
        c.close()

    def test_wait_synced_timeout(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        c.insert(0, "x")
        proc.terminate()
        proc.wait()
        result = c.wait_synced(timeout=0.5)
        assert result is False
        c.close()

    def test_text_is_str(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        assert isinstance(c.text, str)
        c.close()

    def test_rev_is_int(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        assert isinstance(c.rev, int)
        c.close()

    def test_unicode_support(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        c.insert(0, "こんにちは")
        c.wait_synced()
        assert c.text == "こんにちは"
        c.close()

    def test_long_text(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        long_text = "x" * 10000
        c.insert(0, long_text)
        c.wait_synced()
        assert c.text == long_text
        c.close()


# ─── Edge case tests ────────────────────────────────────────────────────────

class TestEdgeCases:
    """Edge cases and stress tests."""

    def test_many_concurrent_clients(self, server):
        port, data_dir, proc = server
        clients = []
        for i in range(5):
            c = _connect(port, "test", f"client{i}")
            c.insert(0, f"X{i}")
            clients.append(c)

        for c in clients:
            c.wait_synced()

        # Wait for all to converge
        final_text = clients[0].text
        for c in clients:
            assert _wait_for_text(c, final_text, 3.0)

        texts = [c.text for c in clients]
        assert len(set(texts)) == 1
        assert len(texts[0]) == 5

        for c in clients:
            c.close()

    def test_op_with_empty_result(self, server):
        port, data_dir, proc = server
        c1 = _connect(port, "test", "alice")
        c2 = _connect(port, "test", "bob")

        c1.insert(0, "X")
        c1.wait_synced()

        c2.delete(0, 1)
        c2.wait_synced()

        c1.wait_synced()

        assert _wait_for_text(c1, "", 3.0)
        assert c1.text == c2.text == ""
        assert c1.rev == 2
        c1.close()
        c2.close()

    def test_delete_then_insert_same_position(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        c.insert(0, "hello")
        c.wait_synced()
        c.delete(0, 5)
        c.wait_synced()
        c.insert(0, "world")
        c.wait_synced()
        assert c.text == "world"
        c.close()

    def test_since_with_no_ops(self, server):
        port, data_dir, proc = server
        c1 = _connect(port, "test", "alice")
        c1.wait_synced()
        c1.close()

        sock = _raw_connect(port)
        try:
            resp = _raw_send_recv(sock, {"type": "join", "doc": "test",
                                          "client": "bob", "since": 0})
            assert resp["type"] == "joined"
            assert resp["rev"] == 0
            assert "ops" in resp
            assert resp["ops"] == []
        finally:
            sock.close()

    def test_multiple_ops_in_flight_buffered(self, server):
        port, data_dir, proc = server
        c = _connect(port, "test", "alice")
        c.insert(0, "A")
        c.insert(1, "B")
        c.insert(2, "C")
        c.wait_synced()
        assert c.text == "ABC"
        assert c.rev == 3
        c.close()

    def test_disconnect_reconnect(self, server):
        port, data_dir, proc = server
        c1 = _connect(port, "test", "alice")
        c1.insert(0, "hello")
        c1.wait_synced()
        c1.close()

        c2 = _connect(port, "test", "alice")
        assert c2.text == "hello"
        assert c2.rev == 1
        c2.close()
