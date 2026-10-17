"""
Server and client protocol tests for Livedoc.

Tests the server protocol: join/joined, ack, broadcast, error codes,
persistence, and client convergence.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from livedoc_client import Client, apply_op


# ---------------------------------------------------------------------------
# Helpers (also in conftest, duplicated for clarity in standalone runs)
# ---------------------------------------------------------------------------


def _send(sock: socket.socket, msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
    sock.sendall(data)


def _recv(sock: socket.socket, timeout: float = 5.0) -> dict:
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
# Protocol tests
# ---------------------------------------------------------------------------


class TestJoin:
    """Basic join/joined protocol."""

    def test_join_returns_text(self, livedoc_socket):
        _send(livedoc_socket, {"type": "join", "doc": "doc1", "client": "alice"})
        msg = _recv(livedoc_socket)
        assert msg["type"] == "joined"
        assert msg["doc"] == "doc1"
        assert msg["rev"] == 0
        assert msg["text"] == ""

    def test_join_with_nonempty_doc(self, livedoc_socket, livedoc_server):
        # Join, send an op, then join another client
        _send(livedoc_socket, {"type": "join", "doc": "doc2", "client": "alice"})
        _recv(livedoc_socket)
        # Send an edit
        _send(livedoc_socket, {
            "type": "op", "doc": "doc2", "rev": 0,
            "client": "alice",
            "id": "op-1", "op": [{"ins": 0, "text": "Hello"}],
        })
        ack = _recv(livedoc_socket)
        assert ack["type"] == "ack"
        assert ack["rev"] == 1

        # Join another client
        host, port, _ = livedoc_server
        bob_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bob_sock.settimeout(5.0)
        bob_sock.connect((host, port))
        _send(bob_sock, {"type": "join", "doc": "doc2", "client": "bob"})
        msg = _recv(bob_sock)
        assert msg["type"] == "joined"
        assert msg["rev"] == 1
        assert msg["text"] == "Hello"
        bob_sock.close()


class TestBroadcast:
    """Broadcast and ack behavior."""

    def test_broadcast_to_others(self, livedoc_socket, livedoc_server):
        # Client alice joins
        _send(livedoc_socket, {"type": "join", "doc": "doc3", "client": "alice"})
        _recv(livedoc_socket)

        # Create a second connection for bob
        host, port, _ = livedoc_server
        bob_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bob_sock.settimeout(5.0)
        bob_sock.connect((host, port))
        _send(bob_sock, {"type": "join", "doc": "doc3", "client": "bob"})
        _recv(bob_sock)

        # Alice sends an op
        _send(livedoc_socket, {
            "type": "op", "doc": "doc3", "client": "alice",
            "rev": 0,
            "id": "op-1", "op": [{"ins": 0, "text": "W"}],
        })
        # Alice gets ack
        ack = _recv(livedoc_socket)
        assert ack["type"] == "ack"
        assert ack["rev"] == 1
        # Alice does NOT receive her own broadcast
        # Bob receives the broadcast
        bob_msg = _recv(bob_sock, timeout=2.0)
        assert bob_msg["type"] == "op"
        assert bob_msg["op"] == [{"ins": 0, "text": "W"}]
        bob_sock.close()


class TestErrorCodes:
    """Error codes with id echoing."""

    def test_bad_request_missing_op(self, livedoc_socket):
        _send(livedoc_socket, {"type": "op", "doc": "doc4", "client": "alice",
                               "rev": 0, "id": "err-1"})
        msg = _recv(livedoc_socket)
        assert msg["type"] == "error"
        assert msg["code"] == "bad_request"
        assert msg["id"] == "err-1"

    def test_bad_rev(self, livedoc_socket):
        _send(livedoc_socket, {"type": "join", "doc": "doc5", "client": "alice"})
        _recv(livedoc_socket)
        _send(livedoc_socket, {
            "type": "op", "doc": "doc5", "client": "alice",
            "rev": 999,
            "id": "err-2", "op": [{"ins": 0, "text": "X"}],
        })
        msg = _recv(livedoc_socket)
        assert msg["type"] == "error"
        assert msg["code"] == "bad_rev"
        assert msg["id"] == "err-2"

    def test_not_joined(self, livedoc_socket):
        _send(livedoc_socket, {
            "type": "op", "doc": "doc6", "client": "alice",
            "rev": 0,
            "id": "err-3", "op": [{"ins": 0, "text": "X"}],
        })
        msg = _recv(livedoc_socket)
        assert msg["type"] == "error"
        assert msg["code"] == "not_joined"
        assert msg["id"] == "err-3"

    def test_unknown_type(self, livedoc_socket):
        _send(livedoc_socket, {"type": "unknown", "id": "err-4"})
        msg = _recv(livedoc_socket)
        assert msg["type"] == "error"
        assert msg["code"] == "bad_request"
        assert msg["id"] == "err-4"

    def test_connection_survives_errors(self, livedoc_socket):
        """After an error, the connection stays open."""
        _send(livedoc_socket, {"type": "join", "doc": "doc7", "client": "alice"})
        _recv(livedoc_socket)
        # Send bad op (missing op field)
        _send(livedoc_socket, {"type": "op", "doc": "doc7", "client": "alice",
                               "rev": 0, "id": "err-5"})
        msg = _recv(livedoc_socket)
        assert msg["type"] == "error"
        # Send valid op
        _send(livedoc_socket, {
            "type": "op", "doc": "doc7", "client": "alice",
            "rev": 0,
            "id": "ok-1", "op": [{"ins": 0, "text": "Y"}],
        })
        ack = _recv(livedoc_socket)
        assert ack["type"] == "ack"


class TestDeduplication:
    """Exactly-once: duplicate id acks original rev."""

    def test_duplicate_id(self, livedoc_socket):
        _send(livedoc_socket, {"type": "join", "doc": "doc8", "client": "alice"})
        _recv(livedoc_socket)
        # Send op with id "dup-1"
        _send(livedoc_socket, {
            "type": "op", "doc": "doc8", "client": "alice",
            "rev": 0,
            "id": "dup-1", "op": [{"ins": 0, "text": "Z"}],
        })
        ack1 = _recv(livedoc_socket)
        assert ack1["type"] == "ack"
        assert ack1["rev"] == 1
        # Send same op again with same id
        _send(livedoc_socket, {
            "type": "op", "doc": "doc8", "client": "alice",
            "rev": 0,
            "id": "dup-1", "op": [{"ins": 0, "text": "Z"}],
        })
        ack2 = _recv(livedoc_socket)
        assert ack2["type"] == "ack"
        assert ack2["rev"] == 1  # Same rev, not incremented


class TestPersistence:
    """SIGKILL, restart, text/rev/since history preserved."""

    def test_persistence_after_restart(self, livedoc_server):
        host, port, tmp_dir = livedoc_server
        # Client joins and sends an op
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((host, port))
        _send(sock, {"type": "join", "doc": "doc9", "client": "alice"})
        _recv(sock)
        _send(sock, {
            "type": "op", "doc": "doc9", "client": "alice",
            "rev": 0,
            "id": "persist-1", "op": [{"ins": 0, "text": "survives"}],
        })
        ack = _recv(sock)
        assert ack["type"] == "ack"
        assert ack["rev"] == 1
        sock.close()

        # Kill the server
        import signal
        # Find the server process
        for line in subprocess.check_output(
            ["pgrep", "-f", "server.py"], text=True
        ).splitlines():
            try:
                os.kill(int(line.strip()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(1)

        # Restart the server on the same port
        import subprocess as sp
        proc = sp.Popen(
            [
                sys.executable, "-B",
                str(Path(__file__).parent.parent / "server.py"),
                "--port", str(port),
                "--data-dir", tmp_dir,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            _wait_for_server(host, port)
        except RuntimeError:
            proc.kill()
            raise

        # New client joins and checks text
        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.settimeout(5.0)
        sock2.connect((host, port))
        _send(sock2, {"type": "join", "doc": "doc9", "client": "bob"})
        msg = _recv(sock2)
        assert msg["type"] == "joined"
        assert msg["text"] == "survives"
        assert msg["rev"] == 1
        sock2.close()
        proc.terminate()
        proc.wait(timeout=5)


def _wait_for_server(host, port, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.connect((host, port))
            return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    raise RuntimeError(f"Server did not start on {host}:{port}")


class TestClientLibrary:
    """Client-side tests: basic sync, convergence, buffering."""

    def test_basic_sync(self, livedoc_client):
        client = livedoc_client
        client.insert(0, "Hello")
        client.wait_synced(timeout=3.0)
        assert client.text == "Hello"

    def test_delete(self, livedoc_client):
        client = livedoc_client
        client.insert(0, "Hello")
        client.wait_synced(timeout=3.0)
        client.delete(0, 5)
        client.wait_synced(timeout=3.0)
        assert client.text == ""

    def test_value_error_on_invalid_edit(self, livedoc_client):
        client = livedoc_client
        with pytest.raises(ValueError):
            client.insert(100, "X")
        with pytest.raises(ValueError):
            client.delete(0, 5)
        with pytest.raises(ValueError):
            client.insert(0, "")

    def test_concurrent_clients_converge(self, livedoc_server):
        """3+ clients converge to the same text on the server."""
        host, port, _ = livedoc_server
        clients = []
        try:
            for i in range(3):
                c = Client(host, port, doc="converge", name=f"client-{i}")
                clients.append(c)

            # Each client inserts their name at position 0
            for i, c in enumerate(clients):
                c.insert(0, f"C{i}")

            # Wait for all clients to sync
            for c in clients:
                c.wait_synced(timeout=5.0)

            # Give time for broadcasts to propagate
            time.sleep(2.0)

            # All clients should see the same text
            texts = [c.text for c in clients]
            assert all(t == texts[0] for t in texts), f"Texts differ: {texts}"
        finally:
            for c in clients:
                c.close()

    def test_join_timeout(self, livedoc_server):
        """Join timeout raises TimeoutError."""
        host, port, _ = livedoc_server
        # Connect to a port where nothing is listening
        import socket as st
        with st.socket(st.AF_INET, st.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        try:
            with pytest.raises((TimeoutError, ConnectionError)):
                Client(host, free_port, doc="x", name="y", timeout=0.5)
        finally:
            pass

    def test_unicode_non_bmp(self, livedoc_client):
        """Unicode (non-BMP) positions work correctly."""
        client = livedoc_client
        client.insert(0, "🎉Hello")  # emoji is a single code point
        client.wait_synced(timeout=3.0)
        assert client.text.startswith("🎉")


class TestCursors:
    """Tests for live cursor (presence) feature."""

    def test_cursor_set_and_broadcast(self, livedoc_socket, livedoc_server):
        """Client sets cursor, server broadcasts to others."""
        host, port, _ = livedoc_server

        # Alice joins
        _send(livedoc_socket, {"type": "join", "doc": "doc-c1", "client": "alice"})
        _recv(livedoc_socket)

        # Bob joins
        bob_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bob_sock.settimeout(5.0)
        bob_sock.connect((host, port))
        _send(bob_sock, {"type": "join", "doc": "doc-c1", "client": "bob"})
        _recv(bob_sock)

        # Alice sets cursor at position 0
        _send(livedoc_socket, {
            "type": "cursor", "doc": "doc-c1", "client": "alice",
            "rev": 0, "pos": 0,
        })
        # Alice gets ack
        ack = _recv(livedoc_socket)
        assert ack["type"] == "ack"

        # Bob receives cursor update from Alice
        bob_msg = _recv(bob_sock)
        assert bob_msg["type"] == "cursor"
        assert bob_msg["client"] == "alice"
        assert bob_msg["pos"] == 0

        bob_sock.close()

    def test_cursor_moves_through_insert(self, livedoc_socket, livedoc_server):
        """Cursor position updates when insert happens at/after cursor."""
        host, port, _ = livedoc_server

        # Alice joins and inserts
        _send(livedoc_socket, {"type": "join", "doc": "doc-c2", "client": "alice"})
        _recv(livedoc_socket)
        _send(livedoc_socket, {
            "type": "op", "doc": "doc-c2", "client": "alice",
            "rev": 0, "id": "op-c2", "op": [{"ins": 0, "text": "XX"}],
        })
        _recv(livedoc_socket)  # ack

        # Bob joins and sets cursor at position 2
        bob_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bob_sock.settimeout(5.0)
        bob_sock.connect((host, port))
        _send(bob_sock, {"type": "join", "doc": "doc-c2", "client": "bob"})
        _recv(bob_sock)
        _send(bob_sock, {
            "type": "cursor", "doc": "doc-c2", "client": "bob",
            "rev": 1, "pos": 2,
        })
        _recv(bob_sock)  # ack

        # Alice inserts at position 0
        _send(livedoc_socket, {
            "type": "op", "doc": "doc-c2", "client": "alice",
            "rev": 1, "id": "op-c2b",
            "op": [{"ins": 0, "text": "YY"}],
        })
        _recv(livedoc_socket)  # ack
        _recv(livedoc_socket)  # broadcast

        # Bob's cursor should move from 2 to 4 (2 chars inserted before it)
        bob_msg = _recv(bob_sock)
        assert bob_msg["type"] == "cursor"
        assert bob_msg["client"] == "alice"
        assert bob_msg["pos"] == 4

        bob_sock.close()

    def test_cursor_query(self, livedoc_socket, livedoc_server):
        """Client queries all cursors for a document."""
        host, port, _ = livedoc_server

        # Alice joins and sets cursor
        _send(livedoc_socket, {"type": "join", "doc": "doc-c3", "client": "alice"})
        _recv(livedoc_socket)
        _send(livedoc_socket, {
            "type": "cursor", "doc": "doc-c3", "client": "alice",
            "rev": 0, "pos": 5,
        })
        _recv(livedoc_socket)

        # Bob joins
        bob_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bob_sock.settimeout(5.0)
        bob_sock.connect((host, port))
        _send(bob_sock, {"type": "join", "doc": "doc-c3", "client": "bob"})
        _recv(bob_sock)

        # Bob queries cursors
        _send(bob_sock, {"type": "cursors", "doc": "doc-c3", "client": "bob"})
        cursors_msg = _recv(bob_sock)
        assert cursors_msg["type"] == "cursors"
        assert "alice" in cursors_msg["cursors"]
        assert cursors_msg["cursors"]["alice"] == 5
        # Bob's own cursor should not be included
        assert "bob" not in cursors_msg["cursors"]

        bob_sock.close()

    def test_cursor_leave_on_disconnect(self, livedoc_socket, livedoc_server):
        """When a client disconnects, others receive a leave message."""
        host, port, _ = livedoc_server

        # Alice joins
        _send(livedoc_socket, {"type": "join", "doc": "doc-c4", "client": "alice"})
        _recv(livedoc_socket)

        # Bob joins
        bob_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bob_sock.settimeout(5.0)
        bob_sock.connect((host, port))
        _send(bob_sock, {"type": "join", "doc": "doc-c4", "client": "bob"})
        _recv(bob_sock)

        # Alice sets cursor
        _send(livedoc_socket, {
            "type": "cursor", "doc": "doc-c4", "client": "alice",
            "rev": 0, "pos": 3,
        })
        _recv(livedoc_socket)

        # Bob should receive cursor update
        bob_msg = _recv(bob_sock)
        assert bob_msg["type"] == "cursor"
        assert bob_msg["client"] == "alice"

        # Alice disconnects
        livedoc_socket.close()

        # Bob should receive leave message
        bob_msg = _recv(bob_sock)
        assert bob_msg["type"] == "leave"
        assert bob_msg["client"] == "alice"

        bob_sock.close()

    def test_cursor_out_of_range(self, livedoc_socket, livedoc_server):
        """Setting cursor at invalid position returns error."""
        _send(livedoc_socket, {"type": "join", "doc": "doc-c5", "client": "alice"})
        _recv(livedoc_socket)

        # Position 100 on empty document is out of range
        _send(livedoc_socket, {
            "type": "cursor", "doc": "doc-c5", "client": "alice",
            "rev": 0, "pos": 100,
        })
        err = _recv(livedoc_socket)
        assert err["type"] == "error"
        assert err["code"] == "out_of_range"


class TestSnapshot:
    """Tests for document history/snapshot feature."""

    def test_snapshot_at_revision(self, livedoc_socket, livedoc_server):
        """Client can fetch text at any revision via snapshot."""
        _send(livedoc_socket, {"type": "join", "doc": "doc-s1", "client": "alice"})
        _recv(livedoc_socket)

        # Insert "Hello"
        _send(livedoc_socket, {
            "type": "op", "doc": "doc-s1", "client": "alice",
            "rev": 0, "id": "s-op1",
            "op": [{"ins": 0, "text": "Hello"}],
        })
        _recv(livedoc_socket)

        # Insert " World" at position 5
        _send(livedoc_socket, {
            "type": "op", "doc": "doc-s1", "client": "alice",
            "rev": 1, "id": "s-op2",
            "op": [{"ins": 5, "text": " World"}],
        })
        _recv(livedoc_socket)

        # Query snapshot at rev 1 (should be "Hello")
        _send(livedoc_socket, {"type": "snapshot", "doc": "doc-s1", "rev": 1})
        snap1 = _recv(livedoc_socket)
        assert snap1["type"] == "snapshot"
        assert snap1["text"] == "Hello"
        assert snap1["rev"] == 1

        # Query snapshot at rev 2 (should be "Hello World")
        _send(livedoc_socket, {"type": "snapshot", "doc": "doc-s1", "rev": 2})
        snap2 = _recv(livedoc_socket)
        assert snap2["type"] == "snapshot"
        assert snap2["text"] == "Hello World"
        assert snap2["rev"] == 2

        # Query snapshot at rev 0 (should be empty)
        _send(livedoc_socket, {"type": "snapshot", "doc": "doc-s1", "rev": 0})
        snap0 = _recv(livedoc_socket)
        assert snap0["type"] == "snapshot"
        assert snap0["text"] == ""
        assert snap0["rev"] == 0

    def test_snapshot_not_found(self, livedoc_socket):
        """Snapshot for non-existent document returns not_found."""
        _send(livedoc_socket, {"type": "snapshot", "doc": "no-such-doc", "rev": 0})
        err = _recv(livedoc_socket)
        assert err["type"] == "error"
        assert err["code"] == "not_found"

    def test_snapshot_bad_rev(self, livedoc_socket, livedoc_server):
        """Snapshot with invalid revision returns bad_rev."""
        _send(livedoc_socket, {"type": "join", "doc": "doc-s2", "client": "alice"})
        _recv(livedoc_socket)

        _send(livedoc_socket, {"type": "snapshot", "doc": "doc-s2", "rev": 999})
        err = _recv(livedoc_socket)
        assert err["type"] == "error"
        assert err["code"] == "bad_rev"

    def test_snapshot_without_join(self, livedoc_socket, livedoc_server):
        """Snapshot can be fetched without joining the document."""
        # No join needed
        _send(livedoc_socket, {"type": "join", "doc": "doc-s3", "client": "alice"})
        _recv(livedoc_socket)
        _send(livedoc_socket, {
            "type": "op", "doc": "doc-s3", "client": "alice",
            "rev": 0, "id": "s3-op1",
            "op": [{"ins": 0, "text": "Snapshot"}],
        })
        _recv(livedoc_socket)

        # New connection queries snapshot without joining
        host, port, _ = livedoc_server
        new_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        new_sock.settimeout(5.0)
        new_sock.connect((host, port))
        _send(new_sock, {"type": "snapshot", "doc": "doc-s3", "rev": 1})
        snap = _recv(new_sock)
        assert snap["type"] == "snapshot"
        assert snap["text"] == "Snapshot"
        new_sock.close()


class TestClientCursors:
    """Tests for client-side cursor management."""

    def test_client_cursors_property(self, livedoc_server):
        """Client cursors dict tracks other clients' positions."""
        host, port, _ = livedoc_server
        alice = Client(host, port, doc="curs-doc", name="alice")
        bob = Client(host, port, doc="curs-doc", name="bob")

        # Alice inserts some text
        alice.insert(0, "Hello")
        alice.wait_synced(timeout=3.0)

        # Bob sets a cursor
        # We simulate by sending a cursor message directly
        import json
        bob._send({"type": "cursor", "doc": "curs-doc", "client": "bob",
                   "rev": bob._rev, "pos": 3})
        time.sleep(0.5)

        # Alice should see Bob's cursor
        time.sleep(0.5)
        cursors = alice.cursors
        assert "bob" in cursors
        assert cursors["bob"] == 3

        alice.close()
        bob.close()

    def test_client_moves_remote_cursors(self, livedoc_server):
        """Client moves remote cursors through its own edits."""
        host, port, _ = livedoc_server
        alice = Client(host, port, doc="curs-doc2", name="alice")
        bob = Client(host, port, doc="curs-doc2", name="bob")

        # Both insert text
        alice.insert(0, "Hello")
        alice.wait_synced(timeout=3.0)
        bob.insert(0, "World")
        bob.wait_synced(timeout=3.0)

        # Bob sets cursor at position 5 (end of "Hello")
        import json
        bob._send({"type": "cursor", "doc": "curs-doc2", "client": "bob",
                   "rev": bob._rev, "pos": 5})
        time.sleep(0.5)

        # Alice inserts at position 0
        alice.insert(0, "XX")
        alice.wait_synced(timeout=3.0)

        # Alice's cursor for Bob should have moved from 5 to 7
        time.sleep(0.5)
        cursors = alice.cursors
        assert cursors.get("bob", 0) == 7  # 5 + 2

        alice.close()
        bob.close()

    def test_client_handles_leave(self, livedoc_server):
        """Client removes cursor when other client leaves."""
        host, port, _ = livedoc_server
        alice = Client(host, port, doc="curs-doc3", name="alice")
        bob = Client(host, port, doc="curs-doc3", name="bob")

        # Bob sets cursor
        import json
        bob._send({"type": "cursor", "doc": "curs-doc3", "client": "bob",
                   "rev": bob._rev, "pos": 2})
        time.sleep(0.5)

        # Alice sees Bob's cursor
        time.sleep(0.5)
        assert "bob" in alice.cursors

        # Bob disconnects
        bob.close()
        time.sleep(1.0)

        # Alice should no longer have Bob's cursor
        assert "bob" not in alice.cursors

        alice.close()
