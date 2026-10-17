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
    assert a.recv()["rev"] == 1


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
