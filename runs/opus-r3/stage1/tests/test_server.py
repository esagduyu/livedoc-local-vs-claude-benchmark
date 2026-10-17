import random
import threading

from conftest import Conn
from livedoc_client import apply_op, transform


def test_join_creates_empty_doc(connect):
    c = connect()
    assert c.join("d", "alice") == {"type": "joined", "doc": "d", "rev": 0, "text": ""}


def test_op_ack_and_broadcast(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    assert a.op("d", 0, "1", [{"ins": 0, "text": "héllo"}]) == {"type": "ack", "doc": "d", "id": "1", "rev": 1}
    assert b.recv() == {"type": "op", "doc": "d", "rev": 1, "client": "alice", "id": "1",
                        "op": [{"ins": 0, "text": "héllo"}]}
    a.expect_silence()
    assert connect().join("d", "carol")["text"] == "héllo"


def test_transform_against_concurrent_ops(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    a.op("d", 0, "a1", [{"ins": 0, "text": "abcdef"}])
    b.recv()
    a.op("d", 1, "a2", [{"ins": 3, "text": "XY"}])  # abcXYdef
    b.recv()
    # bob writes against rev 1 ("abcdef"): insert at the tie point 3, delete "bc"
    ack = b.op("d", 1, "b1", [{"ins": 3, "text": "Z"}, {"del": 1, "len": 2}])
    assert ack["rev"] == 3
    bc = a.recv()
    assert bc["op"] == [{"ins": 5, "text": "Z"}, {"del": 1, "len": 2}]
    assert connect().join("d", "x")["text"] == "aXYZdef"


def test_empty_result_is_still_committed(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    a.op("d", 0, "1", [{"ins": 0, "text": "abc"}])
    b.recv()
    a.op("d", 1, "2", [{"del": 0, "len": 3}])
    b.recv()
    assert b.op("d", 1, "3", [{"del": 1, "len": 1}]) == {"type": "ack", "doc": "d", "id": "3", "rev": 3}
    assert a.recv()["op"] == []
    assert a.op("d", 3, "4", [])["rev"] == 4


def test_join_since(connect):
    a = connect()
    a.join("d", "alice")
    a.op("d", 0, "1", [{"ins": 0, "text": "a"}])
    a.op("d", 1, "2", [{"ins": 1, "text": "b"}])
    r = connect().join("d", "bob", since=1)
    assert r == {"type": "joined", "doc": "d", "rev": 2,
                 "ops": [{"rev": 2, "client": "alice", "id": "2", "op": [{"ins": 1, "text": "b"}]}]}
    assert connect().join("d", "bob", since=2)["ops"] == []
    assert connect().join("d", "bob", since=3)["code"] == "bad_rev"
    assert connect().join("d", "bob", since=-1)["code"] == "bad_rev"


def test_exactly_once(connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    b.join("d", "bob")
    assert a.op("d", 0, "x", [{"ins": 0, "text": "a"}])["rev"] == 1
    b.recv()
    assert a.op("d", 0, "x", [{"ins": 0, "text": "a"}])["rev"] == 1
    # same name from a different connection, too
    c = connect()
    c.join("d", "alice")
    assert c.op("d", 1, "x", [{"ins": 0, "text": "zzz"}])["rev"] == 1
    # but the id is per client name
    assert b.op("d", 1, "x", [{"ins": 0, "text": "b"}])["rev"] == 2
    b.expect_silence()
    assert connect().join("d", "q")["text"] == "ba"


def test_errors(connect):
    c = connect()
    c.send_raw(b"not json\n")
    assert c.recv()["code"] == "bad_json"
    c.send_raw(b"[1, 2]\n")
    assert c.recv()["code"] == "bad_json"
    c.send_raw(b"\xff\xfe\n")
    assert c.recv()["code"] == "bad_json"
    assert c.request({"type": "nope"})["code"] == "bad_request"
    assert c.request({"type": "join", "doc": "", "client": "a"})["code"] == "bad_request"
    assert c.request({"type": "join", "doc": "x" * 101, "client": "a"})["code"] == "bad_request"
    assert c.request({"type": "join", "doc": "d", "client": ""})["code"] == "bad_request"
    assert c.request({"type": "join", "doc": "d", "client": "a", "since": "0"})["code"] == "bad_request"
    r = c.op("d", 0, "1", [])
    assert r == {"type": "error", "code": "not_joined", "message": r["message"], "id": "1"}
    c.join("d", "a")
    assert c.op("d", True, "1", [])["code"] == "bad_request"
    assert c.request({"type": "op", "doc": "d", "rev": 0, "id": 5, "op": []})["code"] == "bad_request"
    assert c.op("d", 0, "1", {"ins": 0})["code"] == "bad_request"
    assert c.op("d", 0, "1", [{"ins": 0}])["code"] == "bad_request"
    assert c.op("d", 0, "1", [{"ins": 0, "text": 3}])["code"] == "bad_request"
    assert c.op("d", 1, "1", [])["code"] == "bad_rev"
    assert c.op("d", -1, "1", [])["code"] == "bad_rev"
    assert c.op("d", 0, "1", [{"ins": 1, "text": "a"}])["code"] == "out_of_range"
    assert c.op("d", 0, "1", [{"ins": 0, "text": ""}])["code"] == "out_of_range"
    assert c.op("d", 0, "1", [{"ins": 0, "text": "ab"}, {"del": 1, "len": 2}])["code"] == "out_of_range"
    assert c.op("d", 0, "1", [{"ins": 0, "text": "ab"}, {"del": 1, "len": 0}])["code"] == "out_of_range"
    # nothing was committed, and the connection still works
    assert c.op("d", 0, "1", [{"ins": 0, "text": "ok"}]) == {"type": "ack", "doc": "d", "id": "1", "rev": 1}


def test_out_of_range_checked_against_base_revision(connect):
    c = connect()
    c.join("d", "a")
    c.op("d", 0, "1", [{"ins": 0, "text": "abc"}])
    # valid against rev 1 text, but not against rev 0
    assert c.op("d", 0, "2", [{"del": 0, "len": 1}])["code"] == "out_of_range"
    assert c.op("d", 1, "2", [{"del": 0, "len": 1}])["rev"] == 2


def test_half_line_disconnect_does_not_affect_others(server, connect):
    a, b = connect(), connect()
    a.join("d", "alice")
    bad = Conn(server.port)
    bad.join("d", "mallory")
    bad.send_raw(b'{"type": "op", "doc": "d", "rev": 0, "id": "1", "op": [{"ins": 0, "te')
    bad.close()
    b.join("d", "bob")
    assert a.op("d", 0, "1", [{"ins": 0, "text": "x"}])["rev"] == 1
    assert b.recv()["rev"] == 1


def test_multiple_docs_one_connection(connect):
    c, other = connect(), connect()
    c.join("one", "a")
    c.join("two", "a")
    other.join("two", "b")
    other.op("two", 0, "1", [{"ins": 0, "text": "2"}])
    assert c.recv()["doc"] == "two"
    assert c.op("one", 0, "1", [{"ins": 0, "text": "1"}])["rev"] == 1
    other.expect_silence()


def test_persistence_across_sigkill(server, connect):
    a = connect()
    a.join("d", "alice")
    a.join("e/../ü", "alice")
    for i in range(20):
        a.op("d", i, f"id{i}", [{"ins": i, "text": str(i % 10)}])
    a.op("e/../ü", 0, "e1", [{"ins": 0, "text": "😀"}])
    server.restart()
    b = Conn(server.port)
    assert b.join("d", "bob") == {"type": "joined", "doc": "d", "rev": 20, "text": "01234567890123456789"}
    assert b.join("e/../ü", "bob")["text"] == "😀"
    assert len(b.join("d", "bob", since=5)["ops"]) == 15
    b.join("d", "alice")
    assert b.op("d", 0, "id3", [{"ins": 0, "text": "dup"}])["rev"] == 4
    assert b.op("d", 20, "new", [{"del": 0, "len": 1}])["rev"] == 21
    server.restart()
    c = Conn(server.port)
    assert c.join("d", "carol")["text"] == "1234567890123456789"
    b.close()
    c.close()


def test_torn_log_tail_is_ignored(server, connect):
    a = connect()
    a.join("d", "alice")
    a.op("d", 0, "1", [{"ins": 0, "text": "a"}])
    server.kill()
    with open(f"{server.data_dir}/ops.jsonl", "ab") as f:
        f.write(b'{"doc":"d","rev":2,"cli')
    server.start()
    b = Conn(server.port)
    assert b.join("d", "bob")["rev"] == 1
    assert b.op("d", 1, "2", [{"ins": 1, "text": "b"}])["rev"] == 2
    server.restart()
    b.close()
    assert Conn(server.port).join("d", "x")["text"] == "ab"


def test_concurrent_raw_clients_converge(server):
    """Connections fire ops concurrently; each replica tracks the text by
    transforming its own op against the broadcasts that preceded its ack,
    and must match the server exactly."""
    n_clients, n_ops = 6, 40
    finals = {}

    def worker(i):
        rng = random.Random(i)
        c = Conn(server.port)
        r = c.join("race", f"c{i}")
        text, rev = r["text"], r["rev"]
        for k in range(n_ops):
            n = len(text)
            op = []
            for _ in range(rng.randint(1, 3)):
                if n and rng.random() < 0.4:
                    p = rng.randrange(n)
                    m = rng.randint(1, min(3, n - p))
                    op.append({"del": p, "len": m})
                    n -= m
                else:
                    op.append({"ins": rng.randint(0, n), "text": f"{i}"})
                    n += 1
            c.send({"type": "op", "doc": "race", "rev": rev, "id": str(k), "op": op})
            while True:
                m = c.recv()
                assert m["rev"] == rev + 1
                rev = m["rev"]
                if m["type"] == "ack":
                    break
                op, _ = transform(op, m["op"])
                text = apply_op(text, m["op"])
            text = apply_op(text, op)
        finals[i] = (c, text, rev)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = Conn(server.port).join("race", "z")
    assert final["rev"] == n_clients * n_ops
    for c, text, rev in finals.values():
        while rev < final["rev"]:
            m = c.recv()
            text = apply_op(text, m["op"])
            rev = m["rev"]
        assert text == final["text"]
        c.close()
    hist = Conn(server.port).join("race", "z", since=0)
    text = ""
    for e in hist["ops"]:
        text = apply_op(text, e["op"])
    assert text == final["text"]


def test_overlong_line_is_rejected(connect):
    c = connect()
    c.join("d", "a")
    c.send_raw(b"x" * (17 * 1024 * 1024) + b"\n")
    assert c.recv()["code"] == "bad_request"
    assert c.op("d", 0, "1", [{"ins": 0, "text": "ok"}])["rev"] == 1
