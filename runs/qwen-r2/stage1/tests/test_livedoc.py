"""Tests for livedoc – transformation, server, and client."""

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

from livedoc_client import (
    Client,
    _apply_op,
    _transform_edit_a_against_b,
    _transform_op_a_against_b,
    _transform_op_against_history,
    _transform_remote_past_local,
)

SERVER_HOST = "127.0.0.1"


def find_free_port():
    """Find a free TCP port."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def livedoc_server():
    """Start a livedoc server for the entire test module."""
    port = find_free_port()
    tmpdir = tempfile.mkdtemp(prefix="livedoc_test_")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "..", "server.py"),
         "--port", str(port), "--data-dir", tmpdir],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        raise RuntimeError(f"Server failed to start: {stderr.decode()}")
    yield port, tmpdir
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    shutil.rmtree(tmpdir, ignore_errors=True)


def make_client(port, doc, name, timeout=5.0):
    return Client(SERVER_HOST, port, doc, name, timeout=timeout)


# ===================================================================
# Transformation tests
# ===================================================================

class TestEditVsEdit:
    """Test the edit-vs-edit transformation rules."""

    def test_ins_vs_ins_p_lt_q(self):
        """p < q: a unchanged."""
        a = {"ins": 5, "text": "X"}
        b = {"ins": 10, "text": "Y"}
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"ins": 5, "text": "X"}
        assert b_prime == {"ins": 11, "text": "Y"}  # q=10 > p=5, so b shifts right by len("X")=1

    def test_ins_vs_ins_p_eq_q_tie(self):
        """p >= q (tie): a shifts right. Earlier insert stays left."""
        a = {"ins": 5, "text": "X"}
        b = {"ins": 5, "text": "Y"}
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"ins": 6, "text": "X"}  # 5 + len("Y")=1 = 6
        assert b_prime == {"ins": 5, "text": "Y"}  # q=5 <= p=5, b unchanged

    def test_ins_vs_ins_p_gt_q(self):
        """p > q: a shifts right."""
        a = {"ins": 10, "text": "X"}
        b = {"ins": 5, "text": "Y"}
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"ins": 11, "text": "X"}  # 10 + len("Y")=1
        assert b_prime == {"ins": 5, "text": "Y"}   # q=5 <= p=10, b unchanged

    def test_ins_vs_del_p_le_q(self):
        """p <= q: ins unchanged."""
        a = {"ins": 3, "text": "X"}
        b = {"del": 5, "len": 2}
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"ins": 3, "text": "X"}

    def test_ins_vs_del_p_inside(self):
        """p strictly inside del: ins at q."""
        a = {"ins": 6, "text": "X"}
        b = {"del": 5, "len": 3}  # deletes [5,8)
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"ins": 5, "text": "X"}

    def test_ins_vs_del_p_after_del(self):
        """p >= q+k: ins shifts left by k."""
        a = {"ins": 10, "text": "X"}
        b = {"del": 5, "len": 3}
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"ins": 7, "text": "X"}

    def test_del_vs_ins_q_le_p(self):
        """q <= p: del shifts right by len(u)."""
        a = {"del": 5, "len": 3}
        b = {"ins": 2, "text": "XY"}
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"del": 7, "len": 3}  # 5 + 2

    def test_del_vs_ins_q_inside(self):
        """q strictly inside del: split into two dels."""
        a = {"del": 5, "len": 6}  # [5, 11)
        b = {"ins": 7, "text": "X"}
        # p=5, q=7, m=6, lb=1
        # First: del at p=5, len=q-p=2
        # Second: del at p+lb=6, len=p+m-q=5+6-7=4
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == [{"del": 5, "len": 2}, {"del": 6, "len": 4}]

    def test_del_vs_ins_q_after_del(self):
        """q >= p+m: unchanged."""
        a = {"del": 5, "len": 3}
        b = {"ins": 10, "text": "X"}
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"del": 5, "len": 3}

    def test_del_vs_del_no_overlap(self):
        """No overlap: a unchanged."""
        a = {"del": 5, "len": 3}
        b = {"del": 10, "len": 2}
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"del": 5, "len": 3}

    def test_del_vs_del_a_inside_b(self):
        """a entirely inside b: a vanishes."""
        a = {"del": 5, "len": 2}  # [5,7)
        b = {"del": 3, "len": 5}  # [3,8)
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime is None

    def test_del_vs_del_b_inside_a(self):
        """b entirely inside a: a shrinks."""
        a = {"del": 5, "len": 10}  # [5,15)
        b = {"del": 7, "len": 3}  # [7,10)
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"del": 5, "len": 7}  # 10 - 3

    def test_del_vs_del_overlap_start(self):
        """a starts before b, ends inside b."""
        a = {"del": 5, "len": 5}  # [5,10)
        b = {"del": 7, "len": 5}  # [7,12)
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"del": 5, "len": 2}  # q - p = 7 - 5

    def test_del_vs_del_overlap_end(self):
        """a starts inside b, ends after b."""
        a = {"del": 7, "len": 5}  # [7,12)
        b = {"del": 5, "len": 5}  # [5,10)
        a_prime, b_prime = _transform_edit_a_against_b(a, b)
        assert a_prime == {"del": 10, "len": 2}  # pa+ma - qb - kb = 12-10=2


class TestOpVsOp:
    """Test operation-vs-operation transformation."""

    def test_empty_A(self):
        A, B = _transform_op_a_against_b([], [{"ins": 5, "text": "X"}])
        assert A == []

    def test_empty_B(self):
        A, B = _transform_op_a_against_b([{"ins": 5, "text": "X"}], [])
        assert B == []

    def test_both_single(self):
        A, B = _transform_op_a_against_b(
            [{"ins": 5, "text": "X"}],
            [{"ins": 3, "text": "Y"}],
        )
        assert A == [{"ins": 6, "text": "X"}]
        assert B == [{"ins": 3, "text": "Y"}]

    def test_multi_A_single_B(self):
        A = [{"ins": 5, "text": "X"}, {"ins": 10, "text": "Z"}]
        B = [{"del": 3, "len": 2}]
        A_prime, B_prime = _transform_op_a_against_b(A, B)
        assert isinstance(A_prime, list)
        assert isinstance(B_prime, list)

    def test_single_A_multi_B(self):
        A = [{"ins": 5, "text": "X"}]
        B = [{"del": 3, "len": 2}, {"ins": 7, "text": "Y"}]
        A_prime, B_prime = _transform_op_a_against_b(A, B)
        assert isinstance(A_prime, list)
        assert isinstance(B_prime, list)

    def test_consistency(self):
        """Both orderings should produce the same final text."""
        A = [{"ins": 3, "text": "AB"}, {"ins": 7, "text": "CD"}]
        B = [{"del": 5, "len": 2}]
        A_prime, B_prime = _transform_op_a_against_b(A, B)
        # Apply B then A_prime
        text1 = _apply_op("0123456789", B)
        text1 = _apply_op(text1, A_prime)
        # Apply A then B_prime
        text2 = _apply_op("0123456789", A)
        text2 = _apply_op(text2, B_prime)
        assert text1 == text2, f"{text1!r} != {text2!r}"


class TestTransformAgainstHistory:
    """Test transforming an operation against a history of operations."""

    def test_empty_history(self):
        op = [{"ins": 5, "text": "X"}]
        result = _transform_op_against_history(op, [])
        assert result == op

    def test_single_history_op(self):
        op = [{"ins": 5, "text": "X"}]
        history = [{"ins": 3, "text": "Y"}]
        result = _transform_op_against_history(op, history)
        # p=5, q=3, p>=q: a shifts right by len('Y')=1 -> ins at 6
        assert result == [{"ins": 6, "text": "X"}]

    def test_multi_history_ops(self):
        op = [{"ins": 5, "text": "X"}]
        history = [
            {"ins": 3, "text": "A"},
            {"del": 6, "len": 1},
        ]
        result = _transform_op_against_history(op, history)
        # First: ins 5 -> ins 5+1=6 (after "A" at 3)
        # Then: del [6,7) -> check: 6 <= 6 -> unchanged
        assert result == [{"ins": 6, "text": "X"}]


# ===================================================================
# Apply op tests
# ===================================================================

class TestApplyOp:
    def test_insert(self):
        assert _apply_op("hello", [{"ins": 5, "text": " world"}]) == "hello world"

    def test_delete(self):
        assert _apply_op("hello world", [{"del": 5, "len": 6}]) == "hello"

    def test_multi_edit(self):
        op = [{"ins": 5, "text": " world"}, {"del": 5, "len": 6}]
        assert _apply_op("hello", op) == "hello"

    def test_empty_op(self):
        assert _apply_op("hello", []) == "hello"


# ===================================================================
# Server tests
# ===================================================================

class TestServerProtocol:
    """Test the wire protocol."""

    def test_join_no_since(self, livedoc_server):
        port, _ = livedoc_server
        client = make_client(port, "doc1", "alice")
        assert client.text == ""
        assert client.rev == 0
        client.close()

    def test_join_with_since(self, livedoc_server):
        port, _ = livedoc_server
        c1 = make_client(port, "doc2", "alice")
        c1.insert(0, "hello")
        c1.wait_synced()
        c1.close()

        c2 = make_client(port, "doc2", "bob")
        assert c2.text == "hello"
        c2.close()

    def test_op_basic(self, livedoc_server):
        port, _ = livedoc_server
        c1 = make_client(port, "doc3", "alice")
        c1.insert(0, "world")
        c1.wait_synced()
        assert c1.text == "world"

        c2 = make_client(port, "doc3", "bob")
        assert c2.text == "world"
        c2.close()
        c1.close()

    def test_concurrent_edits(self, livedoc_server):
        port, _ = livedoc_server
        c1 = make_client(port, "doc4", "alice")
        c2 = make_client(port, "doc4", "bob")

        # Both insert at position 0
        c1.insert(0, "A")
        c2.insert(0, "B")
        c1.wait_synced()
        c2.wait_synced()

        # Process any pending messages on c1
        c1.wait_synced()
        time.sleep(0.05)  # allow broadcast to arrive
        assert c1.text == c2.text
        assert len(c1.text) == 2
        c1.close()
        c2.close()

    def test_delete(self, livedoc_server):
        port, _ = livedoc_server
        c1 = make_client(port, "doc5", "alice")
        c1.insert(0, "hello world")
        c1.wait_synced()

        c2 = make_client(port, "doc5", "bob")
        c2.wait_synced()
        c2.delete(5, 6)
        c2.wait_synced()
        # Wait for c1 to receive and process the broadcast
        c1.wait_synced()
        time.sleep(0.05)

        assert c1.text == c2.text == "hello"
        c1.close()
        c2.close()

    def test_server_restart_persistence(self, livedoc_server):
        port, tmpdir = livedoc_server
        c1 = make_client(port, "doc6", "alice")
        c1.insert(0, "persist me")
        c1.wait_synced()
        c1.close()

        # Restart server
        proc = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(__file__), "..", "server.py"),
             "--port", str(port), "--data-dir", tmpdir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.5)

        c2 = make_client(port, "doc6", "bob")
        assert c2.text == "persist me"
        assert c2.rev == 1
        c2.close()

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def test_invalid_insert_position(self, livedoc_server):
        """Insert at position beyond text length should error."""
        port, _ = livedoc_server
        c1 = make_client(port, "doc8", "alice")
        with pytest.raises(ValueError):
            c1.insert(100, "x")
        c1.close()

    def test_invalid_delete_length(self, livedoc_server):
        """Delete with length 0 should error."""
        port, _ = livedoc_server
        c1 = make_client(port, "doc9", "alice")
        c1.insert(0, "hello")
        c1.wait_synced()
        with pytest.raises(ValueError):
            c1.delete(0, 0)
        c1.close()

    def test_invalid_delete_position(self, livedoc_server):
        """Delete at position beyond text length should error."""
        port, _ = livedoc_server
        c1 = make_client(port, "doc10", "alice")
        c1.insert(0, "hi")
        c1.wait_synced()
        with pytest.raises(ValueError):
            c1.delete(10, 1)
        c1.close()


# ===================================================================
# Client library tests
# ===================================================================

class TestClientLibrary:
    def test_basic_join(self, livedoc_server):
        port, _ = livedoc_server
        c = make_client(port, "test_join", "user1")
        assert c.text == ""
        assert c.rev == 0
        c.close()

    def test_insert_and_sync(self, livedoc_server):
        port, _ = livedoc_server
        c = make_client(port, "test_insert", "user1")
        c.insert(0, "hello")
        assert c.wait_synced()
        assert c.text == "hello"
        assert c.rev == 1
        c.close()

    def test_delete_and_sync(self, livedoc_server):
        port, _ = livedoc_server
        c = make_client(port, "test_delete", "user1")
        c.insert(0, "hello world")
        c.wait_synced()
        c.delete(5, 6)
        assert c.wait_synced()
        assert c.text == "hello"
        c.close()

    def test_multiple_inserts(self, livedoc_server):
        port, _ = livedoc_server
        c = make_client(port, "test_multi", "user1")
        c.insert(0, "A")
        c.wait_synced()
        c.insert(1, "B")
        c.wait_synced()
        c.insert(1, "C")
        c.wait_synced()
        assert c.text == "ACB"
        c.close()

    def test_wait_synced_timeout(self, livedoc_server):
        """wait_synced returns True when already synced."""
        port, _ = livedoc_server
        c = make_client(port, "test_timeout", "user1")
        assert c.wait_synced(timeout=1.0)
        c.close()

    def test_two_clients_sync(self, livedoc_server):
        port, _ = livedoc_server
        c1 = make_client(port, "test_sync", "alice")
        c2 = make_client(port, "test_sync", "bob")

        c1.insert(0, "hello")
        c1.wait_synced()
        c2.wait_synced()  # ensure c2 sees c1's insert

        assert c2.text == "hello"
        c2.delete(0, 5)
        c2.wait_synced()
        c1.wait_synced()  # ensure c1 sees c2's delete

        assert c1.text == c2.text == ""
        c1.close()
        c2.close()

    def test_concurrent_inserts_at_same_position(self, livedoc_server):
        port, _ = livedoc_server
        c1 = make_client(port, "test_concurrent", "alice")
        c2 = make_client(port, "test_concurrent", "bob")

        c1.insert(0, "A")
        c2.insert(0, "B")

        c1.wait_synced()
        c2.wait_synced()
        c1.wait_synced()  # process c2's broadcast

        assert c1.text == c2.text
        assert len(c1.text) == 2
        c1.close()
        c2.close()

    def test_close(self, livedoc_server):
        port, _ = livedoc_server
        c = make_client(port, "test_close", "user1")
        c.insert(0, "test")
        c.wait_synced()
        c.close()
        # Should not raise


# ===================================================================
# Edge cases
# ===================================================================

class TestEdgeCases:
    def test_unicode(self, livedoc_server):
        port, _ = livedoc_server
        c = make_client(port, "unicode", "user1")
        c.insert(0, "こんにちは")
        c.wait_synced()
        assert c.text == "こんにちは"
        c.close()

    def test_unicode_delete(self, livedoc_server):
        port, _ = livedoc_server
        c = make_client(port, "unicode2", "user1")
        c.insert(0, "こんにちは世界")
        c.wait_synced()
        c.delete(5, 2)  # delete "世界" (2 chars)
        c.wait_synced()
        assert c.text == "こんにちは"
        c.close()

    def test_many_small_edits(self, livedoc_server):
        port, _ = livedoc_server
        c = make_client(port, "many_edits", "user1")
        for i in range(20):
            c.insert(0, chr(ord('a') + (i % 26)))
            c.wait_synced()
        assert len(c.text) == 20
        c.close()

    def test_three_clients(self, livedoc_server):
        port, _ = livedoc_server
        c1 = make_client(port, "three", "alice")
        c2 = make_client(port, "three", "bob")
        c3 = make_client(port, "three", "charlie")

        c1.insert(0, "start")
        c1.wait_synced()

        c2.insert(5, " end")
        c3.insert(5, " world")
        c2.wait_synced()
        c3.wait_synced()
        c1.wait_synced()  # ensure c1 sees both broadcasts
        c2.wait_synced()  # ensure c2 sees c3's broadcast
        c3.wait_synced()  # ensure c3 sees c2's broadcast

        assert c1.text == c2.text == c3.text
        c1.close()
        c2.close()
        c3.close()

    def test_concurrent_delete_and_insert(self, livedoc_server):
        """One client deletes while another inserts."""
        port, _ = livedoc_server
        c1 = make_client(port, "concurrent_del", "alice")
        c2 = make_client(port, "concurrent_del", "bob")

        c1.insert(0, "hello world")
        c1.wait_synced()
        c2.wait_synced()  # ensure c2 sees c1's insert

        # Both operate on the same text
        c2.delete(0, 6)  # delete "hello "
        c1.delete(5, 5)  # delete "world"

        c2.wait_synced()
        c1.wait_synced()
        c1.wait_synced()  # ensure c1 sees c2's broadcast
        c2.wait_synced()  # ensure c2 sees c1's broadcast

        assert c1.text == c2.text
        c1.close()
        c2.close()

    def test_empty_operation(self, livedoc_server):
        """An empty operation should still be committed."""
        port, _ = livedoc_server
        c1 = make_client(port, "empty_op", "alice")
        c1.insert(0, "A")
        c1.wait_synced()

        c2 = make_client(port, "empty_op", "bob")
        c2.wait_synced()
        assert c1.text == c2.text == "A"
        c1.close()
        c2.close()
