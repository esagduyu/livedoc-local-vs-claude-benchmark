import random
import threading
import time

import pytest

from conftest import Raw, connect, wait_until
from livedoc_client import Client, transform_cursor


def op_msg(doc, rev, op_id, op):
    return {"type": "op", "doc": doc, "rev": rev, "id": op_id, "op": op}


def cursor_msg(doc, rev, pos):
    return {"type": "cursor", "doc": doc, "rev": rev, "pos": pos}


def ins(p, t):
    return {"ins": p, "text": t}


def dele(p, n):
    return {"del": p, "len": n}


# -- cursor movement rule -------------------------------------------------------

@pytest.mark.parametrize("c, op, own, expected", [
    (5, [ins(2, "abc")], False, 8),
    (5, [ins(5, "abc")], False, 5),     # someone else typing at the cursor
    (5, [ins(5, "abc")], True, 8),      # own typing pushes the cursor
    (5, [ins(6, "abc")], True, 5),
    (5, [dele(1, 2)], False, 3),
    (5, [dele(3, 4)], False, 3),        # inside the deleted range -> its start
    (5, [dele(5, 2)], False, 5),        # delete starting at the cursor
    (5, [dele(4, 1)], False, 4),
    (0, [dele(0, 3)], False, 0),
    (5, [ins(0, "x"), dele(0, 3), ins(3, "yy")], True, 5),  # edits in turn: 6, 3, 5
    (5, [], False, 5),
])
def test_transform_cursor(c, op, own, expected):
    assert transform_cursor(c, op, own) == expected


# -- server -------------------------------------------------------------------

def test_set_and_query_cursor(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    a.request(op_msg("d", 0, "1", [ins(0, "hello")]))
    b.recv()
    a.send(cursor_msg("d", 1, 3))
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 1, "pos": 3}
    a.assert_silent()  # no reply on success
    assert a.request({"type": "cursors", "doc": "d"}) == \
        {"type": "cursors", "doc": "d", "rev": 1, "cursors": {"alice": 3}}
    a.send(cursor_msg("d", 1, 5))  # replaces the previous cursor
    b.recv()
    assert b.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 5}


def test_stale_cursor_is_moved_to_current_revision(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    a.request(op_msg("d", 0, "1", [ins(0, "abcdef")]))
    b.recv()
    b.request(op_msg("d", 1, "2", [ins(3, "XX")]))      # bob types at 3
    a.recv()
    a.request(op_msg("d", 2, "3", [ins(5, "YYY")]))     # alice types at 5 (old 3)
    b.recv()
    # Alice's cursor at old position 3, rev 1: bob's insert at 3 leaves it, alice's
    # own insert at 3+2=5 pushes it.
    a.send(cursor_msg("d", 1, 3))
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 3, "pos": 3}
    a.send(cursor_msg("d", 2, 5))
    assert b.recv()["pos"] == 8
    # A cursor inside a later-deleted range lands on the range start.
    b.send(cursor_msg("d", 1, 2))
    assert a.recv()["pos"] == 2


def test_stored_cursors_follow_commits(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    a.request(op_msg("d", 0, "1", [ins(0, "0123456789")]))
    b.recv()
    a.send(cursor_msg("d", 1, 4))
    b.recv()
    b.send(cursor_msg("d", 1, 8))
    a.recv()
    b.request(op_msg("d", 1, "2", [ins(4, "bb"), ins(10, "BB")]))  # at alice's cursor; at bob's own
    a.recv()
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 4, "bob": 12}
    a.request(op_msg("d", 2, "3", [dele(2, 5)]))
    b.recv()
    assert a.request({"type": "cursors", "doc": "d"}) == \
        {"type": "cursors", "doc": "d", "rev": 3, "cursors": {"alice": 2, "bob": 7}}


def test_cursor_broadcast_is_ordered_with_ops(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    for i in range(20):
        a.send(op_msg("d", i, str(i), [ins(0, "x")]))
        a.send(cursor_msg("d", i + 1, 1))
    for i in range(20):
        assert b.recv() == {"type": "op", "doc": "d", "rev": i + 1, "client": "alice",
                            "id": str(i), "op": [ins(0, "x")]}
        assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice",
                            "rev": i + 1, "pos": 1}


@pytest.mark.parametrize("msg, code", [
    (cursor_msg("other", 0, 0), "not_joined"),
    ({"type": "cursors", "doc": "other"}, "not_joined"),
    (cursor_msg("d", 2, 0), "bad_rev"),
    (cursor_msg("d", -1, 0), "bad_rev"),
    (cursor_msg("d", 1, 4), "out_of_range"),
    (cursor_msg("d", 1, -1), "out_of_range"),
    (cursor_msg("d", 0, 1), "out_of_range"),   # valid now, but not at rev 0
    (cursor_msg("d", 1, "1"), "bad_request"),
    (cursor_msg("d", 1, True), "bad_request"),
    ({"type": "cursor", "doc": "d", "pos": 0}, "bad_request"),
    ({"type": "cursor", "doc": "d", "rev": 1}, "bad_request"),
    (cursor_msg("", 1, 0), "bad_request"),
    ({"type": "cursors"}, "bad_request"),
])
def test_cursor_errors(raw, msg, code):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    a.request(op_msg("d", 0, "1", [ins(0, "abc")]))
    b.recv()
    reply = a.request(msg)
    assert reply["type"] == "error" and reply["code"] == code, reply
    b.assert_silent()
    assert a.request({"type": "cursors", "doc": "d"})["cursors"] == {}


def test_leave_when_last_connection_of_a_name_closes(raw, server):
    watcher = raw()
    watcher.join("d", "w")
    a1, a2 = Raw(server.port), Raw(server.port)
    a1.join("d", "alice")
    a2.join("d", "alice")
    a1.send(cursor_msg("d", 0, 0))
    assert watcher.recv()["type"] == "cursor"
    a2.recv()
    a1.close()
    watcher.assert_silent()  # alice is still connected through a2
    assert watcher.request({"type": "cursors", "doc": "d"})["cursors"] == {"alice": 0}
    a2.close()
    assert watcher.recv() == {"type": "leave", "doc": "d", "client": "alice"}
    assert watcher.request({"type": "cursors", "doc": "d"})["cursors"] == {}
    # Leave is announced even if the name never set a cursor.
    bob = Raw(server.port)
    bob.join("d", "bob")
    bob.close()
    assert watcher.recv() == {"type": "leave", "doc": "d", "client": "bob"}


def test_leave_is_per_document(raw, server):
    w1, w2 = raw(), raw()
    w1.join("one", "w")
    w2.join("two", "w")
    a = Raw(server.port)
    a.join("one", "alice")
    a.join("two", "alice2")
    a.close()
    assert w1.recv() == {"type": "leave", "doc": "one", "client": "alice"}
    assert w2.recv() == {"type": "leave", "doc": "two", "client": "alice2"}


def test_rejoin_under_another_name_leaves_the_old_one(raw):
    w, a = raw(), raw()
    w.join("d", "w")
    a.join("d", "alice")
    a.send(cursor_msg("d", 0, 0))
    w.recv()
    a.join("d", "alicia")
    assert w.recv() == {"type": "leave", "doc": "d", "client": "alice"}
    assert w.request({"type": "cursors", "doc": "d"})["cursors"] == {}


def test_cursors_are_not_persisted(server, raw):
    a = raw()
    a.join("d", "alice")
    a.request(op_msg("d", 0, "1", [ins(0, "abc")]))
    a.send(cursor_msg("d", 1, 2))
    a.request({"type": "cursors", "doc": "d"})  # make sure the cursor was processed
    server.restart()
    b = Raw(server.port)
    try:
        b.join("d", "bob")
        assert b.request({"type": "cursors", "doc": "d"}) == \
            {"type": "cursors", "doc": "d", "rev": 1, "cursors": {}}
    finally:
        b.close()


# -- client, against a scripted fake server -----------------------------------

def test_client_moves_remote_cursors_through_local_edits(fake):
    c = connect(fake, text="0123456789", rev=1)
    try:
        c.insert(5, "AB")                      # in flight
        inflight = fake.recv()
        c.delete(0, 2)                         # buffered: "234AB56789"
        fake.send({"type": "cursor", "doc": "d", "client": "x", "rev": 1, "pos": 5})
        fake.send({"type": "cursor", "doc": "d", "client": "y", "rev": 1, "pos": 1})
        fake.send({"type": "cursor", "doc": "d", "client": "me", "rev": 1, "pos": 0})
        # x at 5: our insert at 5 doesn't push it (it's not x's edit), delete shifts it.
        assert wait_until(lambda: c.cursors == {"x": 3, "y": 0})
        # A remote op by x at x's cursor pushes x's cursor; y's insert at y's does too.
        fake.send({"type": "op", "doc": "d", "rev": 2, "client": "x", "id": "r",
                   "op": [ins(5, "xx"), ins(1, "y")]})
        assert wait_until(lambda: c.rev == 2)
        # Server text is now "0y1234xx56789"; x's cursor there is 5 -> 7 (own insert
        # at the cursor) -> 8, y's stays at 1 (x's insert at y's cursor). Locally our
        # deletion of "01" removes the characters around "y".
        assert c.text == "y234xxAB56789"
        assert c.cursors == {"x": 6, "y": 0}
        fake.send({"type": "ack", "doc": "d", "id": inflight["id"], "rev": 3})
        second = fake.recv()
        assert c.cursors == {"x": 6, "y": 0}
        fake.send({"type": "leave", "doc": "d", "client": "y"})
        assert wait_until(lambda: c.cursors == {"x": 6})
        fake.send({"type": "ack", "doc": "d", "id": second["id"], "rev": 4})
        assert c.wait_synced()
        assert c.cursors == {"x": 6} and c.text[6:8] == "AB"
    finally:
        c.close()


def test_client_loads_cursors_on_join(fake):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("c", Client("127.0.0.1", fake.port, "d", "me")))
    t.start()
    fake.listener.settimeout(5)
    fake.conn, _ = fake.listener.accept()
    fake.recv()
    fake.send({"type": "joined", "doc": "d", "rev": 3, "text": "abcdef"})
    t.join(5)
    c = result["c"]
    try:
        assert fake.recv() == {"type": "cursors", "doc": "d"}
        c.insert(0, "Z")
        fake.recv()
        fake.send({"type": "cursors", "doc": "d", "rev": 3,
                   "cursors": {"x": 0, "y": 6, "me": 2}})
        assert wait_until(lambda: c.cursors == {"x": 0, "y": 7})
        fake.send({"type": "cursor", "doc": "other", "client": "q", "rev": 3, "pos": 0})
        fake.send({"type": "leave", "doc": "other", "client": "x"})
        time.sleep(0.2)
        assert c.cursors == {"x": 0, "y": 7}
    finally:
        c.close()


def test_set_cursor_waits_until_synced(fake):
    c = connect(fake, text="abc", rev=1)
    try:
        c.set_cursor(1)
        assert fake.recv() == {"type": "cursor", "doc": "d", "rev": 1, "pos": 1}
        c.insert(1, "XY")
        op = fake.recv()
        c.set_cursor(3)                       # after "aXY"
        c.insert(3, "Z")                      # own typing pushes it to 4
        fake.send({"type": "op", "doc": "d", "rev": 2, "client": "o", "id": "o",
                   "op": [ins(0, "__")]})
        fake.assert_silent()
        fake.send({"type": "ack", "doc": "d", "id": op["id"], "rev": 3})
        op2 = fake.recv()
        assert op2["op"] == [ins(5, "Z")]
        fake.assert_silent()
        fake.send({"type": "ack", "doc": "d", "id": op2["id"], "rev": 4})
        assert fake.recv() == {"type": "cursor", "doc": "d", "rev": 4, "pos": 6}
        assert c.text == "__aXYZbc"
        with pytest.raises(ValueError):
            c.set_cursor(9)
    finally:
        c.close()


# -- client, against the real server ------------------------------------------

def test_clients_see_each_others_cursors(server):
    a = Client("127.0.0.1", server.port, "d", "alice")
    b = Client("127.0.0.1", server.port, "d", "bob")
    try:
        a.insert(0, "hello world")
        assert a.wait_synced()
        assert wait_until(lambda: b.text == "hello world")
        b.set_cursor(6)
        assert wait_until(lambda: a.cursors == {"bob": 6})
        assert b.cursors == {}
        a.insert(0, ">> ")
        assert a.cursors == {"bob": 9}       # moved by our local edit at once
        a.insert(9, "big ")                  # typing exactly at bob's cursor
        assert a.cursors == {"bob": 9}
        assert a.wait_synced()
        assert wait_until(lambda: b.text == a.text)
        c = Client("127.0.0.1", server.port, "d", "carol")
        try:
            assert wait_until(lambda: c.cursors == {"bob": 9})
            b.close()
            assert wait_until(lambda: c.cursors == {} and a.cursors == {})
        finally:
            c.close()
    finally:
        a.close()
        b.close()


def test_randomized_cursors_match_server(server):
    names = ["c0", "c1", "c2"]
    clients = [Client("127.0.0.1", server.port, "fz", n) for n in names]
    errors = []

    def worker(i, c):
        rng = random.Random(100 + i)
        try:
            for _ in range(120):
                text = c.text
                r = rng.random()
                try:
                    if r < 0.2:
                        c.set_cursor(rng.randint(0, len(text)))
                    elif text and r < 0.5:
                        p = rng.randrange(len(text))
                        c.delete(p, rng.randint(1, min(3, len(text) - p)))
                    else:
                        c.insert(rng.randint(0, len(text)), rng.choice(["a", "bc", "😀"]))
                except ValueError:
                    pass  # the text changed underneath us
                assert all(isinstance(p, int) and p >= 0 for p in c.cursors.values())
                if rng.random() < 0.1:
                    time.sleep(0.002)
            c.set_cursor(rng.randint(0, len(c.text)))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert not errors
        for c in clients:
            assert c.wait_synced(10)
        def server_cursors():
            observer = Raw(server.port)
            try:
                observer.join("fz", "observer")
                reply = observer.request({"type": "cursors", "doc": "fz"})
                return reply["rev"], reply["cursors"]
            finally:
                observer.close()

        def consistent():
            rev, cursors = server_cursors()
            return len(cursors) == 3 and all(
                c.rev == rev and c.cursors == {n: p for n, p in cursors.items() if n != c.name}
                for c in clients)
        assert wait_until(consistent, 10), [(c.rev, c.cursors) for c in clients]
        for c in clients:
            assert all(0 <= p <= len(c.text) for p in c.cursors.values())
    finally:
        for c in clients:
            c.close()
