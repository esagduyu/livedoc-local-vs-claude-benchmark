import os
import threading

from conftest import Conn


def ins(p, t):
    return {"ins": p, "text": t}


def dl(p, n):
    return {"del": p, "len": n}


def test_join_creates_empty_document(connect):
    c = connect()
    assert c.join("doc", "alice") == {"type": "joined", "doc": "doc", "rev": 0, "text": ""}


def test_op_ack_and_broadcast(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    assert a.op("d", 0, "1", [ins(0, "hello")]) == {"type": "ack", "doc": "d", "id": "1", "rev": 1}
    assert b.recv() == {"type": "op", "doc": "d", "rev": 1, "client": "alice", "id": "1", "op": [ins(0, "hello")]}
    a.no_message()
    assert connect().join("d", "carol")["text"] == "hello"


def test_concurrent_ops_are_transformed(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    a.op("d", 0, "a1", [ins(0, "abcdef")])
    b.recv()
    # Both written against revision 1.
    assert a.op("d", 1, "a2", [ins(3, "X")])["rev"] == 2
    assert b.recv()["op"] == [ins(3, "X")]
    assert b.op("d", 1, "b1", [ins(3, "Y"), dl(0, 1)])["rev"] == 3
    msg = a.recv()
    assert msg["op"] == [ins(4, "Y"), dl(0, 1)]   # tie: alice's earlier insert stays left
    assert connect().join("d", "c")["text"] == "bcXYdef"


def test_delete_split_around_concurrent_insert(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    a.op("d", 0, "1", [ins(0, "0123456789")])
    a.op("d", 1, "2", [ins(5, "XY")])
    b.op("d", 1, "3", [dl(2, 6)])
    assert connect().join("d", "c", since=2)["ops"] == [
        {"rev": 3, "client": "bob", "id": "3", "op": [dl(2, 3), dl(4, 3)]}
    ]
    assert connect().join("d", "c")["text"] == "01XY89"


def test_vanished_op_is_committed_as_empty(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    a.op("d", 0, "1", [ins(0, "abc")])
    b.recv()
    a.op("d", 1, "2", [dl(0, 3)])
    b.recv()
    assert b.op("d", 1, "3", [dl(1, 1)]) == {"type": "ack", "doc": "d", "id": "3", "rev": 3}
    assert a.recv()["op"] == []
    assert a.op("d", 3, "4", [])["rev"] == 4


def test_join_since(connect):
    a = connect()
    a.join("d", "alice")
    for i in range(3):
        a.op("d", i, str(i), [ins(0, str(i))])
    assert a.join("d", "alice", since=3)["ops"] == []
    reply = connect().join("d", "bob", since=1)
    assert reply == {"type": "joined", "doc": "d", "rev": 3, "ops": [
        {"rev": 2, "client": "alice", "id": "1", "op": [ins(0, "1")]},
        {"rev": 3, "client": "alice", "id": "2", "op": [ins(0, "2")]},
    ]}
    assert "text" not in reply


def test_exactly_once(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    a.op("d", 0, "x", [ins(0, "a")])
    b.recv()
    a.op("d", 1, "y", [ins(0, "b")])
    b.recv()
    # Resend from a new connection under the same name: original revision, nothing applied.
    a2 = connect()
    a2.join("d", "alice")
    assert a2.op("d", 0, "x", [ins(0, "a")]) == {"type": "ack", "doc": "d", "id": "x", "rev": 1}
    b.no_message()
    # The same id under another name is a different operation.
    assert b.op("d", 2, "x", [ins(0, "c")])["rev"] == 3
    assert connect().join("d", "z")["text"] == "cba"


def test_documents_are_independent(connect):
    a = connect()
    a.join("one", "alice")
    a.join("two", "alice")
    b = connect()
    b.join("two", "bob")
    a.op("one", 0, "1", [ins(0, "1")])
    b.no_message()
    a.op("two", 0, "1", [ins(0, "2")])
    assert b.recv()["doc"] == "two"
    assert connect().join("one", "x")["text"] == "1"


def test_unicode_positions_are_code_points(connect):
    a = connect()
    a.join("d", "alice")
    a.op("d", 0, "1", [ins(0, "h🙂llo wörld")])
    a.op("d", 1, "2", [dl(1, 1), ins(1, "e"), ins(11, "!")])
    assert connect().join("d", "b")["text"] == "hello wörld!"


def test_errors(connect):
    c = connect()

    def err(msg_or_raw):
        if isinstance(msg_or_raw, bytes):
            c.send_raw(msg_or_raw)
        else:
            c.send(msg_or_raw)
        reply = c.recv()
        assert reply["type"] == "error", reply
        return reply

    assert err(b"not json\n")["code"] == "bad_json"
    assert err(b"[1, 2]\n")["code"] == "bad_json"
    assert err(b"\xff\xfe\n")["code"] == "bad_json"
    assert err({"type": "nope"})["code"] == "bad_request"
    assert err({"doc": "d"})["code"] == "bad_request"
    assert err({"type": "join", "doc": "d"})["code"] == "bad_request"
    assert err({"type": "join", "doc": "", "client": "a"})["code"] == "bad_request"
    assert err({"type": "join", "doc": "x" * 101, "client": "a"})["code"] == "bad_request"
    assert err({"type": "join", "doc": "d", "client": ""})["code"] == "bad_request"
    assert err({"type": "join", "doc": "d", "client": "a", "since": "0"})["code"] == "bad_request"
    assert err({"type": "join", "doc": "d", "client": "a", "since": 1})["code"] == "bad_rev"
    assert err({"type": "join", "doc": "d", "client": "a", "since": -1})["code"] == "bad_rev"
    e = err({"type": "op", "doc": "d", "rev": 0, "id": "i1", "op": []})
    assert e["code"] == "not_joined" and e["id"] == "i1"

    c.join("d", "alice")
    c.op("d", 0, "ok", [ins(0, "abc")])
    for msg, code in [
        ({"rev": 0, "id": "e", "op": "x"}, "bad_request"),
        ({"rev": 0, "id": "e", "op": [{"ins": 0}]}, "bad_request"),
        ({"rev": 0, "id": "e"}, "bad_request"),
        ({"rev": "0", "id": "e", "op": []}, "bad_request"),
        ({"rev": 0, "id": 5, "op": []}, "bad_request"),
        ({"rev": 2, "id": "e", "op": []}, "bad_rev"),
        ({"rev": -1, "id": "e", "op": []}, "bad_rev"),
        ({"rev": 1, "id": "e", "op": [ins(4, "x")]}, "out_of_range"),
        ({"rev": 0, "id": "e", "op": [ins(1, "x")]}, "out_of_range"),   # against the text at rev 0
        ({"rev": 1, "id": "e", "op": [dl(1, 3)]}, "out_of_range"),
        ({"rev": 1, "id": "e", "op": [dl(0, 0)]}, "out_of_range"),
        ({"rev": 1, "id": "e", "op": [ins(0, "")]}, "out_of_range"),
        ({"rev": 1, "id": "e", "op": [ins(0, "x"), dl(0, 5)]}, "out_of_range"),
    ]:
        e = err(dict(type="op", doc="d", **msg))
        assert e["code"] == code, (msg, e)
        if "id" in msg:
            assert e["id"] == msg["id"]
    # Nothing was committed; the connection still works and "e" was never used.
    assert c.op("d", 1, "e", [ins(3, "d")])["rev"] == 2
    assert connect().join("d", "z")["text"] == "abcd"


def test_partial_line_and_garbage_do_not_affect_others(server, connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    bad = Conn(server.port)
    bad.send_raw(b'{"type": "join", "doc": "d", "cli')
    bad.close()
    bad = Conn(server.port)
    bad.send_raw(b'{"type": "join", "doc": "d", "client": "m"}\n{"type": "op", "doc": "d", "rev": 0, "id": "q", "op": [{"ins"')
    bad.close()
    assert a.op("d", 0, "1", [ins(0, "fine")])["rev"] == 1
    assert b.recv()["op"] == [ins(0, "fine")]


def test_message_split_across_packets(connect):
    import time
    c = connect()
    for piece in (b'{"type": "jo', b'in", "doc": "d", ', b'"client": "a"}\n'):
        c.send_raw(piece)
        time.sleep(0.05)
    assert c.recv()["type"] == "joined"


def test_ack_follows_all_earlier_broadcasts(connect):
    writers = [connect() for _ in range(4)]
    for i, w in enumerate(writers):
        w.join("d", "w%d" % i)

    def spam(w, i):
        for k in range(50):
            w.send({"type": "op", "doc": "d", "rev": 0, "id": "%d-%d" % (i, k), "op": [ins(0, "x")]})

    threads = [threading.Thread(target=spam, args=(w, i)) for i, w in enumerate(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for w in writers:
        last, acks = 0, 0
        while acks < 50:
            m = w.recv()
            assert m["type"] in ("op", "ack"), m
            assert m["rev"] == last + 1     # broadcasts and acks together cover every revision in order
            last = m["rev"]
            acks += m["type"] == "ack"
    assert connect().join("d", "z") == {"type": "joined", "doc": "d", "rev": 200, "text": "x" * 200}


def test_persistence_across_sigkill(tmp_path):
    from conftest import ServerProcess
    data = tmp_path / "data"           # does not exist yet; the server creates it
    srv = ServerProcess(data).start()
    try:
        assert os.path.isdir(data)
        a = Conn(srv.port)
        a.join("d", "alice")
        a.op("d", 0, "1", [ins(0, "hello")])
        a.op("d", 1, "2", [ins(5, " world")])
        a.join("other", "alice")
        a.op("other", 0, "1", [ins(0, "x")])
        a.close()

        srv.restart()
        b = Conn(srv.port)
        assert b.join("d", "bob") == {"type": "joined", "doc": "d", "rev": 2, "text": "hello world"}
        assert b.join("d", "bob", since=1)["ops"] == [{"rev": 2, "client": "alice", "id": "2", "op": [ins(5, " world")]}]
        assert b.join("other", "bob")["text"] == "x"
        a = Conn(srv.port)
        a.join("d", "alice")
        assert a.op("d", 0, "1", [ins(0, "hello")]) == {"type": "ack", "doc": "d", "id": "1", "rev": 1}
        assert a.op("d", 2, "3", [ins(0, "> ")])["rev"] == 3
        a.close()
        b.close()

        # A torn final record (the process died mid-write, so it was never acked) is dropped.
        with open(os.path.join(data, os.listdir(data)[0]), "ab") as f:
            f.write(b'{"doc": "d", "rev": 4, "cli')
        srv.restart()
        c = Conn(srv.port)
        assert c.join("d", "carol")["text"] == "> hello world"
        assert c.op("d", 3, "4", [ins(0, "!")])["rev"] == 4
        c.close()
        srv.restart()
        c = Conn(srv.port)
        assert c.join("d", "carol") == {"type": "joined", "doc": "d", "rev": 4, "text": "!> hello world"}
        c.close()
    finally:
        srv.kill()
