import signal

from conftest import Wire, start_server, stop_server


def join(port, doc, name, since=None):
    w = Wire(port)
    msg = {"type": "join", "doc": doc, "client": name}
    if since is not None:
        msg["since"] = since
    w.send(msg)
    reply = w.recv()
    assert reply["type"] == "joined"
    return w, reply


def op(w, doc, rev, op_id, ops):
    w.send({"type": "op", "doc": doc, "rev": rev, "id": op_id, "op": ops})


def test_join_empty_document(server):
    port, _ = server
    _, reply = join(port, "d", "alice")
    assert reply == {"type": "joined", "doc": "d", "rev": 0, "text": ""}


def test_op_is_acked_and_broadcast(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    op(a, "d", 0, "1", [{"ins": 0, "text": "hello"}])
    assert a.recv() == {"type": "ack", "doc": "d", "id": "1", "rev": 1}
    assert b.recv() == {"type": "op", "doc": "d", "rev": 1, "client": "alice", "id": "1",
                        "op": [{"ins": 0, "text": "hello"}]}


def test_stale_op_is_transformed(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    op(a, "d", 0, "1", [{"ins": 0, "text": "abc"}])
    a.recv()
    b.recv()
    op(b, "d", 0, "2", [{"ins": 0, "text": "X"}])  # based on the empty text
    assert b.recv() == {"type": "ack", "doc": "d", "id": "2", "rev": 2}
    received = a.recv()
    assert received["op"] == [{"ins": 3, "text": "X"}]
    _, reply = join(port, "d", "carol")
    assert reply["text"] == "abcX"


def test_exactly_once_returns_original_revision(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    op(a, "d", 0, "x", [{"ins": 0, "text": "a"}])
    assert a.recv()["rev"] == 1
    op(a, "d", 1, "y", [{"ins": 1, "text": "b"}])
    assert a.recv()["rev"] == 2
    op(a, "d", 0, "x", [{"ins": 0, "text": "a"}])
    assert a.recv() == {"type": "ack", "doc": "d", "id": "x", "rev": 1}
    _, reply = join(port, "d", "carol")
    assert reply["text"] == "ab"


def test_errors_keep_connection_open(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    a.send(b"not json")
    assert a.recv()["code"] == "bad_json"
    a.send(b"[1, 2]")
    assert a.recv()["code"] == "bad_json"
    a.send({"type": "frobnicate"})
    assert a.recv()["code"] == "bad_request"
    op(a, "other", 0, "1", [])
    assert a.recv()["code"] == "not_joined"
    op(a, "d", 5, "2", [])
    assert a.recv()["code"] == "bad_rev"
    op(a, "d", -1, "2", [])
    assert a.recv()["code"] == "bad_rev"
    op(a, "d", 0, "3", [{"ins": 4, "text": "x"}])
    error = a.recv()
    assert error["code"] == "out_of_range" and error["id"] == "3"
    op(a, "d", 0, "4", [{"ins": 0, "text": ""}])
    assert a.recv()["code"] == "out_of_range"
    op(a, "d", 0, "5", [{"del": 0, "len": 0}])
    assert a.recv()["code"] == "out_of_range"
    op(a, "d", 0, "6", [{"ins": "0", "text": "x"}])
    assert a.recv()["code"] == "bad_request"
    op(a, "d", 0, "7", [{"ins": 0, "text": "ok"}])
    assert a.recv() == {"type": "ack", "doc": "d", "id": "7", "rev": 1}


def test_out_of_range_is_checked_at_the_base_revision(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    op(a, "d", 0, "1", [{"ins": 0, "text": "abcdef"}])
    a.recv()
    op(a, "d", 1, "2", [{"del": 0, "len": 6}])
    a.recv()
    # Valid at revision 1 (text "abcdef"), not at revision 2 (empty text).
    op(a, "d", 1, "3", [{"del": 4, "len": 2}])
    assert a.recv() == {"type": "ack", "doc": "d", "id": "3", "rev": 3}


def test_since_returns_history(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    op(a, "d", 0, "1", [{"ins": 0, "text": "ab"}])
    a.recv()
    op(a, "d", 1, "2", [{"del": 0, "len": 1}])
    a.recv()
    _, reply = join(port, "d", "bob", since=1)
    assert reply == {"type": "joined", "doc": "d", "rev": 2, "ops": [
        {"rev": 2, "client": "alice", "id": "2", "op": [{"del": 0, "len": 1}]},
    ]}
    _, reply = join(port, "d", "carol", since=2)
    assert reply["ops"] == []


def test_since_out_of_range_is_bad_rev(server):
    port, _ = server
    w, _ = join(port, "d", "alice")
    w.send({"type": "join", "doc": "d", "client": "bob", "since": 3})
    assert w.recv()["code"] == "bad_rev"


def test_connection_drop_mid_line_does_not_affect_others(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    broken = Wire(port)
    broken.sock.sendall(b'{"type": "join", "doc": "d", "cli')
    broken.close()
    op(a, "d", 0, "1", [{"ins": 0, "text": "still fine"}])
    assert a.recv()["rev"] == 1


def test_disconnected_client_gets_no_more_broadcasts(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    b.close()
    op(a, "d", 0, "1", [{"ins": 0, "text": "x"}])
    msg = a.recv()
    if msg["type"] == "leave":  # bob's departure may be reported before the ack
        assert msg == {"type": "leave", "doc": "d", "client": "bob"}
        msg = a.recv()
    assert msg["rev"] == 1
    assert "op" not in msg and msg["type"] == "ack"


def test_state_survives_sigkill(tmp_path):
    data = tmp_path / "data"
    proc, port = start_server(data)
    try:
        a, _ = join(port, "d", "alice")
        op(a, "d", 0, "1", [{"ins": 0, "text": "héllo"}])
        assert a.recv()["rev"] == 1
        op(a, "d", 1, "2", [{"del": 1, "len": 1}])
        assert a.recv()["rev"] == 2
        other, _ = join(port, "e", "bob")
        op(other, "e", 0, "9", [{"ins": 0, "text": "z"}])
        assert other.recv()["rev"] == 1
    finally:
        stop_server(proc, signal.SIGKILL)

    proc, port = start_server(data)
    try:
        _, reply = join(port, "d", "carol")
        assert reply == {"type": "joined", "doc": "d", "rev": 2, "text": "hllo"}
        _, reply = join(port, "e", "carol", since=0)
        assert reply["rev"] == 1 and reply["ops"][0]["id"] == "9"
        # Exactly-once still holds after the restart.
        w, _ = join(port, "d", "alice")
        op(w, "d", 0, "1", [{"ins": 0, "text": "héllo"}])
        assert w.recv() == {"type": "ack", "doc": "d", "id": "1", "rev": 1}
    finally:
        stop_server(proc, signal.SIGKILL)


def test_long_history_across_snapshots(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    for i in range(150):
        op(a, "d", i, f"id{i}", [{"ins": 0, "text": "x"}])
        assert a.recv()["rev"] == i + 1
    op(a, "d", 100, "late", [{"ins": 0, "text": "y"}])
    assert a.recv()["rev"] == 151
    _, reply = join(port, "d", "bob")
    # "y" was written at revision 100; the 50 later inserts at 0 stay to its left.
    assert reply["text"] == "x" * 50 + "y" + "x" * 100


def set_cursor(w, doc, rev, pos):
    w.send({"type": "cursor", "doc": doc, "rev": rev, "pos": pos})


def query_cursors(w, doc):
    w.send({"type": "cursors", "doc": doc})
    reply = w.recv()
    assert reply["type"] == "cursors"
    return reply


def test_cursor_is_broadcast_to_others_only(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    set_cursor(a, "d", 0, 0)
    assert b.recv() == {"type": "cursor", "doc": "d", "client": "alice", "rev": 0, "pos": 0}
    # The sender gets no reply on success: the next message it sees is the ack it waits for.
    op(a, "d", 0, "1", [{"ins": 0, "text": "x"}])
    assert a.recv()["type"] == "ack"


def test_own_typing_pushes_cursor_and_others_typing_at_it_does_not(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    op(a, "d", 0, "1", [{"ins": 0, "text": "abc"}])
    a.recv()
    b.recv()
    set_cursor(a, "d", 1, 3)
    op(a, "d", 1, "2", [{"ins": 3, "text": "!"}])  # own typing at the cursor
    assert a.recv()["rev"] == 2
    assert b.recv()["type"] == "cursor"
    assert b.recv()["type"] == "op"
    op(b, "d", 2, "9", [{"ins": 4, "text": "Z"}])  # someone else typing exactly at the cursor
    assert b.recv()["rev"] == 3
    assert a.recv()["type"] == "op"
    assert query_cursors(a, "d")["cursors"] == {"alice": 4}


def test_cursor_follows_remote_edits_and_is_reported_at_current_revision(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    op(a, "d", 0, "1", [{"ins": 0, "text": "abcdef"}])
    a.recv()
    b.recv()
    set_cursor(a, "d", 1, 4)
    assert b.recv()["type"] == "cursor"
    op(b, "d", 1, "2", [{"del": 2, "len": 3}])  # cursor at 4 becomes 2
    assert b.recv()["type"] == "ack"
    assert a.recv()["type"] == "op"
    reply = query_cursors(b, "d")
    assert reply == {"type": "cursors", "doc": "d", "rev": 2, "cursors": {"alice": 2}}


def test_cursor_inside_deleted_range_moves_to_start(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    op(a, "d", 0, "1", [{"ins": 0, "text": "abcdef"}])
    a.recv()
    b.recv()
    set_cursor(a, "d", 1, 5)
    b.recv()
    op(b, "d", 1, "2", [{"del": 1, "len": 4}])  # deletes [1, 5)
    assert b.recv()["type"] == "ack"
    assert a.recv()["type"] == "op"
    assert query_cursors(b, "d")["cursors"] == {"alice": 1}


def test_cursor_errors(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    set_cursor(a, "d", 0, 1)
    assert a.recv()["code"] == "out_of_range"
    set_cursor(a, "d", 5, 0)
    assert a.recv()["code"] == "bad_rev"
    set_cursor(a, "d", 0, "3")
    assert a.recv()["code"] == "bad_request"
    set_cursor(a, "other", 0, 0)
    assert a.recv()["code"] == "not_joined"
    a.send({"type": "cursors", "doc": "other"})
    assert a.recv()["code"] == "not_joined"


def test_cursor_is_removed_when_last_connection_of_name_leaves(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    a2, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    set_cursor(a, "d", 0, 0)
    assert b.recv()["type"] == "cursor"
    a.close()
    assert query_cursors(b, "d")["cursors"] == {"alice": 0}
    a2.close()
    assert b.recv() == {"type": "leave", "doc": "d", "client": "alice"}
    assert query_cursors(b, "d")["cursors"] == {}


def test_leave_is_sent_even_without_a_cursor(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    b, _ = join(port, "d", "bob")
    a.close()
    assert b.recv() == {"type": "leave", "doc": "d", "client": "alice"}


def test_cursors_are_not_persisted(tmp_path):
    data = tmp_path / "data"
    proc, port = start_server(data)
    try:
        a, _ = join(port, "d", "alice")
        op(a, "d", 0, "1", [{"ins": 0, "text": "x"}])
        a.recv()
        set_cursor(a, "d", 1, 1)
        assert query_cursors(a, "d")["cursors"] == {"alice": 1}
    finally:
        stop_server(proc, signal.SIGKILL)
    proc, port = start_server(data)
    try:
        w, _ = join(port, "d", "bob")
        assert query_cursors(w, "d")["cursors"] == {}
    finally:
        stop_server(proc, signal.SIGKILL)


def snapshot(port, doc, rev):
    w = Wire(port)
    w.send({"type": "snapshot", "doc": doc, "rev": rev})
    reply = w.recv()
    w.close()
    return reply


def test_snapshot_of_every_revision(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    texts = [""]
    edits = [
        [{"ins": 0, "text": "hello"}],
        [{"ins": 5, "text": " world"}],
        [{"del": 0, "len": 1}],
        [],
        [{"ins": 0, "text": "J"}, {"del": 1, "len": 2}],
    ]
    for i, edit in enumerate(edits):
        op(a, "d", i, str(i), edit)
        assert a.recv()["rev"] == i + 1
        from livedoc_client import apply_op
        texts.append(apply_op(texts[-1], edit))
    for rev, text in enumerate(texts):
        assert snapshot(port, "d", rev) == {"type": "snapshot", "doc": "d", "rev": rev, "text": text}


def test_snapshot_errors(server):
    port, _ = server
    a, _ = join(port, "d", "alice")
    assert snapshot(port, "missing", 0)["code"] == "not_found"
    assert snapshot(port, "d", 0)["text"] == ""
    assert snapshot(port, "d", 1)["code"] == "bad_rev"
    assert snapshot(port, "d", -1)["code"] == "bad_rev"
    w = Wire(port)
    w.send({"type": "snapshot", "doc": "d", "rev": "0"})
    assert w.recv()["code"] == "bad_request"
    w.send({"type": "snapshot", "doc": "", "rev": 0})
    assert w.recv()["code"] == "bad_request"


def test_snapshot_does_not_need_a_join_and_survives_restart(tmp_path):
    data = tmp_path / "data"
    proc, port = start_server(data)
    try:
        a, _ = join(port, "empty", "alice")  # joined but never edited
        a2, _ = join(port, "d", "alice")
        op(a2, "d", 0, "1", [{"ins": 0, "text": "kept"}])
        assert a2.recv()["rev"] == 1
    finally:
        stop_server(proc, signal.SIGKILL)
    proc, port = start_server(data)
    try:
        assert snapshot(port, "d", 1) == {"type": "snapshot", "doc": "d", "rev": 1, "text": "kept"}
        assert snapshot(port, "empty", 0) == {"type": "snapshot", "doc": "empty", "rev": 0, "text": ""}
    finally:
        stop_server(proc, signal.SIGKILL)


def test_snapshot_of_long_history_is_fast_and_correct(tmp_path):
    import time
    data = tmp_path / "data"
    proc, port = start_server(data)
    total = 5000
    try:
        a, _ = join(port, "long", "alice")
        # Pipeline all the operations: each one is based on the revision before it.
        for i in range(total):
            op(a, "long", i, f"id{i}", [{"ins": 0, "text": "ab"}])
        for i in range(total):
            assert a.recv()["rev"] == i + 1
    finally:
        stop_server(proc, signal.SIGKILL)
    proc, port = start_server(data)
    try:
        expected = "ab" * total
        for rev in [0, 1, 63, 64, 1234, 4999, 5000]:
            start = time.monotonic()
            reply = snapshot(port, "long", rev)
            elapsed = time.monotonic() - start
            assert reply["text"] == "ab" * rev
            assert elapsed < 1.0, f"snapshot at revision {rev} took {elapsed:.2f}s"
        assert snapshot(port, "long", total)["text"] == expected
    finally:
        stop_server(proc, signal.SIGKILL)
