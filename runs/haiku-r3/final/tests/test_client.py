import json
import random
import socket
import threading
import time

import pytest

from livedoc_client import Client


class FakeServer:
    """A scripted server for one connection, so tests control every reply."""

    def __init__(self, text="", rev=0):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.text, self.rev = text, rev
        self.cursors = {}
        self.conn = None
        self.file = None

    def accept_join(self):
        """Accept the client and answer its join. Blocks until done."""
        self.conn, _ = self.listener.accept()
        self.conn.settimeout(5)
        self.file = self.conn.makefile("rb")
        join = json.loads(self.file.readline())
        assert join["type"] == "join"
        self.send({"type": "joined", "doc": join["doc"], "rev": self.rev, "text": self.text})
        # The client asks for existing cursors right after joining.
        query = json.loads(self.file.readline())
        assert query == {"type": "cursors", "doc": join["doc"]}
        self.send({"type": "cursors", "doc": join["doc"], "rev": self.rev, "cursors": self.cursors})
        return join

    def send(self, obj):
        self.conn.sendall((json.dumps(obj) + "\n").encode())

    def recv(self):
        return json.loads(self.file.readline())

    def close(self):
        for s in (self.file, self.conn, self.listener):
            if s is not None:
                s.close()


def connect_fake(fake, doc="d", name="me", timeout=5.0):
    """Create a Client against a FakeServer, serving the join from another thread."""
    joiner = threading.Thread(target=fake.accept_join)
    joiner.start()
    try:
        return Client("127.0.0.1", fake.port, doc, name, timeout=timeout)
    finally:
        joiner.join()


@pytest.fixture
def fake():
    f = FakeServer()
    yield f
    f.close()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_join_returns_current_text_and_revision(server, connect):
    w = connect(server.port)
    w.send({"type": "join", "doc": "d", "client": "seed"})
    w.recv()
    w.send({"type": "op", "doc": "d", "rev": 0, "id": "1", "op": [{"ins": 0, "text": "hello"}]})
    w.recv()

    c = Client("127.0.0.1", server.port, "d", "alice")
    try:
        assert c.text == "hello"
        assert c.rev == 1
    finally:
        c.close()


def test_local_edit_applies_immediately_and_syncs(server):
    c = Client("127.0.0.1", server.port, "d", "alice")
    try:
        c.insert(0, "abc")
        assert c.text == "abc"
        c.delete(1, 1)
        assert c.text == "ac"
        assert c.wait_synced()
        assert c.rev == 2
        other = Client("127.0.0.1", server.port, "d", "bob")
        try:
            assert other.text == "ac"
            assert other.rev == 2
        finally:
            other.close()
    finally:
        c.close()


@pytest.mark.parametrize("call", [
    lambda c: c.insert(4, "x"),  # past the end of "abc"
    lambda c: c.insert(-1, "x"),
    lambda c: c.insert(0, ""),
    lambda c: c.insert("0", "x"),
    lambda c: c.insert(0, 5),
    lambda c: c.delete(0, 0),
    lambda c: c.delete(2, 2),
    lambda c: c.delete(-1, 1),
])
def test_invalid_edits_raise_value_error_and_change_nothing(fake, call):
    fake.text, fake.rev = "abc", 0
    c = connect_fake(fake)
    try:
        with pytest.raises(ValueError):
            call(c)
        assert c.text == "abc"
        assert c.rev == 0
    finally:
        c.close()


def test_remote_op_arriving_during_flight_is_transformed_and_local_edit_kept(fake):
    fake.text, fake.rev = "abc", 0
    c = connect_fake(fake)
    try:
        c.insert(1, "X")  # sent at revision 0, now in flight
        sent = fake.recv()
        assert sent["rev"] == 0
        assert sent["op"] == [{"ins": 1, "text": "X"}]

        # Another user inserted "Z" at 0 and was committed first.
        fake.send({"type": "op", "doc": "d", "rev": 1, "client": "bob", "id": "b1",
                   "op": [{"ins": 0, "text": "Z"}]})
        assert wait_for(lambda: c.rev == 1)
        assert c.text == "ZaXbc"
        assert not c.wait_synced(0.1)

        fake.send({"type": "ack", "doc": "d", "id": sent["id"], "rev": 2})
        assert c.wait_synced(5)
        assert c.rev == 2
        assert c.text == "ZaXbc"
    finally:
        c.close()


def test_edits_made_during_flight_are_buffered_and_sent_after_ack(fake):
    fake.text, fake.rev = "abc", 0
    c = connect_fake(fake)
    try:
        c.insert(0, "1")
        first = fake.recv()
        c.insert(0, "2")  # buffered, must not be sent yet
        c.insert(0, "3")  # buffered behind it
        assert c.text == "321abc"
        assert not c.wait_synced(0.1)

        fake.send({"type": "ack", "doc": "d", "id": first["id"], "rev": 1})
        second = fake.recv()
        assert second["type"] == "op"
        assert second["rev"] == 1
        assert second["op"] == [{"ins": 0, "text": "2"}, {"ins": 0, "text": "3"}]
        assert c.text == "321abc"
        assert c.rev == 1

        fake.send({"type": "ack", "doc": "d", "id": second["id"], "rev": 2})
        assert c.wait_synced(5)
        assert c.rev == 2
        assert c.text == "321abc"
    finally:
        c.close()


def test_remote_ops_for_other_documents_are_ignored(fake):
    fake.text, fake.rev = "abc", 0
    c = connect_fake(fake)
    try:
        fake.send({"type": "op", "doc": "other", "rev": 1, "client": "bob", "id": "b",
                   "op": [{"ins": 0, "text": "Z"}]})
        fake.send({"type": "op", "doc": "d", "rev": 1, "client": "bob", "id": "b",
                   "op": [{"ins": 3, "text": "!"}]})
        assert wait_for(lambda: c.rev == 1)
        assert c.text == "abc!"
    finally:
        c.close()


def test_join_times_out_when_server_never_answers():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        start = time.monotonic()
        with pytest.raises((TimeoutError, OSError)):
            Client("127.0.0.1", port, "d", "alice", timeout=0.3)
        assert time.monotonic() - start < 3
    finally:
        listener.close()


def test_join_error_from_server_raises(server):
    with pytest.raises(RuntimeError):
        Client("127.0.0.1", server.port, "", "alice")


def test_wait_synced_true_when_nothing_pending(server):
    c = Client("127.0.0.1", server.port, "d", "alice")
    try:
        assert c.wait_synced(0.1)
    finally:
        c.close()


def test_context_manager_closes_connection(server):
    with Client("127.0.0.1", server.port, "d", "alice") as c:
        c.insert(0, "x")
        assert c.wait_synced()
    with pytest.raises(RuntimeError):
        c.insert(0, "y")


def test_two_clients_editing_the_same_spot_converge(server):
    a = Client("127.0.0.1", server.port, "d", "alice")
    b = Client("127.0.0.1", server.port, "d", "bob")
    try:
        a.insert(0, "a" * 3)
        b.insert(0, "b" * 3)
        assert a.wait_synced() and b.wait_synced()
        # Each client still has to receive the other's broadcast.
        assert wait_for(lambda: a.rev == 2 and b.rev == 2)
        assert a.text == b.text
        assert sorted(a.text) == sorted("aaabbb")
    finally:
        a.close()
        b.close()


def test_random_concurrent_editing_converges(server):
    names = ["ann", "ben", "cy", "dee"]
    clients = [Client("127.0.0.1", server.port, "stress", n) for n in names]
    errors = []

    def edit(client, seed):
        rng = random.Random(seed)
        try:
            for _ in range(40):
                for _attempt in range(100):
                    try:
                        n = len(client.text)
                        if n == 0 or rng.random() < 0.6:
                            client.insert(rng.randint(0, n), rng.choice(["x", "yz", "Ω"]))
                        else:
                            pos = rng.randint(0, n - 1)
                            client.delete(pos, rng.randint(1, min(3, n - pos)))
                        break
                    except ValueError:
                        continue  # a remote edit shrank the text under us
                time.sleep(rng.random() * 0.003)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=edit, args=(c, i)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        assert not errors
        assert all(c.wait_synced(30) for c in clients)

        checker = Client("127.0.0.1", server.port, "stress", "checker")
        try:
            assert wait_for(lambda: all(c.rev == checker.rev for c in clients), timeout=30)
            assert all(c.text == checker.text for c in clients)
        finally:
            checker.close()
    finally:
        for c in clients:
            c.close()
