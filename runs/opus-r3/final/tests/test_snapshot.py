"""Document history: the snapshot request."""

import random
import time

from conftest import Conn
from livedoc_client import apply_op


def snapshot(conn, doc, rev):
    return conn.request({"type": "snapshot", "doc": doc, "rev": rev})


def test_snapshot_every_revision(connect):
    a = connect()
    a.join("d", "alice")
    texts = [""]
    rng = random.Random(7)
    for i in range(250):  # crosses several checkpoints
        n = len(texts[-1])
        if n and rng.random() < 0.3:
            p = rng.randrange(n)
            op = [{"del": p, "len": rng.randint(1, min(5, n - p))}]
        else:
            op = [{"ins": rng.randint(0, n), "text": f"<{i}>"}]
        a.op("d", i, str(i), op)
        texts.append(apply_op(texts[-1], op))
    outsider = connect()  # no join needed
    for r in range(len(texts)):
        assert snapshot(outsider, "d", r) == {"type": "snapshot", "doc": "d", "rev": r, "text": texts[r]}


def test_snapshot_errors(connect):
    a = connect()
    assert snapshot(a, "nope", 0)["code"] == "not_found"
    a.join("d", "alice")
    assert snapshot(a, "d", 0)["text"] == ""  # exists once joined, even if empty
    a.op("d", 0, "1", [{"ins": 0, "text": "x"}])
    assert snapshot(a, "d", 2)["code"] == "bad_rev"
    assert snapshot(a, "d", -1)["code"] == "bad_rev"
    assert snapshot(a, "d", "1")["code"] == "bad_request"
    assert snapshot(a, "d", True)["code"] == "bad_request"
    assert snapshot(a, "", 0)["code"] == "bad_request"
    assert a.request({"type": "snapshot", "doc": "d"})["code"] == "bad_request"
    # a failed join does not create a document
    assert a.join("ghost", "alice", since=3)["code"] == "bad_rev"
    assert snapshot(a, "ghost", 0)["code"] == "not_found"
    assert snapshot(a, "d", 1)["text"] == "x"


def test_snapshot_after_restart(server, connect):
    a = connect()
    a.join("d", "alice")
    a.join("empty", "alice")
    for i in range(130):
        a.op("d", i, str(i), [{"ins": i, "text": chr(ord("a") + i % 26)}])
    server.restart()
    b = Conn(server.port)
    assert snapshot(b, "d", 0)["text"] == ""
    assert snapshot(b, "d", 3)["text"] == "abc"
    assert len(snapshot(b, "d", 117)["text"]) == 117
    assert snapshot(b, "empty", 0)["text"] == ""
    assert snapshot(b, "never", 0)["code"] == "not_found"
    b.close()


def test_snapshot_is_fast_on_long_history(server):
    a = Conn(server.port)
    a.join("long", "alice")
    batch = 500
    rev = 0
    expected = {0: ""}
    text = ""
    # pipeline the ops to build 5000 revisions quickly
    while rev < 5000:
        for i in range(batch):
            op = [{"ins": len(text), "text": f"{rev + i},"}]
            a.send({"type": "op", "doc": "long", "rev": rev + i, "id": str(rev + i), "op": op})
            text = apply_op(text, op)
            expected[rev + i + 1] = text
        for _ in range(batch):
            assert a.recv()["type"] == "ack"
        rev += batch
    for r in (0, 1, 99, 100, 101, 2499, 4999, 5000):
        t0 = time.perf_counter()
        reply = snapshot(a, "long", r)
        elapsed = time.perf_counter() - t0
        assert reply["text"] == expected[r]
        assert elapsed < 0.25, (r, elapsed)
    a.close()
