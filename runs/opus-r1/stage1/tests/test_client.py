import json
import random
import socket
import threading
import time

import pytest

from conftest import Conn
from livedoc_client import Client


def ins(p, t):
    return {"ins": p, "text": t}


def dl(p, n):
    return {"del": p, "len": n}


def wait_for(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_basic_editing(server):
    with Client("127.0.0.1", server.port, "d", "alice") as a, Client("127.0.0.1", server.port, "d", "bob") as b:
        assert a.text == "" and a.rev == 0
        a.insert(0, "hello")
        assert a.text == "hello"            # optimistic
        assert a.wait_synced()
        assert wait_for(lambda: b.text == "hello")
        b.insert(5, " world")
        b.delete(0, 1)
        assert b.wait_synced()
        assert wait_for(lambda: a.text == "ello world")
        assert a.rev == b.rev


def test_join_existing_document(server):
    with Client("127.0.0.1", server.port, "d", "alice") as a:
        a.insert(0, "abc")
        assert a.wait_synced()
    with Client("127.0.0.1", server.port, "d", "bob") as b:
        assert (b.text, b.rev) == ("abc", 1)


def test_invalid_edits_raise_value_error(server):
    with Client("127.0.0.1", server.port, "d", "alice") as a:
        a.insert(0, "abc")
        for bad in (lambda: a.insert(4, "x"), lambda: a.insert(-1, "x"), lambda: a.insert(0, ""),
                    lambda: a.delete(2, 2), lambda: a.delete(0, 0), lambda: a.delete(-1, 1),
                    lambda: a.insert("0", "x")):
            with pytest.raises(ValueError):
                bad()
        assert a.text == "abc"
        assert a.wait_synced()


def test_join_timeout():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        start = time.time()
        with pytest.raises(Exception):
            Client("127.0.0.1", listener.getsockname()[1], "d", "a", timeout=0.5)
        assert time.time() - start < 3


class FakeServer:
    """A scripted server: the test decides what the client sees and when."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.conn = None
        self.buf = b""

    def accept(self):
        self.conn, _ = self.listener.accept()
        self.conn.settimeout(5)

    def recv(self, timeout=5):
        self.conn.settimeout(timeout)
        while b"\n" not in self.buf:
            self.buf += self.conn.recv(65536)
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def nothing(self, wait=0.2):
        try:
            msg = self.recv(wait)
        except socket.timeout:
            return True
        raise AssertionError("unexpected %r" % (msg,))

    def send(self, msg):
        self.conn.sendall((json.dumps(msg) + "\n").encode())

    def close(self):
        if self.conn:
            self.conn.close()
        self.listener.close()


@pytest.fixture
def fake():
    f = FakeServer()
    yield f
    f.close()


def start_client(fake, text="", rev=0):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("c", Client("127.0.0.1", fake.port, "d", "me")))
    t.start()
    fake.accept()
    assert fake.recv() == {"type": "join", "doc": "d", "client": "me"}
    fake.send({"type": "joined", "doc": "other", "rev": 99, "text": "wrong document"})
    fake.send({"type": "joined", "doc": "d", "rev": rev, "text": text})
    t.join()
    return result["c"]


def test_one_operation_in_flight_and_remote_transform(fake):
    c = start_client(fake, "hello", 3)
    try:
        c.insert(5, "!")
        m1 = fake.recv()
        assert m1["type"] == "op" and m1["rev"] == 3 and m1["op"] == [ins(5, "!")]
        c.insert(0, ">")
        c.delete(1, 1)                      # buffered: ">ello!"
        fake.nothing()
        assert not c.wait_synced(0.1)

        # A remote op committed before ours, and a message for another document.
        fake.send({"type": "op", "doc": "other", "rev": 1, "client": "x", "id": "z", "op": [ins(0, "zzz")]})
        fake.send({"type": "op", "doc": "d", "rev": 4, "client": "bob", "id": "b1", "op": [ins(5, " world"), dl(0, 2)]})
        assert wait_for(lambda: c.rev == 4)
        # Server text: "llo world"; with ours: in flight "!" after " world" (tie: bob's stays left),
        # then buffered ">" at the front and the delete of "e" (already deleted by bob) vanishes.
        assert c.text == ">llo world!"

        fake.send({"type": "ack", "doc": "d", "id": m1["id"], "rev": 5})
        m2 = fake.recv()
        assert m2["rev"] == 5 and m2["id"] != m1["id"]
        assert m2["op"] == [ins(0, ">")]
        assert not c.wait_synced(0.1)
        fake.send({"type": "ack", "doc": "d", "id": m2["id"], "rev": 6})
        assert c.wait_synced()
        assert (c.text, c.rev) == (">llo world!", 6)
        fake.nothing()
    finally:
        c.close()


def test_remote_op_splits_buffered_delete(fake):
    c = start_client(fake, "0123456789")
    try:
        c.insert(0, "A")
        m1 = fake.recv()
        c.delete(3, 5)                      # "A0123456789" -> "A01789", deletes "23456"
        fake.send({"type": "op", "doc": "d", "rev": 1, "client": "bob", "id": "b", "op": [ins(4, "XY")]})
        assert wait_for(lambda: c.rev == 1)
        assert c.text == "A01XY789"            # bob's insert inside the range survives
        fake.send({"type": "ack", "doc": "d", "id": m1["id"], "rev": 2})
        m2 = fake.recv()
        assert m2["rev"] == 2 and m2["op"] == [dl(3, 2), dl(5, 3)]
    finally:
        c.close()


def test_random_concurrent_clients_converge(server):
    n = 4
    clients = [Client("127.0.0.1", server.port, "fuzz", "c%d" % i) for i in range(n)]
    try:
        def work(c, seed):
            rng = random.Random(seed)
            for _ in range(150):
                length = len(c.text)
                try:
                    if length and rng.random() < 0.4:
                        p = rng.randrange(length)
                        c.delete(p, rng.randint(1, min(3, length - p)))
                    else:
                        c.insert(rng.randint(0, length), rng.choice(["a", "bc", "ü🙂", "xyz"]))
                except ValueError:
                    pass  # a remote op shrank the text between reading len() and editing
                if rng.random() < 0.2:
                    time.sleep(0.002)

        threads = [threading.Thread(target=work, args=(c, i)) for i, c in enumerate(clients)]
        for t in threads:
            t.start()
        reader_texts = []
        while any(t.is_alive() for t in threads):
            reader_texts.append(clients[0].text)   # reading from another thread is safe
        for t in threads:
            t.join()
        for c in clients:
            assert c.wait_synced(10)
        with Client("127.0.0.1", server.port, "fuzz", "observer") as obs:
            final_rev, final = obs.rev, obs.text
        assert wait_for(lambda: all(c.rev == final_rev for c in clients), 10)
        assert [c.text for c in clients] == [final] * n
    finally:
        for c in clients:
            c.close()


def test_client_matches_server_history(server):
    # Every op the client sent is based on its latest known revision and was acked in order.
    raw = Conn(server.port)
    raw.join("h", "raw")
    with Client("127.0.0.1", server.port, "h", "lib") as c:
        for i in range(20):
            c.insert(0, str(i % 10))
            raw.send({"type": "op", "doc": "h", "rev": 0, "id": "r%d" % i, "op": [ins(0, "-")]})
        assert c.wait_synced()
        while True:
            m = raw.recv()
            if m["type"] == "ack" and m["id"] == "r19":
                break
        assert wait_for(lambda: c.rev == Conn(server.port).join("h", "x")["rev"])
        assert c.text == Conn(server.port).join("h", "y")["text"]
        assert c.text.count("-") == 20
    raw.close()
