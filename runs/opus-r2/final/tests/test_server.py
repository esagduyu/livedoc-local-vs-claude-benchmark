import json
import socket
import time

import pytest

from conftest import Raw


def op_msg(doc, rev, op_id, op):
    return {"type": "op", "doc": doc, "rev": rev, "id": op_id, "op": op}


def test_join_creates_empty_document(raw):
    c = raw()
    assert c.join("doc", "alice") == {"type": "joined", "doc": "doc", "rev": 0, "text": ""}


def test_op_ack_and_broadcast(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    assert a.request(op_msg("d", 0, "1", [{"ins": 0, "text": "héllo"}])) == \
        {"type": "ack", "doc": "d", "id": "1", "rev": 1}
    assert b.recv() == {"type": "op", "doc": "d", "rev": 1, "client": "alice", "id": "1",
                        "op": [{"ins": 0, "text": "héllo"}]}
    a.assert_silent()
    assert raw().join("d", "carol")["text"] == "héllo"


def test_stale_op_is_transformed(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    a.request(op_msg("d", 0, "a1", [{"ins": 0, "text": "abcdef"}]))
    b.recv()
    a.request(op_msg("d", 1, "a2", [{"ins": 3, "text": "XY"}]))       # abcXYdef
    b.recv()
    # bob still thinks rev is 1 and deletes "bcde"
    ack = b.request(op_msg("d", 1, "b1", [{"del": 1, "len": 4}]))
    assert ack == {"type": "ack", "doc": "d", "id": "b1", "rev": 3}
    bc = a.recv()
    assert bc["op"] == [{"del": 1, "len": 2}, {"del": 3, "len": 2}]
    assert raw().join("d", "z")["text"] == "aXYf"


def test_insert_tie_earlier_stays_left(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    a.request(op_msg("d", 0, "1", [{"ins": 0, "text": "A"}]))
    b.recv()
    b.request(op_msg("d", 0, "2", [{"ins": 0, "text": "B"}]))
    assert a.recv()["op"] == [{"ins": 1, "text": "B"}]
    assert raw().join("d", "z")["text"] == "AB"


def test_vanished_op_is_committed_as_empty(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    a.request(op_msg("d", 0, "1", [{"ins": 0, "text": "abc"}]))
    b.recv()
    a.request(op_msg("d", 1, "2", [{"del": 0, "len": 3}]))
    b.recv()
    assert b.request(op_msg("d", 1, "3", [{"del": 1, "len": 1}]))["rev"] == 3
    assert a.recv() == {"type": "op", "doc": "d", "rev": 3, "client": "bob", "id": "3", "op": []}
    assert a.request(op_msg("d", 3, "4", []))["rev"] == 4


def test_join_since_returns_history(raw):
    a = raw()
    a.join("d", "alice")
    a.request(op_msg("d", 0, "1", [{"ins": 0, "text": "ab"}]))
    a.request(op_msg("d", 0, "2", [{"ins": 0, "text": "X"}]))
    reply = raw().join("d", "bob", since=1)
    assert reply == {"type": "joined", "doc": "d", "rev": 2, "ops": [
        {"rev": 2, "client": "alice", "id": "2", "op": [{"ins": 2, "text": "X"}]}]}
    assert raw().join("d", "bob", since=2)["ops"] == []
    assert len(raw().join("d", "bob", since=0)["ops"]) == 2


def test_exactly_once(raw):
    a, b = raw(), raw()
    a.join("d", "alice")
    b.join("d", "bob")
    a.request(op_msg("d", 0, "x", [{"ins": 0, "text": "hi"}]))
    b.recv()
    a.request(op_msg("d", 1, "y", [{"ins": 0, "text": "!"}]))
    b.recv()
    assert a.request(op_msg("d", 0, "x", [{"ins": 0, "text": "hi"}]))["rev"] == 1
    b.assert_silent()
    # Same name on a new connection: still a duplicate. Different name: not.
    c = raw()
    c.join("d", "alice")
    assert c.request(op_msg("d", 2, "x", [{"ins": 0, "text": "zz"}]))["rev"] == 1
    d = raw()
    d.join("d", "dave")
    assert d.request(op_msg("d", 2, "x", [{"ins": 0, "text": "zz"}]))["rev"] == 3


def test_persistence_after_sigkill(server, raw):
    a = raw()
    a.join("doc/one", "alice")
    a.request(op_msg("doc/one", 0, "1", [{"ins": 0, "text": "persist 😀"}]))
    a.request(op_msg("doc/one", 1, "2", [{"del": 0, "len": 8}]))
    b = raw()
    b.join("other", "bob")
    b.request(op_msg("other", 0, "1", [{"ins": 0, "text": "x"}]))
    server.restart()

    c = Raw(server.port)
    try:
        assert c.join("doc/one", "carol") == {"type": "joined", "doc": "doc/one", "rev": 2,
                                              "text": "😀"}
        assert c.join("doc/one", "carol", since=1)["ops"] == [
            {"rev": 2, "client": "alice", "id": "2", "op": [{"del": 0, "len": 8}]}]
        assert c.join("other", "carol")["text"] == "x"
        # exactly-once survives the restart
        c.join("doc/one", "alice")
        assert c.request(op_msg("doc/one", 0, "1", [{"ins": 0, "text": "persist 😀"}]))["rev"] == 1
        assert c.request(op_msg("doc/one", 2, "3", [{"ins": 1, "text": "!"}]))["rev"] == 3
    finally:
        c.close()
    server.restart()
    c = Raw(server.port)
    try:
        assert c.join("doc/one", "z")["text"] == "😀!"
    finally:
        c.close()


def test_torn_log_tail_is_ignored(server, raw, tmp_path):
    a = raw()
    a.join("d", "alice")
    a.request(op_msg("d", 0, "1", [{"ins": 0, "text": "ok"}]))
    server.kill()
    (log,) = (tmp_path / "data").iterdir()
    with open(log, "ab") as f:
        f.write(b'{"rev": 2, "client": "alice", "id": "2", "op": [{"ins"')
    server.start()
    c = Raw(server.port)
    try:
        assert c.join("d", "alice") == {"type": "joined", "doc": "d", "rev": 1, "text": "ok"}
        assert c.request(op_msg("d", 1, "2", [{"ins": 2, "text": "!"}]))["rev"] == 2
    finally:
        c.close()
    server.restart()
    c = Raw(server.port)
    try:
        assert c.join("d", "z")["text"] == "ok!"
    finally:
        c.close()


@pytest.mark.parametrize("line, code", [
    (b"not json", "bad_json"),
    (b"[1, 2]", "bad_json"),
    (b"\xff\xfe", "bad_json"),
    (b'{"type": "nope"}', "bad_request"),
    (b'{"doc": "d"}', "bad_request"),
    (b'{"type": "join", "doc": "", "client": "a"}', "bad_request"),
    (b'{"type": "join", "doc": "' + b"x" * 101 + b'", "client": "a"}', "bad_request"),
    (b'{"type": "join", "doc": "d", "client": ""}', "bad_request"),
    (b'{"type": "join", "doc": "d", "client": 5}', "bad_request"),
    (b'{"type": "join", "doc": "d", "client": "a", "since": "0"}', "bad_request"),
    (b'{"type": "join", "doc": "d", "client": "a", "since": 1}', "bad_rev"),
    (b'{"type": "join", "doc": "d", "client": "a", "since": -1}', "bad_rev"),
])
def test_request_errors(raw, line, code):
    c = raw()
    c.send_raw(line + b"\n")
    reply = c.recv()
    assert reply["type"] == "error" and reply["code"] == code
    assert "id" not in reply
    assert c.join("d", "a")["rev"] == 0  # connection still works


@pytest.mark.parametrize("fields, code", [
    ({"op": [{"ins": 0, "text": "x"}], "rev": "0"}, "bad_request"),
    ({"op": [{"ins": 0, "text": "x"}], "rev": True}, "bad_request"),
    ({"op": [{"ins": 0, "text": "x"}], "rev": ...}, "bad_request"),
    ({"op": [{"ins": 0, "text": "x"}], "doc": ...}, "bad_request"),
    ({"op": ...}, "bad_request"),
    ({"op": [{"ins": 0, "text": "x"}], "id": 7}, "bad_request"),
    ({"op": {"ins": 0, "text": "x"}}, "bad_request"),
    ({"op": [{"ins": 0}]}, "bad_request"),
    ({"op": [{"ins": "0", "text": "x"}]}, "bad_request"),
    ({"op": [{"del": 0, "len": 1.5}]}, "bad_request"),
    ({"op": [{"ins": 0, "text": "x", "del": 0}]}, "bad_request"),
    ({"op": ["x"]}, "bad_request"),
    ({"op": [{"ins": 0, "text": "x"}], "doc": "other"}, "not_joined"),
    ({"op": [{"ins": 0, "text": "x"}], "rev": 2}, "bad_rev"),
    ({"op": [{"ins": 0, "text": "x"}], "rev": -1}, "bad_rev"),
    ({"op": [{"ins": 4, "text": "x"}]}, "out_of_range"),
    ({"op": [{"ins": -1, "text": "x"}]}, "out_of_range"),
    ({"op": [{"ins": 0, "text": ""}]}, "out_of_range"),
    ({"op": [{"del": 1, "len": 3}]}, "out_of_range"),
    ({"op": [{"del": 0, "len": 0}]}, "out_of_range"),
    ({"op": [{"del": -1, "len": 1}]}, "out_of_range"),
    ({"op": [{"ins": 3, "text": "x"}, {"del": 0, "len": 5}]}, "out_of_range"),
    # valid against the current text (rev 1) but not against rev 0
    ({"op": [{"del": 0, "len": 1}], "rev": 0}, "out_of_range"),
])
def test_op_errors(raw, fields, code):
    c, watcher = raw(), raw()
    c.join("d", "a")
    watcher.join("d", "w")
    c.request(op_msg("d", 0, "init", [{"ins": 0, "text": "abc"}]))
    watcher.recv()
    msg = {"type": "op", "doc": "d", "rev": 1, "id": "bad", **fields}
    msg = {k: v for k, v in msg.items() if v is not ...}  # ... marks a missing field
    reply = c.request(msg)
    assert reply["type"] == "error" and reply["code"] == code, reply
    assert reply["id"] == msg["id"]
    watcher.assert_silent()
    assert c.request(op_msg("d", 1, "good", [{"ins": 3, "text": "!"}]))["rev"] == 2
    assert watcher.recv()["id"] == "good"
    assert watcher.join("d", "w")["text"] == "abc!"


def test_bad_op_with_same_id_can_be_retried(raw):
    c = raw()
    c.join("d", "a")
    assert c.request(op_msg("d", 0, "x", [{"del": 0, "len": 1}]))["code"] == "out_of_range"
    assert c.request(op_msg("d", 0, "x", [{"ins": 0, "text": "q"}]))["rev"] == 1


def test_partial_line_disconnect_does_not_affect_others(raw, server):
    a = raw()
    a.join("d", "alice")
    bad = socket.create_connection(("127.0.0.1", server.port))
    bad.sendall(b'{"type": "join", "doc": "d", "cli')
    bad.close()
    bad = socket.create_connection(("127.0.0.1", server.port))
    bad.sendall(b'{"type": "join", "doc": "d", "client": "evil"}\n{"type": "op", "doc"')
    time.sleep(0.1)
    bad.close()
    assert a.recv() == {"type": "leave", "doc": "d", "client": "evil"}
    assert a.request(op_msg("d", 0, "1", [{"ins": 0, "text": "fine"}]))["rev"] == 1
    assert raw().join("d", "b")["text"] == "fine"


def test_messages_split_across_packets(raw):
    c = raw()
    data = (json.dumps({"type": "join", "doc": "d", "client": "a"}) + "\n" +
            json.dumps(op_msg("d", 0, "1", [{"ins": 0, "text": "ü"}])) + "\n").encode()
    for i in range(len(data)):
        c.send_raw(data[i:i + 1])
    assert c.recv()["type"] == "joined"
    assert c.recv() == {"type": "ack", "doc": "d", "id": "1", "rev": 1}


def test_one_connection_many_documents(raw):
    a, b = raw(), raw()
    a.join("one", "alice")
    a.join("two", "alice")
    b.join("two", "bob")
    a.request(op_msg("one", 0, "1", [{"ins": 0, "text": "1"}]))
    b.assert_silent()
    a.request(op_msg("two", 0, "1", [{"ins": 0, "text": "2"}]))
    assert b.recv()["doc"] == "two"


def test_broadcast_order_and_ack_after_prior_broadcasts(raw):
    clients = [raw() for _ in range(3)]
    for i, c in enumerate(clients):
        c.join("d", f"c{i}")
    # Everyone fires ops based on rev 0 without waiting.
    for n in range(20):
        for i, c in enumerate(clients):
            c.send(op_msg("d", 0, f"{i}-{n}", [{"ins": 0, "text": str(i)}]))
    for i, c in enumerate(clients):
        expected_rev = 1
        acks = 0
        while acks < 20:
            m = c.recv()
            assert m["rev"] == expected_rev, m  # every revision, in order, no gaps
            expected_rev += 1
            if m["type"] == "ack":
                assert m["id"] == f"{i}-{acks}"
                acks += 1
            else:
                assert m["client"] != f"c{i}"
    final = raw().join("d", "z")
    assert final["rev"] == 60 and len(final["text"]) == 60


def test_non_reading_client_does_not_block_others(raw, server):
    lazy = socket.create_connection(("127.0.0.1", server.port))
    lazy.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    lazy.sendall(b'{"type": "join", "doc": "d", "client": "lazy"}\n')
    a = raw()
    a.join("d", "alice")
    big = "x" * 100_000
    start = time.monotonic()
    for i in range(30):
        assert a.request(op_msg("d", i, str(i), [{"ins": 0, "text": big}]))["rev"] == i + 1
    assert time.monotonic() - start < 10
    lazy.close()


def test_every_acked_op_survives_sigkill_under_load(server):
    c = Raw(server.port)
    c.join("load", "alice")
    for i in range(2000):
        c.send(op_msg("load", 0, f"op{i}", [{"ins": 0, "text": "x"}]))
    acked = [c.recv() for _ in range(300)]  # kill while the rest are still in progress
    server.restart()
    c.close()
    c = Raw(server.port)
    try:
        reply = c.join("load", "bob", since=0)
        assert reply["rev"] >= 300
        assert [e["id"] for e in reply["ops"]] == [f"op{i}" for i in range(reply["rev"])]
        assert all(a["rev"] == int(a["id"][2:]) + 1 for a in acked)
        c.join("load", "alice")
        assert c.request(op_msg("load", 0, "op5", [{"ins": 0, "text": "x"}]))["rev"] == 6
    finally:
        c.close()
