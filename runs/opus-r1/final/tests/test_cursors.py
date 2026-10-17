import random
import threading
import time

import pytest

from conftest import Conn
from livedoc_client import Client, move_cursor
from test_client import start_client, fake, wait_for  # noqa: F401  (fake is a fixture)


def ins(p, t):
    return {"ins": p, "text": t}


def dl(p, n):
    return {"del": p, "len": n}


# -- the moving rule -------------------------------------------------------------

@pytest.mark.parametrize("c, op, own, expected", [
    (3, [ins(1, "ab")], False, 5),
    (3, [ins(3, "ab")], False, 3),      # someone else typing at the cursor
    (3, [ins(3, "ab")], True, 5),       # own typing pushes the cursor
    (3, [ins(4, "ab")], True, 3),
    (5, [dl(1, 2)], False, 3),
    (5, [dl(3, 4)], False, 3),          # inside the deleted range: to its start
    (5, [dl(5, 2)], False, 5),
    (5, [dl(6, 2)], False, 5),
    (5, [dl(0, 5)], False, 0),
    (2, [ins(2, "x"), ins(0, "yy"), dl(0, 1)], True, 4),
])
def test_move_cursor(c, op, own, expected):
    assert move_cursor(c, op, own) == expected


# -- server ----------------------------------------------------------------------

def setup_doc(connect, text="hello world"):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    a.op("d", 0, "init", [ins(0, text)])
    b.recv()
    return a, b


def test_cursor_broadcast_and_query(connect):
    a, b = setup_doc(connect)
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 5})
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 1, "pos": 5}
    a.no_message()                                       # no reply on success
    assert b.request({"type": "cursors", "doc": "d"}) == {
        "type": "cursors", "doc": "d", "rev": 1, "cursors": {"alice": 5}}
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 0})   # replaces the previous cursor
    assert b.recv()["pos"] == 0
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 0}


def test_cursor_from_old_revision_is_moved_forward(connect):
    a, b = setup_doc(connect)                            # rev 1: "hello world"
    b.op("d", 1, "b1", [ins(5, "XX")])                   # rev 2, by bob, at alice's position
    a.recv()
    a.op("d", 1, "a1", [ins(0, ">")])                    # rev 3, by alice
    b.recv()
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 5})
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 3, "pos": 6}
    # Alice's own insert at her cursor would push it.
    a.op("d", 3, "a2", [ins(6, "!")])
    b.recv()
    assert b.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 7}


def test_stored_cursors_follow_commits(connect):
    a, b = setup_doc(connect)
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 5})
    b.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 8})
    b.recv()
    a.recv()
    b.op("d", 1, "b1", [ins(5, "__")])                   # bob types at alice's cursor
    a.recv()
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 5, "bob": 10}
    a.op("d", 2, "a1", [ins(5, "A")])                    # alice types at her own cursor
    b.recv()
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 6, "bob": 11}
    a.op("d", 3, "a2", [dl(3, 6)])                       # deletes over alice's cursor
    b.recv()
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 3, "bob": 5}


def test_cursor_is_ordered_with_op_broadcasts(connect):
    a, b = setup_doc(connect)
    c = connect()
    c.join("d", "carol")
    a.send({"type": "op", "doc": "d", "rev": 1, "id": "x", "op": [ins(0, "abc")]})
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 2})
    for conn in (b, c):
        assert conn.recv()["type"] == "op"
        # moved through alice's own insert before it
        assert conn.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 2, "pos": 5}
    assert a.recv()["type"] == "ack"


def test_cursor_errors(connect):
    a, b = setup_doc(connect, "abc")
    other = connect()
    for conn, msg, code in [
        (other, {"type": "cursor", "doc": "d", "rev": 1, "pos": 0}, "not_joined"),
        (other, {"type": "cursors", "doc": "d"}, "not_joined"),
        (a, {"type": "cursor", "doc": "d", "rev": 2, "pos": 0}, "bad_rev"),
        (a, {"type": "cursor", "doc": "d", "rev": -1, "pos": 0}, "bad_rev"),
        (a, {"type": "cursor", "doc": "d", "rev": 1, "pos": 4}, "out_of_range"),
        (a, {"type": "cursor", "doc": "d", "rev": 0, "pos": 1}, "out_of_range"),
        (a, {"type": "cursor", "doc": "d", "rev": 1, "pos": -1}, "out_of_range"),
        (a, {"type": "cursor", "doc": "d", "rev": 1}, "bad_request"),
        (a, {"type": "cursor", "doc": "d", "rev": 1, "pos": "1"}, "bad_request"),
        (a, {"type": "cursor", "doc": "d", "rev": None, "pos": 1}, "bad_request"),
        (a, {"type": "cursor", "rev": 1, "pos": 1}, "bad_request"),
        (a, {"type": "cursors"}, "bad_request"),
    ]:
        reply = conn.request(msg)
        assert reply["type"] == "error" and reply["code"] == code, (msg, reply)
    b.no_message()
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {}


def test_leave_after_last_connection_of_a_name(server, connect):
    a, b = setup_doc(connect)
    a2 = Conn(server.port)
    a2.join("d", "alice")
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 3})
    assert b.recv()["type"] == "cursor"
    assert a2.recv()["type"] == "cursor"                 # another connection, same name: still "other"
    a.close()
    b.no_message()                                       # alice is still connected through a2
    assert b.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 3}
    a2.close()
    assert b.recv() == {"type": "leave", "doc": "d", "client": "alice"}
    assert b.request({"type": "cursors", "doc": "d"})["cursors"] == {}

    # A name that never set a cursor also leaves; a connection on another document hears nothing.
    c = connect()
    c.join("d", "carol")
    c.join("e", "carol")
    e = connect()
    e.join("e", "erin")
    c.close()
    assert b.recv() == {"type": "leave", "doc": "d", "client": "carol"}
    assert e.recv() == {"type": "leave", "doc": "e", "client": "carol"}
    e.no_message()


def test_cursors_are_not_persisted(server, connect):
    a, b = setup_doc(connect)
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 3})
    b.recv()
    server.restart()
    c = Conn(server.port)
    c.join("d", "carol")
    assert c.request({"type": "cursors", "doc": "d"}) == {"type": "cursors", "doc": "d", "rev": 1, "cursors": {}}
    c.close()


# -- client ----------------------------------------------------------------------

def test_client_moves_remote_cursors_through_local_edits(fake):
    c = start_client(fake, "hello world", 1, cursors={"bob": 5, "me": 2})
    try:
        assert c.cursors == {"bob": 5}                   # never lists its own name
        c.insert(5, "!")                                 # local edit at bob's cursor: counts as not bob's
        assert c.state() == ("hello! world", 1, {"bob": 5})
        m1 = fake.recv()
        c.insert(0, ">>")                                # buffered
        assert c.cursors == {"bob": 7}

        # Bob types at his cursor (committed before our op): his cursor moves past his text.
        fake.send({"type": "op", "doc": "d", "rev": 2, "client": "bob", "id": "b", "op": [ins(5, "XY")]})
        assert wait_for(lambda: c.rev == 2)
        text, _, cursors = c.state()
        assert text == ">>helloXY! world" and cursors == {"bob": 9}
        assert text[cursors["bob"]] == "!"

        fake.send({"type": "cursor", "doc": "d", "client": "carol", "rev": 2, "pos": 0})
        fake.send({"type": "cursor", "doc": "d", "client": "me", "rev": 2, "pos": 1})
        fake.send({"type": "cursor", "doc": "other", "client": "zed", "rev": 2, "pos": 1})
        assert wait_for(lambda: "carol" in c.cursors)
        assert c.cursors == {"bob": 9, "carol": 0}       # our buffered ">>" at 0 is not carol's typing

        fake.send({"type": "ack", "doc": "d", "id": m1["id"], "rev": 3})
        m2 = fake.recv()
        assert m2["op"] == [ins(0, ">>")]
        assert c.cursors == {"bob": 9, "carol": 0}
        fake.send({"type": "ack", "doc": "d", "id": m2["id"], "rev": 4})
        assert c.wait_synced()
        assert c.cursors == {"bob": 9, "carol": 0}

        fake.send({"type": "leave", "doc": "d", "client": "bob"})
        assert wait_for(lambda: "bob" not in c.cursors)
        assert c.cursors == {"carol": 0}
    finally:
        c.close()


def test_client_set_cursor_waits_until_synced(fake):
    c = start_client(fake, "abc", 1)
    try:
        c.set_cursor(1)
        assert fake.recv() == {"type": "cursor", "doc": "d", "rev": 1, "pos": 1}
        with pytest.raises(ValueError):
            c.set_cursor(4)
        c.insert(1, "XX")                                # own typing pushes the pending cursor
        m1 = fake.recv()
        c.set_cursor(3)
        fake.nothing()                                   # not sent while an edit is unacknowledged
        c.insert(0, "-")
        fake.send({"type": "ack", "doc": "d", "id": m1["id"], "rev": 2})
        m2 = fake.recv()
        assert m2["type"] == "op"
        fake.nothing()
        fake.send({"type": "ack", "doc": "d", "id": m2["id"], "rev": 3})
        assert fake.recv() == {"type": "cursor", "doc": "d", "rev": 3, "pos": 4}
    finally:
        c.close()


def test_clients_see_cursors_and_leave(server):
    with Client("127.0.0.1", server.port, "d", "alice") as a:
        a.insert(0, "hello")
        a.set_cursor(5)
        assert a.wait_synced()
        b = Client("127.0.0.1", server.port, "d", "bob")
        assert b.cursors == {"alice": 5}                 # loaded on join
        b.set_cursor(0)
        assert wait_for(lambda: a.cursors == {"bob": 0})
        b.insert(0, "> ")
        assert wait_for(lambda: a.text == "> hello")
        assert a.cursors == {"bob": 2}                   # bob's own typing pushed his cursor
        assert b.cursors == {"alice": 7}
        b.close()
        assert wait_for(lambda: a.cursors == {})


def test_random_cursor_convergence(server):
    n = 4
    clients = [Client("127.0.0.1", server.port, "fuzz", "c%d" % i) for i in range(n)]
    errors = []

    def watch():
        # text and cursors are always consistent: every cursor lies inside the text.
        while any(t.is_alive() for t in threads):
            for cl in clients:
                text, _, cursors = cl.state()
                if any(not 0 <= p <= len(text) for p in cursors.values()):
                    errors.append((text, cursors))

    def work(cl, seed):
        rng = random.Random(seed)
        for _ in range(120):
            length = len(cl.text)
            try:
                r = rng.random()
                if r < 0.3:
                    cl.set_cursor(rng.randint(0, length))
                elif length and r < 0.55:
                    p = rng.randrange(length)
                    cl.delete(p, rng.randint(1, min(3, length - p)))
                else:
                    cl.insert(rng.randint(0, length), rng.choice(["a", "bc", "🙂"]))
            except ValueError:
                pass  # a remote op shrank the text after len() was read
            if rng.random() < 0.2:
                time.sleep(0.002)

    try:
        threads = [threading.Thread(target=work, args=(cl, i)) for i, cl in enumerate(clients)]
        for t in threads:
            t.start()
        watcher = threading.Thread(target=watch)
        watcher.start()
        for t in threads:
            t.join()
        watcher.join()
        assert not errors
        for cl in clients:
            assert cl.wait_synced(10)
        probe = Conn(server.port)
        probe.join("fuzz", "probe")
        # Wait for pending cursors to be flushed, then compare with the server's view.
        time.sleep(0.3)
        reply = probe.request({"type": "cursors", "doc": "fuzz"})
        assert wait_for(lambda: all(cl.rev == reply["rev"] for cl in clients), 10)
        for cl in clients:
            expected = {k: v for k, v in reply["cursors"].items() if k != cl.name}
            assert cl.cursors == expected
        probe.close()
    finally:
        for cl in clients:
            cl.close()
