import json
import os
import socket

import pytest


def join(w, doc, client, since=None):
    msg = {"type": "join", "doc": doc, "client": client}
    if since is not None:
        msg["since"] = since
    w.send(msg)
    return w.recv()


def send_op(w, doc, rev, op_id, op):
    w.send({"type": "op", "doc": doc, "rev": rev, "id": op_id, "op": op})


def text_of(connect, port, doc):
    w = connect(port)
    reply = join(w, doc, "reader")
    assert reply["type"] == "joined"
    return reply["text"]


def test_join_new_document_is_empty_at_revision_zero(server, connect):
    w = connect(server.port)
    assert join(w, "notes", "alice") == {"type": "joined", "doc": "notes", "rev": 0, "text": ""}


def test_op_is_acked_and_broadcast_to_others(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    send_op(a, "d", 0, "op1", [{"ins": 0, "text": "hi"}])
    assert a.recv() == {"type": "ack", "doc": "d", "id": "op1", "rev": 1}
    assert b.recv() == {"type": "op", "doc": "d", "rev": 1, "client": "alice", "id": "op1",
                        "op": [{"ins": 0, "text": "hi"}]}


def test_stale_operation_is_transformed_against_committed_ones(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    send_op(a, "d", 0, "a1", [{"ins": 0, "text": "a"}])
    assert a.recv()["rev"] == 1
    assert b.recv()["rev"] == 1
    # Bob wrote against revision 0, so his insert at 0 loses the tie and moves to 1.
    send_op(b, "d", 0, "b1", [{"ins": 0, "text": "b"}])
    assert b.recv() == {"type": "ack", "doc": "d", "id": "b1", "rev": 2}
    assert a.recv() == {"type": "op", "doc": "d", "rev": 2, "client": "bob", "id": "b1",
                        "op": [{"ins": 1, "text": "b"}]}
    assert text_of(connect, server.port, "d") == "ab"


def test_empty_operation_is_committed_and_broadcast(server, connect):
    a, b = connect(server.port), connect(server.port)
    join(a, "d", "alice")
    join(b, "d", "bob")
    send_op(a, "d", 0, "e", [])
    assert a.recv() == {"type": "ack", "doc": "d", "id": "e", "rev": 1}
    assert b.recv() == {"type": "op", "doc": "d", "rev": 1, "client": "alice", "id": "e", "op": []}


def test_multi_edit_operation_applies_edits_in_order(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 0, "1", [{"ins": 0, "text": "abcdef"}])
    w.recv()
    send_op(w, "d", 1, "2", [{"del": 1, "len": 2}, {"ins": 1, "text": "XY"}, {"del": 0, "len": 1}])
    assert w.recv()["rev"] == 2
    assert text_of(connect, server.port, "d") == "XYdef"  # "abcdef" -> "adef" -> "aXYdef" -> "XYdef"


def test_joined_with_since_returns_operations_after_that_revision(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 0, "1", [{"ins": 0, "text": "a"}])
    w.recv()
    send_op(w, "d", 1, "2", [{"ins": 1, "text": "b"}])
    w.recv()
    r = connect(server.port)
    reply = join(r, "d", "carol", since=1)
    assert reply == {"type": "joined", "doc": "d", "rev": 2, "ops": [
        {"rev": 2, "client": "alice", "id": "2", "op": [{"ins": 1, "text": "b"}]}]}
    reply = join(connect(server.port), "d", "dave", since=0)
    assert [o["rev"] for o in reply["ops"]] == [1, 2]


def test_join_with_since_equal_to_revision_has_no_ops(server, connect):
    w = connect(server.port)
    reply = join(w, "d", "alice", since=0)
    assert reply == {"type": "joined", "doc": "d", "rev": 0, "ops": []}


def test_documents_are_independent(server, connect):
    w = connect(server.port)
    join(w, "one", "alice")
    join(w, "two", "alice")
    send_op(w, "one", 0, "1", [{"ins": 0, "text": "x"}])
    assert w.recv()["rev"] == 1
    assert text_of(connect, server.port, "two") == ""


def test_operation_on_document_not_joined_is_rejected_and_connection_stays_open(server, connect):
    w = connect(server.port)
    join(w, "one", "alice")
    send_op(w, "two", 0, "x", [{"ins": 0, "text": "a"}])
    reply = w.recv()
    assert reply["code"] == "not_joined"
    assert reply["id"] == "x"
    send_op(w, "one", 0, "y", [{"ins": 0, "text": "a"}])
    assert w.recv()["type"] == "ack"


@pytest.mark.parametrize("line", [b"not json\n", b"[1, 2]\n", b"\n", b"\xff\xfe\n", b'"str"\n'])
def test_bad_json_is_reported_and_connection_stays_open(server, connect, line):
    w = connect(server.port)
    w.send_raw(line)
    reply = w.recv()
    assert reply["type"] == "error"
    assert reply["code"] == "bad_json"
    join(w, "d", "alice")  # still works


@pytest.mark.parametrize("msg", [
    {"doc": "d"},
    {"type": "frobnicate"},
    {"type": "join", "doc": "d"},
    {"type": "join", "doc": "", "client": "a"},
    {"type": "join", "doc": "x" * 101, "client": "a"},
    {"type": "join", "doc": 5, "client": "a"},
    {"type": "join", "doc": "d", "client": ""},
    {"type": "join", "doc": "d", "client": "a", "since": "0"},
])
def test_malformed_requests_are_bad_request(server, connect, msg):
    w = connect(server.port)
    w.send(msg)
    reply = w.recv()
    assert reply["type"] == "error"
    assert reply["code"] == "bad_request"


@pytest.mark.parametrize("op", [
    "ins",
    [{"ins": "0", "text": "a"}],
    [{"ins": 0}],
    [{"del": 0}],
    [{"del": 0, "len": "1"}],
    [{"foo": 1}],
    ["x"],
])
def test_malformed_operation_is_bad_request(server, connect, op):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 0, "x", op)
    reply = w.recv()
    assert reply["code"] == "bad_request"
    assert reply["id"] == "x"


def test_bad_rev_for_operation_and_join(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 5, "x", [{"ins": 0, "text": "a"}])
    assert w.recv()["code"] == "bad_rev"
    send_op(w, "d", -1, "x", [{"ins": 0, "text": "a"}])
    assert w.recv()["code"] == "bad_rev"
    assert join(connect(server.port), "d", "bob", since=3)["code"] == "bad_rev"
    assert join(connect(server.port), "d", "bob", since=-1)["code"] == "bad_rev"


@pytest.mark.parametrize("op", [
    [{"ins": 1, "text": "a"}],  # past the end of ""
    [{"ins": 0, "text": ""}],  # empty insert
    [{"del": 0, "len": 0}],  # zero length
    [{"del": 0, "len": 1}],  # past the end
    [{"ins": -1, "text": "a"}],
    [{"ins": 0, "text": "ab"}, {"del": 0, "len": 3}],  # second edit runs past the end
])
def test_out_of_range_operation_is_rejected(server, connect, op):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 0, "x", op)
    reply = w.recv()
    assert reply["code"] == "out_of_range"
    assert reply["id"] == "x"
    # Nothing was committed.
    assert join(connect(server.port), "d", "bob")["rev"] == 0


def test_exactly_once_resend_returns_original_revision_without_applying(server, connect):
    a = connect(server.port)
    join(a, "d", "alice")
    send_op(a, "d", 0, "dup", [{"ins": 0, "text": "x"}])
    assert a.recv()["rev"] == 1
    send_op(a, "d", 0, "dup", [{"ins": 0, "text": "x"}])
    assert a.recv() == {"type": "ack", "doc": "d", "id": "dup", "rev": 1}
    assert text_of(connect, server.port, "d") == "x"


def test_exactly_once_applies_per_client_name_and_document(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    join(w, "e", "alice")
    send_op(w, "d", 0, "same", [{"ins": 0, "text": "x"}])
    assert w.recv()["rev"] == 1
    send_op(w, "e", 0, "same", [{"ins": 0, "text": "y"}])
    assert w.recv()["rev"] == 1
    other = connect(server.port)
    join(other, "d", "bob")
    send_op(other, "d", 1, "same", [{"ins": 0, "text": "z"}])
    assert other.recv()["rev"] == 2


def test_persistence_survives_sigkill(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 0, "1", [{"ins": 0, "text": "hello"}])
    assert w.recv()["rev"] == 1
    send_op(w, "d", 1, "2", [{"del": 1, "len": 3}])
    assert w.recv()["rev"] == 2
    send_op(w, "d", 2, "3", [{"ins": 2, "text": "!"}])
    assert w.recv()["rev"] == 3

    server.restart()

    reply = join(connect(server.port), "d", "bob")
    assert reply == {"type": "joined", "doc": "d", "rev": 3, "text": "ho!"}
    history = join(connect(server.port), "d", "carol", since=0)
    assert [o["id"] for o in history["ops"]] == ["1", "2", "3"]


def test_exactly_once_survives_sigkill(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 0, "once", [{"ins": 0, "text": "a"}])
    assert w.recv()["rev"] == 1
    send_op(w, "d", 1, "two", [{"ins": 1, "text": "b"}])
    assert w.recv()["rev"] == 2

    server.restart()

    w2 = connect(server.port)
    join(w2, "d", "alice")
    send_op(w2, "d", 0, "once", [{"ins": 0, "text": "a"}])
    assert w2.recv() == {"type": "ack", "doc": "d", "id": "once", "rev": 1}
    send_op(w2, "d", 2, "two", [{"ins": 2, "text": "c"}])
    assert w2.recv() == {"type": "ack", "doc": "d", "id": "two", "rev": 2}
    assert text_of(connect, server.port, "d") == "ab"


def test_torn_log_record_from_a_crash_is_discarded(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 0, "1", [{"ins": 0, "text": "ok"}])
    assert w.recv()["rev"] == 1

    server.kill()
    with open(os.path.join(server.data_dir, "oplog.jsonl"), "ab") as f:
        f.write(b'{"doc": "d", "rev": 2, "client": "alice", "id": "2", "op": [{"ins"')

    server.start()
    assert text_of(connect, server.port, "d") == "ok"
    w2 = connect(server.port)
    join(w2, "d", "alice")
    send_op(w2, "d", 1, "3", [{"ins": 2, "text": "!"}])
    assert w2.recv()["rev"] == 2

    server.restart()
    assert text_of(connect, server.port, "d") == "ok!"


def test_client_disconnecting_mid_line_does_not_affect_others(server, connect):
    a = connect(server.port)
    join(a, "d", "alice")
    rude = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    rude.sendall(b'{"type": "op", "doc": "d", "rev": 0, "id": "x", "op": [{"ins": 0, "te')
    rude.close()
    send_op(a, "d", 0, "1", [{"ins": 0, "text": "fine"}])
    assert a.recv()["type"] == "ack"
    assert text_of(connect, server.port, "d") == "fine"


def test_unterminated_line_is_not_processed(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    w.send_raw(b'{"type": "op", "doc": "d", "rev": 0, "id": "x", "op": [{"ins": 0, "text": "a"}]}')
    w.send_raw(b"\n")
    assert w.recv()["type"] == "ack"


def test_broadcasts_arrive_in_revision_order(server, connect):
    writer = connect(server.port)
    join(writer, "d", "alice")
    watcher = connect(server.port)
    join(watcher, "d", "bob")
    for i in range(20):
        send_op(writer, "d", i, f"op{i}", [{"ins": i, "text": str(i % 10)}])
        assert writer.recv()["rev"] == i + 1
    revs = [watcher.recv()["rev"] for _ in range(20)]
    assert revs == list(range(1, 21))


def test_server_speaks_only_single_lines(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    send_op(w, "d", 0, "unicode", [{"ins": 0, "text": "héllo 😀"}])
    raw = w.file.readline()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert json.loads(raw)["type"] == "ack"
    assert text_of(connect, server.port, "d") == "héllo 😀"
