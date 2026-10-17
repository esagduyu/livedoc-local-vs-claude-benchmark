import random
import threading
import time

import pytest

from livedoc_client import Client
from test_client import FakeServer, connect_fake, fake, wait_for  # noqa: F401  (shared helpers and fixture)


def test_existing_cursors_are_listed_after_join_without_own_name(fake):
    fake.text, fake.rev = "abc", 0
    fake.cursors = {"bob": 2, "me": 1}
    c = connect_fake(fake, name="me")
    try:
        assert wait_for(lambda: c.cursors == {"bob": 2})
    finally:
        c.close()


def test_cursor_and_leave_messages_update_the_list(fake):
    fake.text, fake.rev = "abc", 0
    c = connect_fake(fake, name="me")
    try:
        fake.send({"type": "cursor", "doc": "d", "client": "bob", "rev": 0, "pos": 1})
        assert wait_for(lambda: c.cursors == {"bob": 1})
        fake.send({"type": "cursor", "doc": "d", "client": "me", "rev": 0, "pos": 2})
        fake.send({"type": "cursor", "doc": "d", "client": "bob", "rev": 0, "pos": 3})
        assert wait_for(lambda: c.cursors == {"bob": 3})  # own name never shown
        fake.send({"type": "leave", "doc": "d", "client": "bob"})
        assert wait_for(lambda: c.cursors == {})
    finally:
        c.close()


def test_remote_cursor_follows_local_and_remote_edits_like_the_server(fake):
    # "abc" with bob's cursor between b and c.
    fake.text, fake.rev = "abc", 0
    fake.cursors = {"bob": 2}
    c = connect_fake(fake, name="me")
    try:
        assert wait_for(lambda: c.cursors == {"bob": 2})

        c.insert(0, "X")  # in flight; "Xabc"
        op1 = fake.recv()
        assert c.cursors == {"bob": 3}

        c.insert(3, "Y")  # buffered; "XabYc". Typing at bob's cursor does not push it.
        assert c.text == "XabYc"
        assert c.cursors == {"bob": 3}

        # carol typed "Q" at the start, and the server commits it before our op.
        fake.send({"type": "op", "doc": "d", "rev": 1, "client": "carol", "id": "q",
                   "op": [{"ins": 0, "text": "Q"}]})
        assert wait_for(lambda: c.rev == 1)
        assert c.text == "QXabYc"
        assert c.cursors == {"bob": 4}

        fake.send({"type": "ack", "doc": "d", "id": op1["id"], "rev": 2})
        op2 = fake.recv()
        assert op2["op"] == [{"ins": 4, "text": "Y"}]
        assert op2["rev"] == 2
        assert c.cursors == {"bob": 4}

        fake.send({"type": "ack", "doc": "d", "id": op2["id"], "rev": 3})
        assert c.wait_synced(5)
        assert c.text == "QXabYc"
        assert c.cursors == {"bob": 4}
    finally:
        c.close()


def test_set_cursor_sends_the_revision_and_position(fake):
    fake.text, fake.rev = "abc", 0
    c = connect_fake(fake, name="me")
    try:
        c.set_cursor(2)
        assert fake.recv() == {"type": "cursor", "doc": "d", "rev": 0, "pos": 2}
        with pytest.raises(ValueError):
            c.set_cursor(9)
        with pytest.raises(ValueError):
            c.set_cursor("2")
    finally:
        c.close()


def test_set_cursor_refused_while_edits_are_pending(fake):
    fake.text, fake.rev = "abc", 0
    c = connect_fake(fake, name="me")
    try:
        c.insert(0, "X")
        fake.recv()  # the op is in flight and not acknowledged
        with pytest.raises(RuntimeError):
            c.set_cursor(1)
    finally:
        c.close()


def test_own_cursor_is_not_listed_on_a_real_server(server):
    a = Client("127.0.0.1", server.port, "d", "alice")
    b = Client("127.0.0.1", server.port, "d", "bob")
    try:
        a.insert(0, "hello")
        assert a.wait_synced()
        assert wait_for(lambda: b.rev == 1)
        b.set_cursor(3)
        assert wait_for(lambda: a.cursors == {"bob": 3})
        a.set_cursor(0)
        assert "alice" not in a.cursors
        assert wait_for(lambda: b.cursors == {"alice": 0})
    finally:
        a.close()
        b.close()


def test_cursor_disappears_when_its_client_closes(server):
    a = Client("127.0.0.1", server.port, "d", "alice")
    b = Client("127.0.0.1", server.port, "d", "bob")
    try:
        b.set_cursor(0)
        assert wait_for(lambda: a.cursors == {"bob": 0})
        b.close()
        assert wait_for(lambda: a.cursors == {})
    finally:
        a.close()


def test_random_editing_with_cursors_converges_to_the_server_view(server):
    names = ["ann", "ben", "cy"]
    clients = [Client("127.0.0.1", server.port, "stress", n) for n in names]
    errors = []

    def work(client, seed):
        rng = random.Random(seed)
        try:
            for _ in range(30):
                for _attempt in range(100):
                    try:
                        n = len(client.text)
                        if n == 0 or rng.random() < 0.6:
                            client.insert(rng.randint(0, n), rng.choice(["x", "yz"]))
                        else:
                            pos = rng.randint(0, n - 1)
                            client.delete(pos, rng.randint(1, min(3, n - pos)))
                        break
                    except ValueError:
                        continue
                if rng.random() < 0.4:
                    for _attempt in range(100):
                        client.wait_synced(30)
                        try:
                            client.set_cursor(rng.randint(0, len(client.text)))
                            break
                        except (ValueError, RuntimeError):
                            continue
                time.sleep(rng.random() * 0.003)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(c, i)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        assert not errors
        assert all(c.wait_synced(30) for c in clients)

        checker = Client("127.0.0.1", server.port, "stress", "checker")
        try:
            def settled():
                return all(c.rev == checker.rev and c.text == checker.text
                           and c.cursors == {k: v for k, v in checker.cursors.items() if k != c.name}
                           for c in clients)
            assert wait_for(settled, timeout=30)
        finally:
            checker.close()
    finally:
        for c in clients:
            c.close()
