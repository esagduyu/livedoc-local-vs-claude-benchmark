import random
import socket
import threading
import time

import pytest

from conftest import Raw, connect, wait_until
from livedoc_client import Client


def server_state(port, doc):
    c = Raw(port)
    try:
        reply = c.join(doc, "observer")
        return reply["rev"], reply["text"]
    finally:
        c.close()


# -- against the real server -------------------------------------------------

def test_join_and_edit(server):
    with Client("127.0.0.1", server.port, "d", "alice") as a:
        assert (a.text, a.rev) == ("", 0)
        a.insert(0, "hello")
        a.insert(5, " wörld")
        a.delete(0, 1)
        assert a.text == "ello wörld"  # applied optimistically
        assert a.wait_synced()
        assert wait_until(lambda: server_state(server.port, "d") == (a.rev, a.text))
    with Client("127.0.0.1", server.port, "d", "bob") as b:
        assert b.text == "ello wörld"


def test_invalid_edits_raise(server):
    with Client("127.0.0.1", server.port, "d", "alice") as a:
        a.insert(0, "abc")
        for bad in [lambda: a.insert(4, "x"), lambda: a.insert(-1, "x"),
                    lambda: a.insert(0, ""), lambda: a.delete(2, 2),
                    lambda: a.delete(0, 0), lambda: a.delete(-1, 1)]:
            with pytest.raises(ValueError):
                bad()
        assert a.text == "abc"


def test_concurrent_edits_converge(server):
    a = Client("127.0.0.1", server.port, "d", "alice")
    b = Client("127.0.0.1", server.port, "d", "bob")
    try:
        a.insert(0, "AAAA")
        b.insert(0, "BBBB")
        a.delete(1, 2)
        b.insert(2, "xx")
        assert a.wait_synced() and b.wait_synced()
        rev, text = server_state(server.port, "d")
        assert wait_until(lambda: a.rev == b.rev == rev)
        assert a.text == b.text == text
        assert sorted(text) == sorted("AABBBBxx")
    finally:
        a.close()
        b.close()


def test_randomized_many_clients(server):
    clients = [Client("127.0.0.1", server.port, "fuzz", f"c{i}") for i in range(4)]
    errors = []

    def worker(i, c):
        rng = random.Random(i)
        try:
            for _ in range(150):
                text = c.text
                if text and rng.random() < 0.4:
                    p = rng.randrange(len(text))
                    n = rng.randint(1, min(3, len(text) - p))
                    try:
                        c.delete(p, n)
                    except ValueError:
                        pass  # a remote op shrank the text in between
                else:
                    try:
                        c.insert(rng.randint(0, len(text)), rng.choice(["a", "bc", "é", "😀"]))
                    except ValueError:
                        pass
                if rng.random() < 0.1:
                    time.sleep(0.001)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    # Read state from another thread while edits fly.
    while any(t.is_alive() for t in threads):
        for c in clients:
            assert isinstance(c.text, str) and isinstance(c.rev, int)
        time.sleep(0.005)
    for t in threads:
        t.join()
    assert not errors
    try:
        for c in clients:
            assert c.wait_synced(10)
        rev, text = server_state(server.port, "fuzz")
        assert wait_until(lambda: all(c.rev == rev for c in clients), 10)
        assert all(c.text == text for c in clients)
    finally:
        for c in clients:
            c.close()


def test_join_timeout():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        start = time.monotonic()
        with pytest.raises(Exception):
            Client("127.0.0.1", listener.getsockname()[1], "d", "a", timeout=0.5)
        assert time.monotonic() - start < 3
    finally:
        listener.close()


def test_join_error_raises(server):
    with pytest.raises(Exception):
        Client("127.0.0.1", server.port, "x" * 101, "a")


# -- against a scripted fake server ------------------------------------------

def test_one_operation_in_flight(fake):
    c = connect(fake, text="abc", rev=5)
    try:
        c.insert(3, "d")
        first = fake.recv()
        assert first["type"] == "op" and first["rev"] == 5 and first["op"] == [{"ins": 3, "text": "d"}]
        c.insert(4, "e")
        c.delete(0, 1)
        fake.assert_silent()                      # buffered while one is in flight
        assert c.text == "bcde" and not c.wait_synced(0.1)
        fake.send({"type": "ack", "doc": "d", "id": first["id"], "rev": 6})
        second = fake.recv()
        assert second["rev"] == 6 and second["id"] != first["id"]
        assert second["op"] == [{"ins": 4, "text": "e"}, {"del": 0, "len": 1}]
        fake.send({"type": "ack", "doc": "d", "id": second["id"], "rev": 7})
        assert c.wait_synced()
        assert (c.rev, c.text) == (7, "bcde")
    finally:
        c.close()


def test_remote_ops_transform_inflight_and_buffer(fake):
    c = connect(fake, text="0123456789", rev=1)
    try:
        c.delete(2, 3)                            # in flight: "0156789"
        inflight = fake.recv()
        c.insert(0, "M")                          # buffered: "M0156789"
        # Remote op committed before ours: insert inside our deleted range, and at 0.
        fake.send({"type": "op", "doc": "d", "rev": 2, "client": "x", "id": "r1",
                   "op": [{"ins": 3, "text": "R"}, {"ins": 0, "text": "S"}]})
        assert wait_until(lambda: c.rev == 2)
        # server text: "S012R3456789"; ours on top: delete 2,3,4 keeping R; M before S?
        # M's insert at 0 ties with S at 0; S was committed earlier so it stays left.
        assert c.text == "SM01R56789"
        fake.send({"type": "op", "doc": "other", "rev": 3, "client": "x", "id": "zz",
                   "op": [{"ins": 0, "text": "IGNORED"}]})
        fake.send({"type": "ack", "doc": "other", "id": inflight["id"], "rev": 3})
        fake.send({"type": "ack", "doc": "d", "id": inflight["id"], "rev": 3})
        second = fake.recv()
        assert second["rev"] == 3 and second["op"] == [{"ins": 1, "text": "M"}]
        fake.send({"type": "ack", "doc": "d", "id": second["id"], "rev": 4})
        assert c.wait_synced()
        assert (c.rev, c.text) == (4, "SM01R56789")
    finally:
        c.close()


def test_wait_synced_returns_false_when_disconnected(fake):
    c = connect(fake)
    try:
        c.insert(0, "x")
        fake.recv()
        fake.conn.close()
        fake.conn = None
        assert c.wait_synced(2) is False
    finally:
        c.close()
