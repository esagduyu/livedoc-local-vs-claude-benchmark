import os
import time
import uuid

from helpers import Wire
from livedoc_ot import apply_op


def ins(p, t):
    return {"ins": p, "text": t}


def dele(p, n):
    return {"del": p, "len": n}


def op_msg(doc, rev, op, op_id=None):
    return {"type": "op", "doc": doc, "rev": rev, "id": op_id or str(uuid.uuid4()), "op": op}


def test_join_new_document_is_empty_at_rev_zero(wire):
    c = wire()
    reply = c.join("d1", "alice")
    assert reply == {"type": "joined", "doc": "d1", "rev": 0, "text": ""}


def test_op_is_acked_and_broadcast_to_others_only(wire):
    a, b = wire(), wire()
    a.join("d", "alice")
    b.join("d", "bob")
    a.send(op_msg("d", 0, [ins(0, "hi")], "op-1"))
    assert a.recv() == {"type": "ack", "doc": "d", "id": "op-1", "rev": 1}
    assert b.recv() == {"type": "op", "doc": "d", "rev": 1, "client": "alice",
                        "id": "op-1", "op": [ins(0, "hi")]}
    # alice must not have received her own broadcast before her next message
    a.send({"type": "join", "doc": "d", "client": "alice", "since": 1})
    assert a.recv()["type"] == "joined"


def test_concurrent_ops_are_transformed_by_server(wire):
    a, b = wire(), wire()
    a.join("d", "alice")
    b.join("d", "bob")
    a.send(op_msg("d", 0, [ins(0, "AAA")], "a1"))
    a.recv()
    b.recv()
    # bob wrote against revision 0 without seeing alice's insert
    b.send(op_msg("d", 0, [ins(0, "B")], "b1"))
    ack = b.recv()
    assert ack == {"type": "ack", "doc": "d", "id": "b1", "rev": 2}
    broadcast = a.recv()
    assert broadcast["op"] == [ins(3, "B")]
    assert broadcast["client"] == "bob"
    assert broadcast["rev"] == 2


def test_join_since_returns_history_and_text_is_consistent(wire):
    a = wire()
    a.join("d", "alice")
    a.send(op_msg("d", 0, [ins(0, "abc")], "1"))
    a.recv()
    a.send(op_msg("d", 1, [dele(1, 1)], "2"))
    a.recv()
    c = wire()
    reply = c.join("d", "carol", since=1)
    assert reply == {"type": "joined", "doc": "d", "rev": 2, "ops": [
        {"rev": 2, "client": "alice", "id": "2", "op": [dele(1, 1)]}]}
    full = wire().join("d", "dave", since=0)
    assert apply_op("", [e for entry in full["ops"] for e in entry["op"]]) == "ac"


def test_join_with_since_out_of_range_is_bad_rev(wire):
    c = wire()
    c.join("d", "alice")
    c.send({"type": "join", "doc": "d", "client": "alice", "since": 1})
    assert c.recv()["code"] == "bad_rev"


def test_errors_keep_connection_open(wire):
    c = wire()
    c.send_raw(b"this is not json\n")
    assert c.recv()["code"] == "bad_json"
    c.send_raw(b"[1, 2]\n")
    assert c.recv()["code"] == "bad_json"
    c.send({"type": "frobnicate", "id": "x"})
    assert c.recv() == {"type": "error", "code": "bad_request",
                        "message": "unknown message type 'frobnicate'", "id": "x"}
    c.send(op_msg("d", 0, []))  # not joined
    assert c.recv()["code"] == "not_joined"
    c.join("d", "alice")
    c.send({"type": "join", "doc": "", "client": "alice"})
    assert c.recv()["code"] == "bad_request"
    c.send({"type": "join", "doc": "x" * 101, "client": "alice"})
    assert c.recv()["code"] == "bad_request"
    c.send({"type": "op", "doc": "d", "rev": "0", "id": "1", "op": []})
    assert c.recv()["code"] == "bad_request"
    c.send({"type": "op", "doc": "d", "rev": 0, "id": "1", "op": [{"ins": 0}]})
    assert c.recv()["code"] == "bad_request"
    c.send(op_msg("d", 5, []))
    assert c.recv()["code"] == "bad_rev"
    c.send(op_msg("d", -1, []))
    assert c.recv()["code"] == "bad_rev"
    # the connection still works
    c.send(op_msg("d", 0, [ins(0, "ok")], "fine"))
    assert c.recv()["type"] == "ack"


def test_out_of_range_operations_are_rejected_and_not_committed(wire):
    c = wire()
    c.join("d", "alice")
    bad_ops = [
        [ins(1, "x")],          # past the end of ""
        [ins(0, "")],           # empty insert
        [dele(0, 1)],           # delete from ""
        [ins(0, "abc"), dele(2, 2)],  # second edit runs past the end
    ]
    for op in bad_ops:
        c.send(op_msg("d", 0, op, "bad"))
        reply = c.recv()
        assert reply["code"] == "out_of_range", op
        assert reply["id"] == "bad"
    c.send({"type": "join", "doc": "d", "client": "other", "since": 0})
    assert c.recv()["rev"] == 0


def test_zero_length_delete_and_empty_op_rules(wire):
    c = wire()
    c.join("d", "alice")
    c.send(op_msg("d", 0, [ins(0, "ab")], "1"))
    c.recv()
    c.send(op_msg("d", 1, [dele(0, 0)], "2"))
    assert c.recv()["code"] == "out_of_range"
    c.send(op_msg("d", 1, [], "3"))
    assert c.recv() == {"type": "ack", "doc": "d", "id": "3", "rev": 2}


def test_all_edits_vanishing_still_commits_empty_op(wire):
    a, b = wire(), wire()
    a.join("d", "alice")
    b.join("d", "bob")
    a.send(op_msg("d", 0, [ins(0, "xyz")], "1"))
    a.recv()
    b.recv()
    b.send(op_msg("d", 1, [dele(0, 3)], "b"))
    assert b.recv()["rev"] == 2
    a.recv()
    a.send(op_msg("d", 1, [dele(1, 1)], "a"))  # 'y' was already deleted by bob
    assert a.recv() == {"type": "ack", "doc": "d", "id": "a", "rev": 3}
    assert b.recv() == {"type": "op", "doc": "d", "rev": 3, "client": "alice",
                        "id": "a", "op": []}


def test_retransmission_is_acked_with_original_revision(wire):
    a, b = wire(), wire()
    a.join("d", "alice")
    b.join("d", "bob")
    a.send(op_msg("d", 0, [ins(0, "x")], "same"))
    assert a.recv()["rev"] == 1
    b.recv()
    a.send(op_msg("d", 0, [ins(0, "x")], "same"))
    assert a.recv() == {"type": "ack", "doc": "d", "id": "same", "rev": 1}
    # no second broadcast: the next real op is the next message bob gets
    a.send(op_msg("d", 1, [ins(1, "y")], "next"))
    assert a.recv()["rev"] == 2
    assert b.recv()["id"] == "next"


def test_persistence_survives_sigkill(server_process, tmp_path):
    data = tmp_path / "data"
    proc = server_process(data)
    c = Wire(proc.port)
    c.join("doc", "alice")
    c.send(op_msg("doc", 0, [ins(0, "hello")], "a1"))
    assert c.recv()["rev"] == 1
    c.send(op_msg("doc", 1, [ins(5, " world")], "a2"))
    assert c.recv()["rev"] == 2
    c.close()
    proc.kill()

    proc2 = server_process(data)
    c = Wire(proc2.port)
    assert c.join("doc", "alice") == {"type": "joined", "doc": "doc", "rev": 2,
                                      "text": "hello world"}
    # the exactly-once record survived too: alice's retransmission is not reapplied
    c.send(op_msg("doc", 0, [ins(0, "hello")], "a1"))
    assert c.recv() == {"type": "ack", "doc": "doc", "id": "a1", "rev": 1}
    c.send({"type": "join", "doc": "doc", "client": "alice", "since": 0})
    assert [e["rev"] for e in c.recv()["ops"]] == [1, 2]
    c.close()


def test_torn_final_record_is_dropped_on_restart(server_process, tmp_path):
    data = tmp_path / "data"
    proc = server_process(data)
    c = Wire(proc.port)
    c.join("doc", "alice")
    c.send(op_msg("doc", 0, [ins(0, "kept")], "1"))
    c.recv()
    c.close()
    proc.kill()
    with open(os.path.join(data, "livedoc.log"), "ab") as f:
        f.write(b'{"doc": "doc", "rev": 2, "cli')  # cut off mid-write, never acked

    proc2 = server_process(data)
    c = Wire(proc2.port)
    assert c.join("doc", "bob")["text"] == "kept"
    c.send(op_msg("doc", 1, [ins(4, "!")], "2"))
    assert c.recv() == {"type": "ack", "doc": "doc", "id": "2", "rev": 2}
    c.close()
    proc2.kill()
    proc3 = server_process(data)
    c = Wire(proc3.port)
    assert c.join("doc", "carol")["text"] == "kept!"
    c.close()


def test_half_written_line_from_client_does_not_affect_others(wire):
    a, b = wire(), wire()
    a.join("d", "alice")
    b.join("d", "bob")
    a.send_raw(b'{"type": "op", "doc": "d", "rev": 0, "id": "x"')
    a.close()
    time.sleep(0.1)
    b.send(op_msg("d", 0, [ins(0, "fine")], "b1"))
    assert b.recv()["type"] == "ack"


def test_name_and_client_length_and_unicode_positions(wire):
    c = wire()
    assert c.join("d" * 100, "alice")["type"] == "joined"
    c.send(op_msg("d" * 100, 0, [ins(0, "\U0001F600é")], "u"))
    assert c.recv()["rev"] == 1
    c.send(op_msg("d" * 100, 1, [dele(1, 1)], "u2"))  # deletes the combining mark's base 'e'
    assert c.recv()["rev"] == 2
    c.send({"type": "join", "doc": "d" * 100, "client": "alice"})
    assert c.recv()["text"] == "\U0001F600́"


def test_unique_ids_are_per_client_name(wire):
    c = wire()
    c.join("d", "alice")
    c.send(op_msg("d", 0, [ins(0, "a")], "same"))
    c.recv()
    c.send({"type": "join", "doc": "d", "client": "bob"})
    c.recv()
    c.send(op_msg("d", 1, [ins(1, "b")], "same"))
    assert c.recv()["rev"] == 2  # a different client name: a new operation
