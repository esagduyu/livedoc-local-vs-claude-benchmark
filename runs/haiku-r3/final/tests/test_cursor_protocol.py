import pytest


def join(w, doc, client):
    w.send({"type": "join", "doc": doc, "client": client})
    return w.recv()


def send_op(w, doc, rev, op_id, op):
    w.send({"type": "op", "doc": doc, "rev": rev, "id": op_id, "op": op})


def set_cursor(w, doc, rev, pos):
    w.send({"type": "cursor", "doc": doc, "rev": rev, "pos": pos})


def query_cursors(w, doc):
    w.send({"type": "cursors", "doc": doc})
    return w.recv_until("cursors")["cursors"]


def build(w, doc, text):
    """Insert ``text`` as the first op of ``doc`` by ``w``'s client; returns the revision."""
    send_op(w, doc, 0, "build", [{"ins": 0, "text": text}])
    return w.recv_until("ack")["rev"]


def test_cursor_is_broadcast_to_others_and_not_answered(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    set_cursor(a, "d", 0, 0)
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 0, "pos": 0}
    # The sender got no reply: its next message is the answer to this query.
    assert query_cursors(a, "d") == {"alice": 0}


def test_own_typing_at_cursor_pushes_it_but_others_typing_does_not(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    rev = build(a, "d", "abc")
    b.recv_until("op")
    set_cursor(a, "d", rev, 3)
    b.recv_until("cursor")

    send_op(b, "d", rev, "bob-type", [{"ins": 3, "text": "X"}])
    b.recv_until("ack")
    assert query_cursors(b, "d") == {"alice": 3}

    send_op(a, "d", rev + 1, "alice-type", [{"ins": 4, "text": "Y"}])
    a.recv_until("ack")
    assert query_cursors(b, "d") == {"alice": 3}  # "abcX" -> cursor 3 is before X, Y is after it

    send_op(a, "d", rev + 2, "alice-at", [{"ins": 3, "text": "Z"}])
    a.recv_until("ack")
    assert query_cursors(b, "d") == {"alice": 4}


def test_deletion_moves_cursors_to_the_start_of_the_range_or_back(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    rev = build(a, "d", "abcdef")
    b.recv_until("op")
    set_cursor(a, "d", rev, 4)  # inside the range deleted below
    set_cursor(b, "d", rev, 6)  # after it
    send_op(b, "d", rev, "del", [{"del": 2, "len": 3}])
    b.recv_until("ack")
    assert query_cursors(b, "d") == {"alice": 2, "bob": 3}


def test_cursor_at_a_stale_revision_is_moved_to_the_current_one(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    rev = build(a, "d", "abcd")
    b.recv_until("op")
    send_op(b, "d", rev, "front", [{"ins": 0, "text": "ZZ"}])
    b.recv_until("ack")
    a.recv_until("op")
    set_cursor(a, "d", rev, 2)  # written against the text before "ZZ"
    msg = b.recv_until("cursor")
    assert msg == {"type": "cursor", "doc": "d", "client": "alice", "rev": 2, "pos": 4}


def test_cursors_are_delivered_in_order_with_operations(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    rev = build(a, "d", "x")
    b.recv_until("op")
    # One connection is processed in order, so the cursor precedes the op it was sent before.
    set_cursor(a, "d", rev, 1)
    send_op(a, "d", rev, "after", [{"ins": 1, "text": "y"}])
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 1, "pos": 1}
    assert b.recv()["type"] == "op"


@pytest.mark.parametrize("msg, code", [
    ({"type": "cursor", "doc": "other", "rev": 1, "pos": 0}, "not_joined"),
    ({"type": "cursor", "doc": "d", "rev": 5, "pos": 0}, "bad_rev"),
    ({"type": "cursor", "doc": "d", "rev": -1, "pos": 0}, "bad_rev"),
    ({"type": "cursor", "doc": "d", "rev": 1, "pos": 4}, "out_of_range"),
    ({"type": "cursor", "doc": "d", "rev": 1, "pos": -1}, "out_of_range"),
    ({"type": "cursor", "doc": "d", "rev": 1, "pos": "0"}, "bad_request"),
    ({"type": "cursor", "doc": "d", "pos": 0}, "bad_request"),
    ({"type": "cursor", "doc": "d", "rev": 1}, "bad_request"),
    ({"type": "cursors", "doc": "other"}, "not_joined"),
    ({"type": "cursors"}, "bad_request"),
])
def test_cursor_errors(server, connect, msg, code):
    w = connect(server.port)
    join(w, "d", "alice")
    build(w, "d", "abc")
    w.send(msg)
    reply = w.recv()
    assert reply["type"] == "error"
    assert reply["code"] == code
    assert query_cursors(w, "d") == {}  # the connection still works, and nothing was stored


def test_leave_is_sent_when_the_last_connection_of_a_name_closes(server, connect):
    a1, a2, b = connect(server.port), connect(server.port), connect(server.port)
    join(a1, "d", "alice")
    join(a2, "d", "alice")
    join(b, "d", "bob")
    set_cursor(a1, "d", 0, 0)
    assert b.recv_until("cursor")["client"] == "alice"

    a1.close()
    # Still used by a2, so there is no leave; the query gets its direct answer.
    assert query_cursors(b, "d") == {"alice": 0}

    a2.close()
    assert b.recv() == {"type": "leave", "doc": "d", "client": "alice"}
    assert query_cursors(b, "d") == {}


def test_leave_is_sent_for_a_name_that_never_set_a_cursor(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    a.close()
    assert b.recv() == {"type": "leave", "doc": "d", "client": "alice"}


def test_rejoining_under_another_name_releases_the_old_one(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    set_cursor(a, "d", 0, 0)
    join(a, "d", "carol")
    assert b.recv_until("leave") == {"type": "leave", "doc": "d", "client": "alice"}
    assert query_cursors(b, "d") == {}


def test_cursors_are_not_persisted(server, connect):
    a = connect(server.port)
    join(a, "d", "alice")
    build(a, "d", "abc")
    set_cursor(a, "d", 1, 2)
    assert query_cursors(a, "d") == {"alice": 2}

    server.restart()

    b = connect(server.port)
    join(b, "d", "bob")
    assert query_cursors(b, "d") == {}
