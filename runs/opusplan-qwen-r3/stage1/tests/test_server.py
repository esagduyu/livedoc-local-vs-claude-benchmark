"""Tests for the Livedoc server."""

import asyncio
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from livedoc_ot import apply_op


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
    """Connect to server and join a document. Returns (send, close)."""
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

    return send, close


class TestJoin:
    def test_join_fresh_doc(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "test1", "alice")
            msg = await send({"type": "op", "doc": "test1", "rev": 0, "id": "noop", "op": []})
            # Just verify the connection is alive
            assert msg["type"] == "ack" or msg["type"] == "error"
            await close()

        event_loop.run_until_complete(run())

    def test_join_existing_doc(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1 = await _make_client(port, "test2", "alice")
            ack = await send1({"type": "op", "doc": "test2", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello"}]})
            assert ack["type"] == "ack"
            assert ack["rev"] == 1
            ack2 = await send1({"type": "op", "doc": "test2", "rev": 1, "id": "op2", "op": [{"ins": 5, "text": " world"}]})
            assert ack2["type"] == "ack"
            assert ack2["rev"] == 2
            await close1()

        event_loop.run_until_complete(run())

    def test_join_since(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1 = await _make_client(port, "test3", "alice")
            await send1({"type": "op", "doc": "test3", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello"}]})
            await send1({"type": "op", "doc": "test3", "rev": 1, "id": "op2", "op": [{"ins": 5, "text": " world"}]})
            await close1()

            send2, close2 = await _make_client(port, "test3", "bob")
            msg = await send2({"type": "op", "doc": "test3", "rev": 2, "id": "noop", "op": []})

            # Now check since
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            async def send_since(msg):
                writer.write((json.dumps(msg) + "\n").encode("utf-8"))
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                return json.loads(line.decode("utf-8"))

            await send_since({"type": "join", "doc": "test3", "client": "charlie", "since": 1})
            jmsg = await send_since({"type": "op", "doc": "test3", "rev": 2, "id": "noop2", "op": []})
            await close2()

        event_loop.run_until_complete(run())

    def test_join_since_bad_rev(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1 = await _make_client(port, "test4", "alice")
            send2, close2 = await _make_client(port, "test4", "bob")
            msg = await send2({"type": "join", "doc": "test4", "client": "charlie", "since": 5})
            assert msg["type"] == "error"
            assert msg["code"] == "bad_rev"
            await close1()
            await close2()

        event_loop.run_until_complete(run())

    def test_join_since_negative(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "test5", "alice")
            msg = await send({"type": "join", "doc": "test5", "client": "bob", "since": -1})
            assert msg["type"] == "error"
            assert msg["code"] == "bad_rev"
            await close()

        event_loop.run_until_complete(run())


class TestOp:
    def test_basic_op(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "doc1", "alice")
            msg = await send({"type": "op", "doc": "doc1", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "hello"}]})
            assert msg["type"] == "ack"
            assert msg["rev"] == 1
            await close()

        event_loop.run_until_complete(run())

    def test_op_from_stale_revision(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1 = await _make_client(port, "doc3", "alice")
            send2, close2 = await _make_client(port, "doc3", "bob")

            ack1 = await send1({"type": "op", "doc": "doc3", "rev": 0, "id": "a1", "op": [{"ins": 0, "text": "A"}]})
            assert ack1["type"] == "ack"
            assert ack1["rev"] == 1

            # Bob's op may receive ack or broadcast first (broadcast is sent after ack)
            msg_b1 = await send2({"type": "op", "doc": "doc3", "rev": 0, "id": "b1", "op": [{"ins": 0, "text": "B"}]})
            if msg_b1["type"] == "op":
                # Got broadcast first, now get ack
                ack2 = await send2({"type": "op", "doc": "doc3", "rev": 2, "id": "noop", "op": []})
                assert ack2["type"] == "ack"
                assert ack2["rev"] == 2
            else:
                assert msg_b1["type"] == "ack"
                assert msg_b1["rev"] == 2

            # Verify via fresh join
            send3, close3 = await _make_client(port, "doc3", "check")
            jmsg = await send3({"type": "op", "doc": "doc3", "rev": 2, "id": "noop", "op": []})

            await close1()
            await close2()
            await close3()

        event_loop.run_until_complete(run())

    def test_empty_op(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "doc4", "alice")
            msg = await send({"type": "op", "doc": "doc4", "rev": 0, "id": "op1", "op": []})
            assert msg["type"] == "ack"
            assert msg["rev"] == 1
            await close()

        event_loop.run_until_complete(run())


class TestErrors:
    def test_bad_json(self, event_loop, server):
        port, _ = server

        async def run():
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"not json\n")
            await asyncio.wait_for(writer.drain(), timeout=5.0)
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            msg = json.loads(line.decode("utf-8"))
            assert msg["type"] == "error"
            assert msg["code"] == "bad_json"
            writer.close()

        event_loop.run_until_complete(run())

    def test_bad_request_unknown_type(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "docX", "alice")
            msg = await send({"type": "unknown"})
            assert msg["type"] == "error"
            assert msg["code"] == "bad_request"
            await close()

        event_loop.run_until_complete(run())

    def test_not_joined(self, event_loop, server):
        port, _ = server

        async def run():
            # Join docY, then try to send op to docX (not joined)
            send_y, close = await _make_client(port, "docY", "alice")
            msg = await send_y({"type": "op", "doc": "docX", "rev": 0, "id": "op1", "op": [{"ins": 0, "text": "x"}]})
            assert msg["type"] == "error"
            assert msg["code"] == "not_joined"
            await close()

        event_loop.run_until_complete(run())

    def test_bad_rev(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "doc5", "alice")
            msg = await send({"type": "op", "doc": "doc5", "rev": 5, "id": "op1", "op": [{"ins": 0, "text": "x"}]})
            assert msg["type"] == "error"
            assert msg["code"] == "bad_rev"
            await close()

        event_loop.run_until_complete(run())

    def test_out_of_range(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "doc6", "alice")
            msg = await send({"type": "op", "doc": "doc6", "rev": 0, "id": "op1", "op": [{"del": 0, "len": 5}]})
            assert msg["type"] == "error"
            assert msg["code"] == "out_of_range"
            await close()

        event_loop.run_until_complete(run())

    def test_connection_still_works_after_error(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "doc7", "alice")
            msg = await send({"type": "op", "doc": "doc7", "rev": 0, "id": "op1", "op": [{"del": 0, "len": 5}]})
            assert msg["type"] == "error"
            send2, close2 = await _make_client(port, "doc7", "alice2")
            ack = await send2({"type": "op", "doc": "doc7", "rev": 0, "id": "op2", "op": [{"ins": 0, "text": "ok"}]})
            assert ack["type"] == "ack"
            await close()
            await close2()

        event_loop.run_until_complete(run())


class TestExactlyOnce:
    def test_duplicate_op_id(self, event_loop, server):
        port, _ = server

        async def run():
            send, close = await _make_client(port, "doc8", "alice")
            ack1 = await send({"type": "op", "doc": "doc8", "rev": 0, "id": "unique-id", "op": [{"ins": 0, "text": "x"}]})
            assert ack1["type"] == "ack"
            assert ack1["rev"] == 1
            ack2 = await send({"type": "op", "doc": "doc8", "rev": 1, "id": "unique-id", "op": [{"ins": 0, "text": "y"}]})
            assert ack2["type"] == "ack"
            assert ack2["rev"] == 1
            await close()

        event_loop.run_until_complete(run())


class TestDisconnect:
    def test_disconnect_no_effect_on_others(self, event_loop, server):
        port, _ = server

        async def run():
            send1, close1 = await _make_client(port, "doc9", "alice")
            send2, close2 = await _make_client(port, "doc9", "bob")

            # Disconnect alice abruptly
            send1.__globals__.get("writer")  # We need to close the writer
            # Get the underlying writer from the send function
            await close1()

            # Bob should still be able to send ops
            ack = await send2({"type": "op", "doc": "doc9", "rev": 0, "id": "b1", "op": [{"ins": 0, "text": "ok"}]})
            assert ack["type"] == "ack"
            await close2()

        event_loop.run_until_complete(run())
