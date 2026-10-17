"""Live cursors: the server protocol and the client's `cursors`."""

import random
import threading
import time

import pytest

from conftest import Conn
from livedoc_client import Client, move_cursor
from test_client import FakeServer, wait_converged


def ins(p, t):
    return {"ins": p, "text": t}


def dele(p, n):
    return {"del": p, "len": n}


# --- moving a cursor ---------------------------------------------------------

@pytest.mark.parametrize("pos, op, own, expected", [
    (3, [ins(1, "ab")], False, 5),
    (3, [ins(3, "ab")], False, 3),  # someone else typing at my cursor
    (3, [ins(3, "ab")], True, 5),  # my own typing pushes me
    (3, [ins(4, "ab")], True, 3),
    (3, [dele(0, 2)], False, 1),
    (3, [dele(1, 5)], False, 1),  # inside the range: to its start
    (3, [dele(3, 2)], False, 3),
    (3, [dele(2, 1)], True, 2),
    (3, [ins(0, "x"), dele(0, 4)], False, 0),  # edits apply in turn
    (0, [], True, 0),
])
def test_move_cursor(pos, op, own, expected):
    assert move_cursor(pos, op, own) == expected


# --- server -------------------------------------------------------------------

def setup_doc(connect, text="hello"):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    a.op("d", 0, "init", [ins(0, text)])
    b.recv()
    return a, b


def test_cursor_broadcast_and_query(connect):
    a, b = setup_doc(connect)
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 2})
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 1, "pos": 2}
    a.expect_silence()  # no reply on success
    assert b.request({"type": "cursors", "doc": "d"}) == {
        "type": "cursors", "doc": "d", "rev": 1, "cursors": {"alice": 2}}
    # replacing the cursor
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 5})
    assert b.recv()["pos"] == 5
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 5}


def test_cursors_move_with_ops(connect):
    a, b = setup_doc(connect)
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 2})
    b.recv()
    b.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 2})
    a.recv()
    # alice types at 2: her cursor is pushed, bob's (same spot) is not
    a.op("d", 1, "x", [ins(2, "XY")])
    b.recv()
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 4, "bob": 2}
    b.op("d", 2, "y", [dele(0, 3)])
    a.recv()
    assert a.request({"type": "cursors", "doc": "d"}) == {
        "type": "cursors", "doc": "d", "rev": 3, "cursors": {"alice": 1, "bob": 0}}


def test_cursor_at_old_revision_is_moved_forward(connect):
    a, b = setup_doc(connect)  # rev 1: "hello"
    a.op("d", 1, "1", [ins(2, "AA")])  # rev 2 by alice: "heAAllo"
    b.recv()
    b.op("d", 2, "2", [ins(0, "B")])  # rev 3 by bob: "BheAAllo"
    a.recv()
    # alice's cursor at 2 in rev 1: her own insert at 2 pushes it, bob's at 0 too
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 2})
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 3, "pos": 5}
    # bob's cursor at 2 in rev 1: alice's insert at 2 does not push it
    b.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 2})
    assert a.recv()["pos"] == 3


def test_cursor_messages_ordered_with_ops(connect):
    a, b = setup_doc(connect)
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 1})
    a.op("d", 1, "1", [ins(0, "z")])
    a.send({"type": "cursor", "doc": "d", "rev": 2, "pos": 0})
    assert [m["type"] for m in (b.recv(), b.recv(), b.recv())] == ["cursor", "op", "cursor"]


def test_cursor_errors(connect):
    a, _ = setup_doc(connect)
    c = connect()
    assert c.request({"type": "cursor", "doc": "d", "rev": 0, "pos": 0})["code"] == "not_joined"
    assert c.request({"type": "cursors", "doc": "d"})["code"] == "not_joined"
    for msg, code in [
        ({"doc": "d", "rev": 2, "pos": 0}, "bad_rev"),
        ({"doc": "d", "rev": -1, "pos": 0}, "bad_rev"),
        ({"doc": "d", "rev": 0, "pos": 1}, "out_of_range"),  # rev 0 is empty
        ({"doc": "d", "rev": 1, "pos": 6}, "out_of_range"),
        ({"doc": "d", "rev": 1, "pos": -1}, "out_of_range"),
        ({"doc": "d", "rev": 1, "pos": "1"}, "bad_request"),
        ({"doc": "d", "pos": 1}, "bad_request"),
        ({"rev": 1, "pos": 1}, "bad_request"),
    ]:
        assert a.request({"type": "cursor", **msg})["code"] == code, msg
    assert a.request({"type": "cursors"})["code"] == "bad_request"
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {}
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 5})  # still works
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 5}


def test_leave_when_last_connection_of_name_closes(server, connect):
    a, b = setup_doc(connect)
    a2 = Conn(server.port)
    a2.join("d", "alice")
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 3})
    assert b.recv()["client"] == "alice"
    assert a2.recv()["client"] == "alice"  # same name, other connection: still "other"
    a.close()
    b.expect_silence()  # alice is still here through a2
    assert b.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 3}
    a2.close()
    assert b.recv() == {"type": "leave", "doc": "d", "client": "alice"}
    assert b.request({"type": "cursors", "doc": "d"})["cursors"] == {}
    # leave is sent even without a cursor
    c = Conn(server.port)
    c.join("d", "carol")
    c.close()
    assert b.recv() == {"type": "leave", "doc": "d", "client": "carol"}


def test_cursors_are_not_persisted(server, connect):
    a, _ = setup_doc(connect)
    a.send({"type": "cursor", "doc": "d", "rev": 1, "pos": 3})
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 3}
    server.restart()
    c = Conn(server.port)
    c.join("d", "carol")
    assert c.request({"type": "cursors", "doc": "d"}) == {
        "type": "cursors", "doc": "d", "rev": 1, "cursors": {}}
    c.close()


# --- client -------------------------------------------------------------------

def wait_for(pred, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_client_sees_cursors(server):
    a = Client("127.0.0.1", server.port, "doc", "alice")
    a.insert(0, "hello world")
    a.set_cursor(5)
    assert a.wait_synced()
    b = Client("127.0.0.1", server.port, "doc", "bob")
    assert b.cursors == {"alice": 5}  # learned on join
    assert a.cursors == {}  # never lists itself
    b.set_cursor(0)
    assert wait_for(lambda: a.cursors == {"bob": 0})
    b.insert(0, ">> ")  # bob's own typing pushes bob's cursor
    assert b.wait_synced()
    assert wait_for(lambda: a.view()[0] == ">> hello world")
    assert a.cursors == {"bob": 3}
    assert b.cursors == {"alice": 8}
    b.close()
    assert wait_for(lambda: a.cursors == {})
    a.close()


def test_client_moves_remote_cursors_through_pending_edits():
    fake = FakeServer(cursors={"zed": 1, "me": 0})
    c = Client("127.0.0.1", fake.port, "d", "me")
    assert c.cursors == {"zed": 1}  # own name dropped
    c.insert(1, "XY")  # in flight; zed's cursor is at the insert point: stays
    first = fake.recv()
    assert (c.text, c.cursors) == ("aXYbc", {"zed": 1})
    c.insert(0, "Q")  # buffered: pushes zed
    assert (c.text, c.cursors) == ("QaXYbc", {"zed": 2})
    # zed's cursor update at rev 7 is moved through in-flight + buffer
    fake.send({"type": "cursor", "doc": "d", "client": "zed", "rev": 7, "pos": 3})
    assert wait_for(lambda: c.cursors == {"zed": 6})
    # zed types at their cursor (rev 8): pushes their own cursor
    fake.send({"type": "op", "doc": "d", "rev": 8, "client": "zed", "id": "z", "op": [ins(3, "!")]})
    assert wait_for(lambda: c.rev == 8)
    assert (c.text, c.cursors) == ("QaXYbc!", {"zed": 7})
    fake.send({"type": "ack", "doc": "d", "id": first["id"], "rev": 9})
    second = fake.recv()
    assert second["rev"] == 9 and second["op"] == [ins(0, "Q")]
    assert (c.text, c.cursors) == ("QaXYbc!", {"zed": 7})
    fake.send({"type": "leave", "doc": "d", "client": "zed"})
    fake.send({"type": "leave", "doc": "other", "client": "x"})
    assert wait_for(lambda: c.cursors == {})
    c.close()
    fake.close()


def test_set_cursor_waits_for_acknowledgement():
    fake = FakeServer()
    c = Client("127.0.0.1", fake.port, "d", "me")
    c.insert(3, "!")  # "abc!" in flight
    first = fake.recv()
    c.set_cursor(4)
    with pytest.raises(ValueError):
        c.set_cursor(5)
    fake.expect_nothing()  # can't be expressed at a server revision yet
    fake.send({"type": "op", "doc": "d", "rev": 8, "client": "z", "id": "z", "op": [ins(0, ">")]})
    fake.send({"type": "ack", "doc": "d", "id": first["id"], "rev": 9})
    assert fake.recv() == {"type": "cursor", "doc": "d", "rev": 9, "pos": 5}
    assert c.wait_synced()
    c.close()
    fake.close()


def test_client_cursors_match_server_under_load(server):
    clients = [Client("127.0.0.1", server.port, "busy", f"u{i}") for i in range(4)]
    stop = threading.Event()
    bad = []

    def watcher():
        while not stop.is_set():
            for c in clients:
                text, _, cursors = c.view()
                if any(not 0 <= p <= len(text) for p in cursors.values()):
                    bad.append((text, cursors))

    def typist(i, c):
        rng = random.Random(i)
        for _ in range(120):
            n = len(c.text)
            try:
                r = rng.random()
                if r < 0.3:
                    c.set_cursor(rng.randint(0, n))
                elif n and r < 0.55:
                    p = rng.randrange(n)
                    c.delete(p, rng.randint(1, min(3, n - p)))
                else:
                    c.insert(rng.randint(0, n), rng.choice(["a", "bc", "😀"]))
            except ValueError:
                pass  # the text changed between reading its length and editing

    w = threading.Thread(target=watcher)
    w.start()
    threads = [threading.Thread(target=typist, args=(i, c)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = wait_converged(clients, server.port, "busy")
    stop.set()
    w.join()
    assert not bad
    server_cursors = Conn(server.port)
    server_cursors.join("busy", "observer")
    truth = server_cursors.request({"type": "cursors", "doc": "busy"})
    assert truth["rev"] == expected["rev"]
    for c in clients:
        # cursor messages don't advance the revision, so they may still be on the way
        want = {n: p for n, p in truth["cursors"].items() if n != c.name}
        assert wait_for(lambda: c.cursors == want), (c.name, c.cursors, want)
        text, rev, cursors = c.view()
        assert (text, rev) == (expected["text"], expected["rev"])
    for c in clients:
        c.close()
    server_cursors.close()
