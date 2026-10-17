"""Tests for live cursors and document history (snapshots)."""

import asyncio
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from livedoc_client import Client, move_cursor_through_op


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 5.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"Server did not start on port {port}")


@pytest.fixture(scope="module")
def server():
    """Start a Livedoc server subprocess. Yields (port, data_dir)."""
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="livedoc_test_")

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
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture()
def event_loop():
    """Create a fresh event loop for each test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _make_client(port, doc, client_name):
    """Connect to server and join a document. Returns (send, close, reader, writer)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def send(msg):
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        return json.loads(line.decode("utf-8"))

    await send({"type": "join", "doc": doc, "client": client_name})

    async def close():
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    return send, close, reader, writer


async def _set_cursor(reader, writer, doc, rev, pos):
    """Set cursor (no response on success)."""
    writer.write((json.dumps({
        "type": "cursor",
        "doc": doc,
        "rev": rev,
        "pos": pos,
    }) + "\n").encode("utf-8"))
    await writer.drain()


async def _drain(reader, timeout=0.3):
    """Drain all pending messages from reader, return list."""
    msgs = []
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                break
            msg = json.loads(line.decode("utf-8"))
            msgs.append(msg)
    except (asyncio.TimeoutError, TimeoutError):
        pass
    return msgs


# ===================================================================
# Cursor movement
# ===================================================================


class TestMoveCursorThroughOp:
    def test_ins_before_cursor(self):
        assert move_cursor_through_op(5, [{"ins": 0, "text": "X"}]) == 6

    def test_ins_after_cursor(self):
        assert move_cursor_through_op(5, [{"ins": 10, "text": "X"}]) == 5

    def test_ins_at_cursor_not_owner(self):
        assert move_cursor_through_op(5, [{"ins": 5, "text": "X"}], owner=None) == 5

    def test_ins_at_cursor_is_owner(self):
        assert move_cursor_through_op(5, [{"ins": 5, "text": "X"}], owner="alice") == 6

    def test_del_before_cursor(self):
        assert move_cursor_through_op(5, [{"del": 0, "len": 3}]) == 2

    def test_del_after_cursor(self):
        assert move_cursor_through_op(5, [{"del": 10, "len": 3}]) == 5

    def test_del_at_cursor(self):
        assert move_cursor_through_op(5, [{"del": 5, "len": 3}]) == 5

    def test_del_cursor_inside_range(self):
        assert move_cursor_through_op(6, [{"del": 3, "len": 5}]) == 3

    def test_del_cursor_at_start_of_range(self):
        assert move_cursor_through_op(3, [{"del": 3, "len": 5}]) == 3

    def test_del_cursor_at_end_of_range(self):
        assert move_cursor_through_op(8, [{"del": 3, "len": 5}]) == 3

    def test_multiple_edits(self):
        op = [{"ins": 0, "text": "X"}, {"del": 5, "len": 2}]
        assert move_cursor_through_op(0, op, owner="alice") == 1


# ===================================================================
# Cursor protocol tests
# ===================================================================


class TestCursorProtocol:
    def test_set_cursor(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "curs1", "alice")
            send2, close2, r2, w2 = await _make_client(port, "curs1", "bob")

            # Alice sets cursor at position 0
            await _set_cursor(r1, w1, "curs1", 0, 0)

            # Bob drains to get cursor broadcast
            bcasts = await _drain(r2)
            cursor_msgs = [m for m in bcasts if m.get("type") == "cursor"]
            assert len(cursor_msgs) >= 1
            assert cursor_msgs[0]["client"] == "alice"
            assert cursor_msgs[0]["pos"] == 0

            # Bob queries to verify
            cursors = await send2({"type": "cursors", "doc": "curs1"})
            assert cursors["type"] == "cursors"
            assert "alice" in cursors["cursors"]
            assert cursors["cursors"]["alice"] == 0

            await close1()
            await close2()

        event_loop.run_until_complete(run())

    def test_cursor_moves_through_op(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "curs2", "alice")
            send2, close2, r2, w2 = await _make_client(port, "curs2", "bob")

            # Alice inserts "hello"
            ack = await send1({"type": "op", "doc": "curs2", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello"}]})
            assert ack["type"] == "ack"
            assert ack["rev"] == 1
            await _drain(r2)  # Bob gets op broadcast

            # Alice sets cursor at position 2
            await _set_cursor(r1, w1, "curs2", 1, 2)

            # Bob drains to get cursor broadcast
            bcasts = await _drain(r2)
            cursor_msgs = [m for m in bcasts if m.get("type") == "cursor"]
            assert len(cursor_msgs) >= 1
            assert cursor_msgs[0]["pos"] == 2

            # Bob queries to verify
            cursors = await send2({"type": "cursors", "doc": "curs2"})
            assert cursors["cursors"]["alice"] == 2

            # Alice inserts "XX" at position 0
            ack1 = await send1({"type": "op", "doc": "curs2", "rev": 1, "id": "op2", "op": [{"ins": 0, "text": "XX"}]})
            assert ack1["type"] == "ack"
            assert ack1["rev"] == 2
            await _drain(r2)  # Bob gets op broadcast

            # Alice's cursor should move from 2 to 4 (owner typing pushes forward)
            cursors = await send2({"type": "cursors", "doc": "curs2"})
            assert cursors["cursors"]["alice"] == 4

            await close1()
            await close2()

        event_loop.run_until_complete(run())

    def test_cursor_not_moved_by_others_at_same_pos(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "curs3", "alice")
            send2, close2, r2, w2 = await _make_client(port, "curs3", "bob")

            # Alice inserts "hello"
            ack = await send1({"type": "op", "doc": "curs3", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello"}]})
            assert ack["type"] == "ack"
            await _drain(r2)

            # Alice sets cursor at position 0
            await _set_cursor(r1, w1, "curs3", 1, 0)
            await _drain(r2)  # Bob gets cursor broadcast

            # Bob inserts "X" at position 0
            ack1 = await send2({"type": "op", "doc": "curs3", "rev": 1, "id": "op2", "op": [{"ins": 0, "text": "X"}]})
            assert ack1["type"] == "ack"
            await _drain(r1)  # Alice gets op broadcast

            # Alice's cursor should NOT move (bob typed at her cursor, not owner)
            cursors = await send1({"type": "cursors", "doc": "curs3"})
            assert cursors["cursors"]["alice"] == 0

            await close1()
            await close2()

        event_loop.run_until_complete(run())

    def test_cursor_out_of_range(self, event_loop, server):
        port, _ = server

        async def run():
            send, close, r, w = await _make_client(port, "curs4", "alice")
            msg = await send({"type": "cursor", "doc": "curs4", "rev": 0, "pos": 100})
            assert msg["type"] == "error"
            assert msg["code"] == "out_of_range"
            await close()

        event_loop.run_until_complete(run())

    def test_cursor_bad_rev(self, event_loop, server):
        port, _ = server

        async def run():
            send, close, r, w = await _make_client(port, "curs5", "alice")
            msg = await send({"type": "cursor", "doc": "curs5", "rev": -1, "pos": 0})
            assert msg["type"] == "error"
            assert msg["code"] == "bad_rev"
            msg = await send({"type": "cursor", "doc": "curs5", "rev": 5, "pos": 0})
            assert msg["type"] == "error"
            assert msg["code"] == "bad_rev"
            await close()

        event_loop.run_until_complete(run())

    def test_cursor_not_joined(self, event_loop, server):
        port, _ = server

        async def run():
            send, close, r, w = await _make_client(port, "cursY", "alice")
            msg = await send({"type": "cursor", "doc": "cursX", "rev": 0, "pos": 0})
            assert msg["type"] == "error"
            assert msg["code"] == "not_joined"
            await close()

        event_loop.run_until_complete(run())

    def test_cursors_query(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "curs6", "alice")
            send2, close2, r2, w2 = await _make_client(port, "curs6", "bob")
            send3, close3, r3, w3 = await _make_client(port, "curs6", "charlie")

            # Insert text so cursors have valid positions
            ack = await send1({"type": "op", "doc": "curs6", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello world foo bar"}]})
            assert ack["type"] == "ack"
            await _drain(r2)
            await _drain(r3)

            # Alice sets cursor
            await _set_cursor(r1, w1, "curs6", 1, 5)
            await _drain(r2)  # bob gets broadcast
            await _drain(r3)  # charlie gets broadcast

            # Bob sets cursor
            await _set_cursor(r2, w2, "curs6", 1, 10)
            await _drain(r1)  # alice gets broadcast
            await _drain(r3)  # charlie gets broadcast

            cursors = await send3({"type": "cursors", "doc": "curs6"})
            assert cursors["type"] == "cursors"
            assert cursors["cursors"]["alice"] == 5
            assert cursors["cursors"]["bob"] == 10
            assert "charlie" not in cursors["cursors"]

            await close1()
            await close2()
            await close3()

        event_loop.run_until_complete(run())

    def test_cursor_replaced(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "curs7", "alice")
            send2, close2, r2, w2 = await _make_client(port, "curs7", "bob")

            # Insert text so cursors have valid positions
            ack = await send1({"type": "op", "doc": "curs7", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello world"}]})
            assert ack["type"] == "ack"
            await _drain(r2)

            # Alice sets cursor at 5
            await _set_cursor(r1, w1, "curs7", 1, 5)
            await _drain(r2)

            cursors = await send2({"type": "cursors", "doc": "curs7"})
            assert cursors["cursors"]["alice"] == 5

            # Alice moves cursor to 10
            await _set_cursor(r1, w1, "curs7", 1, 10)
            await _drain(r2)

            cursors = await send2({"type": "cursors", "doc": "curs7"})
            assert cursors["cursors"]["alice"] == 10

            await close1()
            await close2()

        event_loop.run_until_complete(run())

    def test_cursor_moves_through_delete(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "curs8", "alice")
            send2, close2, r2, w2 = await _make_client(port, "curs8", "bob")

            # Alice inserts "hello world" (11 chars)
            ack = await send1({"type": "op", "doc": "curs8", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello world"}]})
            assert ack["type"] == "ack"
            await _drain(r2)

            # Alice sets cursor at position 10
            await _set_cursor(r1, w1, "curs8", 1, 10)
            await _drain(r2)

            # Bob deletes 5 chars at position 0
            ack = await send2({"type": "op", "doc": "curs8", "rev": 1, "id": "op2", "op": [{"del": 0, "len": 5}]})
            assert ack["type"] == "ack"
            await _drain(r2)

            # Alice's cursor was at 10, after deleting 5 at 0: max(0, 10-5) = 5
            cursors = await send2({"type": "cursors", "doc": "curs8"})
            assert cursors["cursors"]["alice"] == 5

            await close1()
            await close2()

        event_loop.run_until_complete(run())

    def test_cursor_moves_through_stale_rev(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "curs9", "alice")
            send2, close2, r2, w2 = await _make_client(port, "curs9", "bob")

            # Alice inserts "XXX" at 0, rev goes to 1
            await send1({"type": "op", "doc": "curs9", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "XXX"}]})
            await _drain(r2)

            # Alice sets cursor at position 0 (rev 1)
            await _set_cursor(r1, w1, "curs9", 1, 0)
            await _drain(r2)

            # Bob queries: alice's cursor should be at 0
            cursors = await send2({"type": "cursors", "doc": "curs9"})
            assert cursors["cursors"]["alice"] == 0

            await close1()
            await close2()

        event_loop.run_until_complete(run())


# ===================================================================
# Leave messages
# ===================================================================


class TestLeaveMessages:
    def test_leave_on_disconnect(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "leave1", "alice")
            send2, close2, r2, w2 = await _make_client(port, "leave1", "bob")

            # Insert text so cursor has valid position
            ack = await send1({"type": "op", "doc": "leave1", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello world"}]})
            assert ack["type"] == "ack"
            await _drain(r2)

            # Alice sets cursor
            await _set_cursor(r1, w1, "leave1", 1, 3)
            await _drain(r2)  # Bob gets cursor broadcast

            # Bob queries to see alice's cursor
            cursors = await send2({"type": "cursors", "doc": "leave1"})
            assert "alice" in cursors["cursors"]

            # Disconnect alice abruptly
            w1.close()
            try:
                await w1.wait_closed()
            except Exception:
                pass

            # Bob should receive a leave message
            bcasts = await _drain(r2, timeout=1.0)
            leave_found = any(m.get("type") == "leave" and m.get("client") == "alice" for m in bcasts)
            assert leave_found, f"No leave message found. Got: {bcasts}"

            # Alice's cursor should be gone
            cursors = await send2({"type": "cursors", "doc": "leave1"})
            assert "alice" not in cursors["cursors"]

            await close2()

        event_loop.run_until_complete(run())

    def test_leave_only_last_connection(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1, r1, w1 = await _make_client(port, "leave2", "alice")
            send2, close2, r2, w2 = await _make_client(port, "leave2", "bob")
            send3, close3, r3, w3 = await _make_client(port, "leave2", "charlie")

            # Insert text so cursor has valid position
            ack = await send1({"type": "op", "doc": "leave2", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello world"}]})
            assert ack["type"] == "ack"
            await _drain(r2)
            await _drain(r3)

            # Alice sets cursor
            await _set_cursor(r1, w1, "leave2", 1, 5)
            await _drain(r2)
            await _drain(r3)

            # Disconnect alice
            w1.close()
            try:
                await w1.wait_closed()
            except Exception:
                pass

            # Wait for leave to propagate
            bcasts = await _drain(r2, timeout=1.0)
            leave_found = any(m.get("type") == "leave" and m.get("client") == "alice" for m in bcasts)
            assert leave_found, f"No leave message found. Got: {bcasts}"

            cursors = await send2({"type": "cursors", "doc": "leave2"})
            assert "alice" not in cursors["cursors"]

            await close2()
            await close3()

        event_loop.run_until_complete(run())


# ===================================================================
# Snapshot / document history tests
# ===================================================================


class TestSnapshot:
    def test_snapshot_fresh_doc(self, event_loop, server):
        port, _ = server

        async def run():
            send, close, r, w = await _make_client(port, "snap1", "alice")

            msg = await send({"type": "snapshot", "doc": "snap1", "rev": 0})
            assert msg["type"] == "error"
            assert msg["code"] == "not_found"

            await close()

        event_loop.run_until_complete(run())

    def test_snapshot_after_ops(self, event_loop, server):
        port, _ = server

        async def run():
            send, close, r, w = await _make_client(port, "snap2", "alice")

            # Insert some text
            ack = await send({"type": "op", "doc": "snap2", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello"}]})
            assert ack["type"] == "ack"
            assert ack["rev"] == 1

            # Insert more
            ack = await send({"type": "op", "doc": "snap2", "rev": 1, "id": "op2", "op": [{"ins": 5, "text": " world"}]})
            assert ack["type"] == "ack"
            assert ack["rev"] == 2

            # Snapshot at rev 0
            snap = await send({"type": "snapshot", "doc": "snap2", "rev": 0})
            assert snap["type"] == "snapshot"
            assert snap["rev"] == 0
            assert snap["text"] == ""

            # Snapshot at rev 1
            snap = await send({"type": "snapshot", "doc": "snap2", "rev": 1})
            assert snap["rev"] == 1
            assert snap["text"] == "hello"

            # Snapshot at rev 2
            snap = await send({"type": "snapshot", "doc": "snap2", "rev": 2})
            assert snap["rev"] == 2
            assert snap["text"] == "hello world"

            await close()

        event_loop.run_until_complete(run())

    def test_snapshot_not_joined(self, event_loop, server):
        """Snapshot does not require joining."""
        port, _ = server

        async def run():
            # First, create a document by joining
            send2, close2, r2, w2 = await _make_client(port, "snap3", "alice")
            await send2({"type": "op", "doc": "snap3", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "test"}]})
            await close2()

            # Now query snapshot without joining
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            async def send(msg):
                writer.write((json.dumps(msg) + "\n").encode("utf-8"))
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                return json.loads(line.decode("utf-8"))

            snap = await send({"type": "snapshot", "doc": "snap3", "rev": 1})
            assert snap["type"] == "snapshot"
            assert snap["text"] == "test"
            assert snap["rev"] == 1

            writer.close()

        event_loop.run_until_complete(run())

    def test_snapshot_bad_rev(self, event_loop, server):
        port, _ = server

        async def run():
            send, close, r, w = await _make_client(port, "snap4", "alice")
            await send({"type": "op", "doc": "snap4", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "x"}]})

            msg = await send({"type": "snapshot", "doc": "snap4", "rev": -1})
            assert msg["type"] == "error"
            assert msg["code"] == "bad_rev"

            msg = await send({"type": "snapshot", "doc": "snap4", "rev": 5})
            assert msg["type"] == "error"
            assert msg["code"] == "bad_rev"

            await close()

        event_loop.run_until_complete(run())

    def test_snapshot_not_found(self, event_loop, server):
        port, _ = server

        async def run():
            send, close, r, w = await _make_client(port, "snap5", "alice")

            msg = await send({"type": "snapshot", "doc": "nonexistent_doc_xyz", "rev": 0})
            assert msg["type"] == "error"
            assert msg["code"] == "not_found"

            await close()

        event_loop.run_until_complete(run())

    def test_snapshot_persistence(self, event_loop, server):
        """Snapshot should work after server restart."""
        port, data_dir = server

        async def run():
            send, close, r, w = await _make_client(port, "snap6", "alice")
            await send({"type": "op", "doc": "snap6", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "persistent"}]})
            await send({"type": "op", "doc": "snap6", "rev": 1, "id": "op2", "op": [{"ins": 10, "text": " data"}]})
            await close()

            # Verify the snapshot works
            send2, close2, r2, w2 = await _make_client(port, "snap6", "bob")
            snap = await send2({"type": "snapshot", "doc": "snap6", "rev": 0})
            assert snap["text"] == ""
            snap = await send2({"type": "snapshot", "doc": "snap6", "rev": 1})
            assert snap["text"] == "persistent"
            snap = await send2({"type": "snapshot", "doc": "snap6", "rev": 2})
            assert snap["text"] == "persistent data"

            await close2()

        event_loop.run_until_complete(run())


# ===================================================================
# Client library cursor tests
# ===================================================================


class TestClientCursors:
    def test_cursors_property_empty(self, event_loop, server):
        port, _ = server

        async def run():
            c = Client("127.0.0.1", port, "cltest1", "alice")
            assert c.cursors == {}
            c.close()

        event_loop.run_until_complete(run())

    def test_cursors_not_list_own_name(self, event_loop, server):
        port, _ = server

        async def run():
            c = Client("127.0.0.1", port, "cltest2", "alice")
            c.insert(0, "hello")
            c.wait_synced(timeout=5.0)

            # Bob joins and sets cursor
            c2 = Client("127.0.0.1", port, "cltest2", "bob")
            # Wait for Bob's cursor to be processed
            import time
            time.sleep(0.3)

            # Alice should not see bob's cursor until bob sets one
            assert c.cursors == {}

            c.close()
            c2.close()

        event_loop.run_until_complete(run())
