import json
import random
import socket
import threading
import time
import uuid

import pytest

from helpers import Wire
from livedoc_client import Client, ProtocolError


def ins(p, t):
    return {"ins": p, "text": t}


def dele(p, n):
    return {"del": p, "len": n}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def server_text(port, doc):
    """Ask the real server for the committed state of a document."""
    w = Wire(port)
    try:
        reply = w.join(doc, f"probe-{uuid.uuid4()}")
        return reply["text"], reply["rev"]
    finally:
        w.close()


@pytest.fixture
def make_client(running_server):
    clients = []

    def make(doc, name, timeout=5.0):
        c = Client("127.0.0.1", running_server["port"], doc, name, timeout=timeout)
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.close()


class FakeServer:
    """A scripted server on a real socket, for deterministic interleavings."""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.conn = None
        self.errors = []

    def accept_and_join(self, text="", rev=0):
        self.conn, _ = self.listener.accept()
        self.rfile = self.conn.makefile("rb")
        join = json.loads(self.rfile.readline())
        assert join["type"] == "join"
        self.send({"type": "joined", "doc": join["doc"], "rev": rev, "text": text})

    def send(self, message):
        self.conn.sendall(json.dumps(message).encode() + b"\n")

    def recv(self):
        return json.loads(self.rfile.readline())

    def close(self):
        for s in (self.conn, self.listener):
            if s is not None:
                s.close()


@pytest.fixture
def fake_server():
    fs = FakeServer()
    yield fs
    fs.close()


def connect_fake(fake, text="", rev=0):
    """Join a client to the fake server. The fake accepts on a thread, because
    Client() blocks until the join reply arrives."""
    accepting = threading.Thread(target=fake.accept_and_join, args=(text, rev))
    accepting.start()
    client = Client("127.0.0.1", fake.port, "d", "alice")
    accepting.join()
    return client


def test_join_timeout_raises(fake_server):
    thread = threading.Thread(target=lambda: fake_server.listener.accept(), daemon=True)
    thread.start()
    with pytest.raises((TimeoutError, OSError)):
        Client("127.0.0.1", fake_server.port, "d", "alice", timeout=0.3)


def test_join_refused_raises_protocol_error(running_server):
    with pytest.raises(ProtocolError):
        Client("127.0.0.1", running_server["port"], "", "alice")


def test_local_edits_apply_optimistically_and_validate(make_client, running_server):
    c = make_client("local", "alice")
    c.insert(0, "abc")
    assert c.text == "abc"
    c.delete(1, 1)
    assert c.text == "ac"
    for bad in [lambda: c.insert(5, "x"), lambda: c.insert(0, ""),
                lambda: c.delete(0, 0), lambda: c.delete(1, 5), lambda: c.delete(-1, 1)]:
        with pytest.raises(ValueError):
            bad()
    assert c.text == "ac"
    assert c.wait_synced()
    assert server_text(running_server["port"], "local") == ("ac", 2)


def test_rev_tracks_acknowledged_and_remote_revisions(make_client, running_server):
    a = make_client("rev", "alice")
    b = make_client("rev", "bob")
    assert a.rev == 0
    a.insert(0, "x")
    assert a.wait_synced()
    assert a.rev == 1
    assert wait_for(lambda: b.rev == 1 and b.text == "x")


def test_remote_op_while_in_flight_and_buffered(fake_server):
    client = connect_fake(fake_server)

    client.insert(0, "x")          # sent: in flight
    client.insert(1, "z")          # buffered
    assert client.text == "xz"
    op1 = fake_server.recv()
    assert op1["op"] == [ins(0, "x")] and op1["rev"] == 0

    # bob's op arrives while alice's is unacknowledged
    fake_server.send({"type": "op", "doc": "d", "rev": 1, "client": "bob",
                      "id": "b1", "op": [ins(0, "y")]})
    assert wait_for(lambda: client.text == "yxz")
    assert client.rev == 1
    fake_server.send({"type": "ack", "doc": "d", "id": op1["id"], "rev": 2})

    op2 = fake_server.recv()
    assert op2["rev"] == 2 and op2["op"] == [ins(2, "z")]
    fake_server.send({"type": "ack", "doc": "d", "id": op2["id"], "rev": 3})
    assert client.wait_synced(2)
    assert client.text == "yxz" and client.rev == 3
    client.close()


def test_remote_delete_overlapping_local_edit(fake_server):
    client = connect_fake(fake_server, "abcdef")
    client.delete(1, 3)            # deletes "bcd", in flight
    assert client.text == "aef"
    op1 = fake_server.recv()
    fake_server.send({"type": "op", "doc": "d", "rev": 1, "client": "bob",
                      "id": "b", "op": [dele(2, 3)]})  # bob deletes "cde"
    assert wait_for(lambda: client.text == "af")
    fake_server.send({"type": "ack", "doc": "d", "id": op1["id"], "rev": 2})
    assert client.wait_synced(2)
    assert client.text == "af" and client.rev == 2
    client.close()


def test_wait_synced_false_when_server_never_acks(fake_server):
    client = connect_fake(fake_server)
    client.insert(0, "x")
    fake_server.recv()
    assert client.wait_synced(0.2) is False
    client.close()


def test_two_clients_converge_on_real_server(make_client, running_server):
    a = make_client("pair", "alice")
    b = make_client("pair", "bob")
    a.insert(0, "hello")
    b.insert(0, "world")
    a.insert(5, "!")
    assert a.wait_synced() and b.wait_synced()
    assert wait_for(lambda: a.text == b.text)
    assert server_text(running_server["port"], "pair") == (a.text, 3)


def test_concurrent_editors_converge(make_client, running_server):
    doc = "fuzz"
    editors = [make_client(doc, f"user{i}") for i in range(3)]
    errors = []

    def edit(client, seed):
        rng = random.Random(seed)
        try:
            for _ in range(60):
                time.sleep(rng.random() * 0.002)
                text = client.text
                try:
                    if text and rng.random() < 0.35:
                        pos = rng.randrange(len(text))
                        client.delete(pos, rng.randint(1, min(3, len(text) - pos)))
                    else:
                        word = "".join(rng.choice("abcXYZ") for _ in range(rng.randint(1, 3)))
                        client.insert(rng.randint(0, len(text)), word)
                except ValueError:
                    pass  # a remote edit changed the text since we looked: skip it
        except Exception as exc:  # surface failures in the main thread
            errors.append(exc)

    threads = [threading.Thread(target=edit, args=(c, i)) for i, c in enumerate(editors)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(c.wait_synced(10) for c in editors)

    expected_text, expected_rev = server_text(running_server["port"], doc)
    assert wait_for(lambda: all(c.text == expected_text and c.rev == expected_rev
                                for c in editors))
    assert expected_rev > 0
