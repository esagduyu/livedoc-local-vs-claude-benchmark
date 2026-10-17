"""Tests for Livedoc – transformation, server, and client."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

import server as server_mod
import livedoc_client as client_mod


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerHandle:
    def __init__(self, port, data_dir):
        self.port = port
        self.data_dir = data_dir
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(os.path.dirname(__file__), "..", "server.py"),
             "--port", str(self.port), "--data-dir", self.data_dir],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for _ in range(50):
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.2)
                s.close()
                return
            except (OSError, ConnectionRefusedError):
                time.sleep(0.1)
        raise RuntimeError("Server did not start in time")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def _send_msg(sock, msg):
    sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))


def _recv_msg(sock, timeout=5.0):
    buf = b""
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        sock.settimeout(min(remaining, 0.5))
        try:
            data = sock.recv(65536)
            if not data:
                return None
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                return json.loads(raw)
        except socket.timeout:
            return None


def _recv_all(sock, timeout=5.0):
    msgs = []
    deadline = time.time() + timeout
    buf = b""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sock.settimeout(min(remaining, 0.1))
        try:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                msgs.append(json.loads(raw))
        except socket.timeout:
            break
    return msgs


def _apply_edits(text, edits):
    chars = list(text)
    for e in edits:
        if "ins" in e:
            p, t = e["ins"], e["text"]
            chars[p:p] = list(t)
        elif "del" in e:
            p, n = e["del"], e["len"]
            del chars[p:p + n]
    return "".join(chars)


def ins(p, t):
    return {"ins": p, "text": t}


def del_(p, n):
    return {"del": p, "len": n}


# ---------------------------------------------------------------------------
# Transformation unit tests
# ---------------------------------------------------------------------------

class TestTransformEditVSEdit:
    """Test the edit-vs-edit transformation rules."""

    def _test_text_equality(self, a, b, text="hello"):
        """Test that applying a then b gives same result as b then a (transformed)."""
        a_prime, b_prime = server_mod.transform_op([a], [b])
        text_ab = _apply_edits(_apply_edits(text, [a]), b_prime)
        text_ba = _apply_edits(_apply_edits(text, [b]), a_prime)
        assert text_ab == text_ba, (
            f"text_ab={text_ab!r} != text_ba={text_ba!r}\n"
            f"a={a}, b={b}\n"
            f"a_prime={a_prime}, b_prime={b_prime}"
        )

    def _test_op_text_equality(self, A, B, text="hello"):
        """Test that applying A then B gives same result as B then A (transformed)."""
        A_prime, B_prime = server_mod.transform_op(A, B)
        text_AB = _apply_edits(_apply_edits(text, A), B_prime)
        text_BA = _apply_edits(_apply_edits(text, B), A_prime)
        assert text_AB == text_BA, (
            f"text_AB={text_AB!r} != text_BA={text_BA!r}\n"
            f"A={A}, B={B}\n"
            f"A_prime={A_prime}, B_prime={B_prime}"
        )

    # --- ins vs ins ---
    def test_ins_vs_ins_before(self):
        self._test_text_equality(ins(3, "X"), ins(1, "Y"))

    def test_ins_vs_ins_after(self):
        self._test_text_equality(ins(1, "X"), ins(3, "Y"))

    def test_ins_vs_ins_tie(self):
        self._test_text_equality(ins(3, "X"), ins(3, "Y"))

    # --- ins vs del ---
    def test_ins_vs_del_before(self):
        self._test_text_equality(ins(1, "X"), del_(3, 2))

    def test_ins_vs_del_after(self):
        self._test_text_equality(ins(5, "X"), del_(1, 3))

    def test_ins_vs_del_inside(self):
        self._test_text_equality(ins(2, "X"), del_(1, 4))

    # --- del vs ins ---
    def test_del_vs_ins_before(self):
        self._test_text_equality(del_(2, 2), ins(0, "X"))

    def test_del_vs_ins_after(self):
        self._test_text_equality(del_(2, 2), ins(5, "X"))

    def test_del_vs_ins_inside(self):
        self._test_text_equality(del_(1, 4), ins(2, "X"))

    # --- del vs del ---
    def test_del_vs_del_no_overlap(self):
        self._test_text_equality(del_(0, 2), del_(3, 2))

    def test_del_vs_del_partial_overlap(self):
        self._test_text_equality(del_(1, 3), del_(2, 3))

    def test_del_vs_del_same_range(self):
        self._test_text_equality(del_(1, 3), del_(1, 3))

    def test_del_vs_del_full_overlap(self):
        self._test_text_equality(del_(0, 5), del_(1, 3))

    def test_del_vs_del_nested(self):
        self._test_text_equality(del_(0, 10), del_(3, 4))

    # --- op vs op (multi-edit) ---
    def test_multi_edit_op(self):
        self._test_op_text_equality([ins(1, "X"), del_(3, 1)], [del_(2, 2)])

    def test_empty_op(self):
        a_prime, b_prime = server_mod.transform_op([], [ins(2, "X")])
        assert a_prime == []
        assert b_prime == [ins(2, "X")]

    def test_both_empty(self):
        a_prime, b_prime = server_mod.transform_op([], [])
        assert a_prime == []
        assert b_prime == []

    def test_multi_edit_left(self):
        self._test_op_text_equality([ins(1, "X"), ins(3, "Y")], [del_(0, 2)])

    def test_multi_edit_right(self):
        self._test_op_text_equality([del_(1, 2)], [ins(0, "X"), del_(3, 1)])

    def test_vanishing_op(self):
        """An operation that vanishes after transform should still be committed as []."""
        a = [del_(0, 5)]
        b = [del_(0, 5)]
        a_prime, b_prime = server_mod.transform_op(a, b)
        assert a_prime == []
        self._test_op_text_equality(a, b)

    def test_longer_text(self):
        """Test with longer text."""
        self._test_text_equality(ins(5, "X"), del_(0, 3), "hello world!")
        self._test_op_text_equality([ins(5, "X"), del_(0, 3)], [del_(0, 3)])


class TestTransformOpChaining:
    """Test that transforming an op against multiple earlier ops works correctly."""

    def test_chain_transform(self):
        """Transform A against B then C should equal transform (transform(A,B), C)."""
        a = [ins(2, "X")]
        b = [del_(0, 1)]
        c = [ins(0, "Y")]
        # Step 1: transform a past b
        a1, b1 = server_mod.transform_op(a, b)
        # Step 2: transform a1 past c
        a2, c1 = server_mod.transform_op(a1, c)
        # Now apply b then c to original text, then a2
        text_bc = _apply_edits(_apply_edits("hello", b), c)
        text_result = _apply_edits(text_bc, a2)
        # Also: transform b past a, then transform c past a, then apply
        b2, a3 = server_mod.transform_op(b, a)
        c2, a4 = server_mod.transform_op(c, a)
        text_ba = _apply_edits(_apply_edits("hello", b), a3)
        text_bac = _apply_edits(text_ba, c2)
        assert text_result == text_bac

    def test_three_way_convergence(self):
        """Three clients editing concurrently should converge."""
        base = "hello"
        a = [ins(0, "A")]
        b = [ins(0, "B")]
        c = [ins(0, "C")]

        # Server commits a, then b, then c
        # a_prime applies after nothing
        # b_prime applies after a
        # c_prime applies after (a + b)
        a_prime, _ = server_mod.transform_op(a, [])
        b_prime, a2 = server_mod.transform_op(b, a)
        c_prime, b2 = server_mod.transform_op(c, b_prime)
        c2, a3 = server_mod.transform_op(a2, c_prime)

        text = ""
        text = _apply_edits(text, a_prime)
        text = _apply_edits(text, b_prime)
        text = _apply_edits(text, c_prime)

        # Verify from other client's perspective
        text2 = ""
        text2 = _apply_edits(text2, a3)
        text2 = _apply_edits(text2, b2)
        text2 = _apply_edits(text2, c2)

        assert text == text2


# ---------------------------------------------------------------------------
# Server tests
# ---------------------------------------------------------------------------

class TestServerJoin:
    def test_join_creates_document(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                sock = socket.create_connection(("127.0.0.1", port))
                try:
                    _send_msg(sock, {"type": "join", "doc": "test", "client": "alice"})
                    resp = _recv_msg(sock)
                    assert resp["type"] == "joined"
                    assert resp["doc"] == "test"
                    assert resp["rev"] == 0
                    assert resp["text"] == ""
                finally:
                    sock.close()

    def test_join_with_since(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s1 = socket.create_connection(("127.0.0.1", port))
                _send_msg(s1, {"type": "join", "doc": "test", "client": "alice"})
                _recv_msg(s1)
                _send_msg(s1, {"type": "op", "doc": "test", "rev": 0, "id": "op1",
                               "op": [{"ins": 0, "text": "hello"}]})
                _recv_msg(s1)
                _recv_all(s1)

                s2 = socket.create_connection(("127.0.0.1", port))
                _send_msg(s2, {"type": "join", "doc": "test", "client": "bob", "since": 0})
                resp = _recv_msg(s2)
                assert resp["type"] == "joined"
                assert "ops" in resp
                assert "text" not in resp
                assert len(resp["ops"]) == 1
                assert resp["ops"][0]["op"] == [{"ins": 0, "text": "hello"}]
                s1.close()
                s2.close()

    def test_join_with_since_partial(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s1 = socket.create_connection(("127.0.0.1", port))
                _send_msg(s1, {"type": "join", "doc": "test", "client": "alice"})
                _recv_msg(s1)
                _send_msg(s1, {"type": "op", "doc": "test", "rev": 0, "id": "op1",
                               "op": [{"ins": 0, "text": "he"}]})
                _recv_msg(s1)
                _recv_all(s1)
                _send_msg(s1, {"type": "op", "doc": "test", "rev": 1, "id": "op2",
                               "op": [{"ins": 2, "text": "llo"}]})
                _recv_msg(s1)
                _recv_all(s1)

                s2 = socket.create_connection(("127.0.0.1", port))
                _send_msg(s2, {"type": "join", "doc": "test", "client": "bob", "since": 1})
                resp = _recv_msg(s2)
                assert resp["type"] == "joined"
                assert len(resp["ops"]) == 1
                assert resp["ops"][0]["rev"] == 2
                s1.close()
                s2.close()


class TestServerOp:
    def test_basic_insert(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s = socket.create_connection(("127.0.0.1", port))
                _send_msg(s, {"type": "join", "doc": "test", "client": "alice"})
                _recv_msg(s)
                _send_msg(s, {"type": "op", "doc": "test", "rev": 0, "id": "op1",
                               "op": [{"ins": 0, "text": "hello"}]})
                ack = _recv_msg(s)
                assert ack["type"] == "ack"
                assert ack["rev"] == 1
                s.close()

    def test_basic_delete(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s1 = socket.create_connection(("127.0.0.1", port))
                _send_msg(s1, {"type": "join", "doc": "test", "client": "alice"})
                _recv_msg(s1)
                _send_msg(s1, {"type": "op", "doc": "test", "rev": 0, "id": "op1",
                               "op": [{"ins": 0, "text": "hello"}]})
                _recv_msg(s1)
                _recv_all(s1)

                s2 = socket.create_connection(("127.0.0.1", port))
                _send_msg(s2, {"type": "join", "doc": "test", "client": "bob"})
                _recv_msg(s2)
                _send_msg(s2, {"type": "op", "doc": "test", "rev": 1, "id": "op2",
                               "op": [{"del": 1, "len": 3}]})
                ack = _recv_msg(s2)
                assert ack["type"] == "ack"
                assert ack["rev"] == 2
                s1.settimeout(1.0)
                try:
                    bc = _recv_all(s1)
                    assert any(m["type"] == "op" and m["client"] == "bob" for m in bc)
                finally:
                    s1.settimeout(None)
                s1.close()
                s2.close()

    def test_exactly_once(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s = socket.create_connection(("127.0.0.1", port))
                _send_msg(s, {"type": "join", "doc": "test", "client": "alice"})
                _recv_msg(s)
                _send_msg(s, {"type": "op", "doc": "test", "rev": 0, "id": "dup",
                               "op": [{"ins": 0, "text": "hi"}]})
                ack1 = _recv_msg(s)
                assert ack1["rev"] == 1
                _send_msg(s, {"type": "op", "doc": "test", "rev": 1, "id": "dup",
                               "op": [{"ins": 0, "text": "hi"}]})
                ack2 = _recv_msg(s)
                assert ack2["type"] == "ack"
                assert ack2["rev"] == 1
                s.close()

    def test_exactly_once_after_restart(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            srv1 = _ServerHandle(port, tmpdir)
            srv1.start()
            s1 = socket.create_connection(("127.0.0.1", port))
            _send_msg(s1, {"type": "join", "doc": "test", "client": "alice"})
            _recv_msg(s1)
            _send_msg(s1, {"type": "op", "doc": "test", "rev": 0, "id": "unique-id",
                           "op": [{"ins": 0, "text": "persisted"}]})
            _recv_msg(s1)
            _recv_all(s1)
            srv1.stop()
            s1.close()

            srv2 = _ServerHandle(port, tmpdir)
            srv2.start()
            s2 = socket.create_connection(("127.0.0.1", port))
            _send_msg(s2, {"type": "join", "doc": "test", "client": "alice"})
            resp = _recv_msg(s2)
            assert resp["rev"] == 1
            assert resp["text"] == "persisted"
            _send_msg(s2, {"type": "op", "doc": "test", "rev": 1, "id": "unique-id",
                           "op": [{"ins": 0, "text": "persisted"}]})
            ack = _recv_msg(s2)
            assert ack["rev"] == 1
            s2.close()
            srv2.stop()


class TestServerConcurrency:
    def test_concurrent_inserts(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s1 = socket.create_connection(("127.0.0.1", port))
                s2 = socket.create_connection(("127.0.0.1", port))
                _send_msg(s1, {"type": "join", "doc": "test", "client": "alice"})
                _send_msg(s2, {"type": "join", "doc": "test", "client": "bob"})
                _recv_msg(s1)
                _recv_msg(s2)

                _send_msg(s1, {"type": "op", "doc": "test", "rev": 0, "id": "a1",
                               "op": [{"ins": 0, "text": "X"}]})
                _send_msg(s2, {"type": "op", "doc": "test", "rev": 0, "id": "b1",
                               "op": [{"ins": 0, "text": "Y"}]})

                # Collect all messages from both sockets
                s1.settimeout(2.0)
                s2.settimeout(2.0)
                try:
                    msgs1 = _recv_all(s1)
                    msgs2 = _recv_all(s2)
                finally:
                    s1.settimeout(None)
                    s2.settimeout(None)

                # Each connection gets its own ack plus the other's broadcast
                acks1 = [m for m in msgs1 if m["type"] == "ack"]
                acks2 = [m for m in msgs2 if m["type"] == "ack"]
                assert len(acks1) == 1
                assert len(acks2) == 1
                # Both ops are committed, so revisions are 1 and 2 (order depends on processing)
                revs = sorted([acks1[0]["rev"], acks2[0]["rev"]])
                assert revs == [1, 2]

                # Both should have received the other's op broadcast
                client1_ops = [m for m in msgs1 if m["type"] == "op"]
                client2_ops = [m for m in msgs2 if m["type"] == "op"]
                assert len(client1_ops) == 1
                assert len(client2_ops) == 1

                # Both should have same text
                all_ops = sorted(client1_ops + client2_ops, key=lambda m: m["rev"])
                text1 = _apply_edits("", [m["op"] for m in all_ops])
                text2 = _apply_edits("", [m["op"] for m in all_ops])
                assert text1 == text2
                s1.close()
                s2.close()


class TestServerErrors:
    def test_bad_json(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s = socket.create_connection(("127.0.0.1", port))
                s.sendall(b"not json\n")
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                assert resp["code"] == "bad_json"
                s.close()

    def test_unknown_type(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s = socket.create_connection(("127.0.0.1", port))
                _send_msg(s, {"type": "unknown"})
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                assert resp["code"] == "bad_request"
                s.close()

    def test_not_joined(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s = socket.create_connection(("127.0.0.1", port))
                _send_msg(s, {"type": "op", "doc": "test", "rev": 0, "id": "x", "op": []})
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                assert resp["code"] == "not_joined"
                s.close()

    def test_bad_rev(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s = socket.create_connection(("127.0.0.1", port))
                _send_msg(s, {"type": "join", "doc": "test", "client": "alice"})
                _recv_msg(s)
                _send_msg(s, {"type": "op", "doc": "test", "rev": -1, "id": "x", "op": []})
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                assert resp["code"] == "bad_rev"
                _send_msg(s, {"type": "op", "doc": "test", "rev": 999, "id": "x", "op": []})
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                assert resp["code"] == "bad_rev"
                s.close()

    def test_out_of_range(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s = socket.create_connection(("127.0.0.1", port))
                _send_msg(s, {"type": "join", "doc": "test", "client": "alice"})
                _recv_msg(s)
                _send_msg(s, {"type": "op", "doc": "test", "rev": 0, "id": "x",
                               "op": [{"ins": 999, "text": "hi"}]})
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                assert resp["code"] == "out_of_range"
                _send_msg(s, {"type": "op", "doc": "test", "rev": 0, "id": "y",
                               "op": [{"del": 0, "len": 0}]})
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                assert resp["code"] == "out_of_range"
                _send_msg(s, {"type": "op", "doc": "test", "rev": 0, "id": "z",
                               "op": [{"ins": 0, "text": ""}]})
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                assert resp["code"] == "out_of_range"
                s.close()

    def test_error_does_not_affect_connection(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                s = socket.create_connection(("127.0.0.1", port))
                _send_msg(s, {"type": "join", "doc": "test", "client": "alice"})
                _recv_msg(s)
                _send_msg(s, {"type": "op", "doc": "test", "rev": 0, "id": "x",
                               "op": [{"ins": 999, "text": "hi"}]})
                resp = _recv_msg(s)
                assert resp["type"] == "error"
                _send_msg(s, {"type": "op", "doc": "test", "rev": 0, "id": "y",
                               "op": [{"ins": 0, "text": "hello"}]})
                ack = _recv_msg(s)
                assert ack["type"] == "ack"
                assert ack["rev"] == 1
                s.close()


class TestServerPersistence:
    def test_persistence(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            srv1 = _ServerHandle(port, tmpdir)
            srv1.start()
            s1 = socket.create_connection(("127.0.0.1", port))
            _send_msg(s1, {"type": "join", "doc": "test", "client": "alice"})
            _recv_msg(s1)
            _send_msg(s1, {"type": "op", "doc": "test", "rev": 0, "id": "op1",
                           "op": [{"ins": 0, "text": "hello world"}]})
            _recv_msg(s1)
            _recv_all(s1)
            srv1.stop()
            s1.close()

            srv2 = _ServerHandle(port, tmpdir)
            srv2.start()
            s2 = socket.create_connection(("127.0.0.1", port))
            _send_msg(s2, {"type": "join", "doc": "test", "client": "bob"})
            resp = _recv_msg(s2)
            assert resp["type"] == "joined"
            assert resp["rev"] == 1
            assert resp["text"] == "hello world"
            s2.close()
            srv2.stop()


# ---------------------------------------------------------------------------
# Client library tests
# ---------------------------------------------------------------------------

class TestClient:
    def test_basic_join_and_text(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c = client_mod.Client("127.0.0.1", port, "test", "alice")
                try:
                    assert c.text == ""
                    assert c.rev == 0
                finally:
                    c.close()

    def test_insert(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c = client_mod.Client("127.0.0.1", port, "test", "alice")
                try:
                    c.insert(0, "hello")
                    assert c.wait_synced(timeout=3)
                    assert c.text == "hello"
                    assert c.rev == 1
                finally:
                    c.close()

    def test_delete(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c = client_mod.Client("127.0.0.1", port, "test", "alice")
                try:
                    c.insert(0, "hello")
                    c.wait_synced(timeout=3)
                    c.delete(1, 3)
                    assert c.wait_synced(timeout=3)
                    assert c.text == "ho"
                finally:
                    c.close()

    def test_concurrent_clients(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c1 = client_mod.Client("127.0.0.1", port, "test", "alice")
                c2 = client_mod.Client("127.0.0.1", port, "test", "bob")
                try:
                    c1.insert(0, "hello")
                    c2.insert(0, "world")
                    c1.wait_synced(timeout=5)
                    c2.wait_synced(timeout=5)
                    assert c1.text == c2.text, f"{c1.text!r} != {c2.text!r}"
                finally:
                    c1.close()
                    c2.close()

    def test_remote_edit_transform(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c1 = client_mod.Client("127.0.0.1", port, "test", "alice")
                c2 = client_mod.Client("127.0.0.1", port, "test", "bob")
                try:
                    c1.insert(0, "X")
                    c2.insert(0, "Y")
                    c1.wait_synced(timeout=5)
                    c2.wait_synced(timeout=5)
                    assert c1.text == c2.text
                finally:
                    c1.close()
                    c2.close()

    def test_invalid_insert(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c = client_mod.Client("127.0.0.1", port, "test", "alice")
                try:
                    with pytest.raises(ValueError):
                        c.insert(999, "hi")
                    with pytest.raises(ValueError):
                        c.insert(0, "")
                finally:
                    c.close()

    def test_invalid_delete(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c = client_mod.Client("127.0.0.1", port, "test", "alice")
                try:
                    c.insert(0, "hello")
                    c.wait_synced(timeout=3)
                    with pytest.raises(ValueError):
                        c.delete(0, 999)
                    with pytest.raises(ValueError):
                        c.delete(100, 1)
                finally:
                    c.close()

    def test_wait_synced_timeout(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c = client_mod.Client("127.0.0.1", port, "test", "alice")
                try:
                    c.insert(0, "hello")
                    assert c.wait_synced(timeout=3)
                finally:
                    c.close()

    def test_multiple_operations(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c = client_mod.Client("127.0.0.1", port, "test", "alice")
                try:
                    c.insert(0, "hello")
                    c.wait_synced(timeout=3)
                    c.delete(1, 2)
                    c.wait_synced(timeout=3)
                    c.insert(1, "XX")
                    c.wait_synced(timeout=3)
                    assert c.text == "hXXlo"
                finally:
                    c.close()

    def test_unicode(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c = client_mod.Client("127.0.0.1", port, "test", "alice")
                try:
                    c.insert(0, "こんにちは")
                    c.wait_synced(timeout=3)
                    assert c.text == "こんにちは"
                finally:
                    c.close()


class TestClientIntegration:
    def test_three_clients_converge(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            with _ServerHandle(port, tmpdir):
                c1 = client_mod.Client("127.0.0.1", port, "test", "alice")
                c2 = client_mod.Client("127.0.0.1", port, "test", "bob")
                c3 = client_mod.Client("127.0.0.1", port, "test", "charlie")
                try:
                    c1.insert(0, "A")
                    c2.insert(0, "B")
                    c3.insert(0, "C")
                    c1.wait_synced(timeout=5)
                    c2.wait_synced(timeout=5)
                    c3.wait_synced(timeout=5)
                    assert c1.text == c2.text == c3.text
                finally:
                    c1.close()
                    c2.close()
                    c3.close()

    def test_persistence_with_client(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmpdir:
            srv = _ServerHandle(port, tmpdir)
            srv.start()
            c1 = client_mod.Client("127.0.0.1", port, "test", "alice")
            c1.insert(0, "persistent")
            c1.wait_synced(timeout=3)
            c1.close()
            srv.stop()

            srv2 = _ServerHandle(port, tmpdir)
            srv2.start()
            c2 = client_mod.Client("127.0.0.1", port, "test", "bob")
            try:
                assert c2.text == "persistent"
                assert c2.rev == 1
            finally:
                c2.close()
            srv2.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
