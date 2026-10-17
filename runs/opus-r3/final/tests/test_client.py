import json
import random
import socket
import threading
import time

import pytest

from conftest import Conn
from livedoc_client import Client


def test_basic_editing(server):
    a = Client("127.0.0.1", server.port, "doc", "alice")
    b = Client("127.0.0.1", server.port, "doc", "bob")
    a.insert(0, "hello")
    assert a.text == "hello"  # optimistic
    assert a.wait_synced()
    b.insert(0, "x")
    assert b.wait_synced()
    deadline = time.time() + 5
    while (a.text, b.text) != ("xhello", "xhello") and time.time() < deadline:
        time.sleep(0.01)
    assert a.text == b.text == "xhello"
    assert a.rev == b.rev == 2
    a.delete(0, 2)
    assert a.text == "ello"
    with pytest.raises(ValueError):
        a.delete(3, 5)
    with pytest.raises(ValueError):
        a.insert(10, "z")
    with pytest.raises(ValueError):
        a.insert(0, "")
    assert a.wait_synced()
    c = Client("127.0.0.1", server.port, "doc", "carol")
    assert (c.text, c.rev) == ("ello", 3)
    for cl in (a, b, c):
        cl.close()


def test_join_timeout():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    try:
        with pytest.raises(Exception):
            Client("127.0.0.1", srv.getsockname()[1], "doc", "a", timeout=0.3)
    finally:
        srv.close()


def test_join_rejected(server):
    with pytest.raises(ConnectionError):
        Client("127.0.0.1", server.port, "x" * 101, "a")


def wait_converged(clients, server_port, doc, timeout=10):
    for c in clients:
        assert c.wait_synced(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        expected = Conn(server_port).join(doc, "observer")
        if all(c.rev == expected["rev"] for c in clients):
            break
        time.sleep(0.02)
    return expected


def test_many_clients_converge(server):
    clients = [Client("127.0.0.1", server.port, "shared", f"u{i}") for i in range(5)]

    def typist(i, c):
        rng = random.Random(i)
        for _ in range(150):
            n = len(c.text)
            try:
                if n and rng.random() < 0.35:
                    p = rng.randrange(n)
                    c.delete(p, rng.randint(1, min(4, n - p)))
                else:
                    c.insert(rng.randint(0, n), rng.choice(["a", "bc", "é", "😀", f"<{i}>"]))
            except ValueError:
                pass  # a remote op shrank the text between reading len and editing
            if rng.random() < 0.1:
                time.sleep(0.001)

    threads = [threading.Thread(target=typist, args=(i, c)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = wait_converged(clients, server.port, "shared")
    for c in clients:
        assert c.text == expected["text"]
        c.close()


class FakeServer:
    """Scripted server: lets a test see exactly what a Client sends."""

    def __init__(self, cursors=None):
        self.initial_cursors = cursors or {}
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.conn = None
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.conn, _ = self.sock.accept()
        self.file = self.conn.makefile("rb")
        join = self.recv()
        assert join["type"] == "join"
        self.send({"type": "joined", "doc": join["doc"], "rev": 7, "text": "abc"})
        assert self.recv() == {"type": "cursors", "doc": join["doc"]}
        self.send({"type": "cursors", "doc": join["doc"], "rev": 7, "cursors": self.initial_cursors})

    def send(self, msg):
        self.conn.sendall((json.dumps(msg) + "\n").encode())

    def recv(self, timeout=5):
        self.conn.settimeout(timeout)
        return json.loads(self.file.readline())

    def expect_nothing(self):
        self.conn.settimeout(0.2)
        with pytest.raises((socket.timeout, TimeoutError)):
            self.conn.recv(1, socket.MSG_PEEK)

    def close(self):
        if self.conn:
            self.conn.close()
        self.sock.close()


def test_one_op_in_flight_and_remote_transform():
    fake = FakeServer()
    c = Client("127.0.0.1", fake.port, "d", "me")
    assert (c.text, c.rev) == ("abc", 7)

    c.insert(3, "X")  # abcX
    first = fake.recv()
    assert first["rev"] == 7 and first["op"] == [{"ins": 3, "text": "X"}]
    c.insert(0, "Y")  # YabcX, buffered
    c.delete(1, 1)  # YbcX, buffered
    fake.expect_nothing()
    assert not c.wait_synced(0.1)

    # a remote op committed before ours: insert "R" at 1 in "abc"
    fake.send({"type": "op", "doc": "other", "rev": 1, "client": "z", "id": "q", "op": [{"del": 0, "len": 1}]})
    fake.send({"type": "op", "doc": "d", "rev": 8, "client": "z", "id": "r", "op": [{"ins": 1, "text": "R"}]})
    fake.send({"type": "ack", "doc": "d", "id": first["id"], "rev": 9})
    second = fake.recv()
    assert second["rev"] == 9
    # buffer was [ins 0 Y, del 1 1] on "abcX"; after R at 1 the deleted "a" moved
    assert second["op"] == [{"ins": 0, "text": "Y"}, {"del": 1, "len": 1}]
    assert c.text == "YRbcX"
    fake.expect_nothing()
    fake.send({"type": "ack", "doc": "d", "id": second["id"], "rev": 10})
    assert c.wait_synced(2)
    assert (c.text, c.rev) == ("YRbcX", 10)
    c.close()
    fake.close()


def test_remote_op_transformed_against_inflight():
    fake = FakeServer()
    c = Client("127.0.0.1", fake.port, "d", "me")
    c.delete(0, 3)  # in flight: delete "abc"
    first = fake.recv()
    # remote inserted inside the range we deleted; it must survive
    fake.send({"type": "op", "doc": "d", "rev": 8, "client": "z", "id": "r", "op": [{"ins": 1, "text": "Q"}]})
    fake.send({"type": "ack", "doc": "d", "id": first["id"], "rev": 9})
    assert c.wait_synced(2)
    assert (c.text, c.rev) == ("Q", 9)
    c.close()
    fake.close()
