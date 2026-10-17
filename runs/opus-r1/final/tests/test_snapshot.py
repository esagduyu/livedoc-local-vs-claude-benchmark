import json
import random
import time

from conftest import Conn


def ins(p, t):
    return {"ins": p, "text": t}


def dl(p, n):
    return {"del": p, "len": n}


def snapshot(conn, doc, rev):
    return conn.request({"type": "snapshot", "doc": doc, "rev": rev})


def build(conn, doc, count, seed=0):
    """Commit `count` random ops (pipelined) and return the text at every revision."""
    rng = random.Random(seed)
    conn.join(doc, "writer")
    texts = [""]
    lines = []
    for r in range(count):
        text = texts[-1]
        if text and rng.random() < 0.3:
            p = rng.randrange(len(text))
            op = [dl(p, rng.randint(1, min(5, len(text) - p)))]
        else:
            op = [ins(rng.randint(0, len(text)), rng.choice(["ab", "ü", "🙂x", "hello "]))]
        texts.append(_apply(text, op))
        lines.append({"type": "op", "doc": doc, "rev": r, "id": str(r), "op": op})
    conn.send_raw(b"".join((json.dumps(m) + "\n").encode() for m in lines))
    for r in range(count):
        assert conn.recv(timeout=30) == {"type": "ack", "doc": doc, "id": str(r), "rev": r + 1}
    return texts


def _apply(text, op):
    for e in op:
        if "ins" in e:
            text = text[:e["ins"]] + e["text"] + text[e["ins"]:]
        else:
            text = text[:e["del"]] + text[e["del"] + e["len"]:]
    return text


def test_every_revision(connect):
    texts = build(connect(), "d", 300)
    reader = connect()                                   # does not join
    for r in range(len(texts)):
        assert snapshot(reader, "d", r) == {"type": "snapshot", "doc": "d", "rev": r, "text": texts[r]}


def test_snapshot_errors(connect):
    w = connect()
    w.join("d", "w")
    w.op("d", 0, "1", [ins(0, "x")])
    c = connect()
    for msg, code in [
        ({"doc": "nope", "rev": 0}, "not_found"),
        ({"doc": "d", "rev": 2}, "bad_rev"),
        ({"doc": "d", "rev": -1}, "bad_rev"),
        ({"doc": "d"}, "bad_request"),
        ({"doc": "d", "rev": "1"}, "bad_request"),
        ({"doc": "d", "rev": True}, "bad_request"),
        ({"doc": "", "rev": 0}, "bad_request"),
        ({"rev": 0}, "bad_request"),
    ]:
        reply = c.request(dict(type="snapshot", **msg))
        assert reply["type"] == "error" and reply["code"] == code, (msg, reply)
    # A failed request does not create the document.
    assert snapshot(c, "nope", 0)["code"] == "not_found"
    assert c.request({"type": "join", "doc": "nope", "client": "c", "since": 1})["code"] == "bad_rev"
    assert snapshot(c, "nope", 0)["code"] == "not_found"
    c.join("nope", "c")
    assert snapshot(c, "nope", 0) == {"type": "snapshot", "doc": "nope", "rev": 0, "text": ""}


def test_long_history_is_fast_and_survives_restart(server):
    w = Conn(server.port)
    texts = build(w, "long", 5000, seed=7)
    w.join("empty", "w")                                 # exists, but has no operations
    w.close()

    def check():
        c = Conn(server.port)
        revs = [0, 1, 63, 64, 65, 2500, 4031, 4999, 5000] + random.Random(1).sample(range(5001), 40)
        for r in revs:
            start = time.perf_counter()
            reply = snapshot(c, "long", r)
            assert time.perf_counter() - start < 0.25, r
            assert reply["text"] == texts[r]
        assert snapshot(c, "empty", 0) == {"type": "snapshot", "doc": "empty", "rev": 0, "text": ""}
        c.close()

    check()
    server.restart()
    check()
