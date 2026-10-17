import random
import time

import pytest

from conftest import Raw
from livedoc_client import Client, apply_op


def op_msg(doc, rev, op_id, op):
    return {"type": "op", "doc": doc, "rev": rev, "id": op_id, "op": op}


def snap(doc, rev):
    return {"type": "snapshot", "doc": doc, "rev": rev}


def build_history(conn, doc, n, seed=0):
    """Commit `n` random ops (pipelined) and return the text at every revision."""
    rng = random.Random(seed)
    texts = [""]
    for i in range(n):
        text = texts[-1]
        if text and rng.random() < 0.3:
            p = rng.randrange(len(text))
            op = [{"del": p, "len": rng.randint(1, min(5, len(text) - p))}]
        else:
            op = [{"ins": rng.randint(0, len(text)), "text": rng.choice(["ab", "ü", "😀", "xyz"])}]
        texts.append(apply_op(text, op))
        conn.send(op_msg(doc, i, f"op{i}", op))
    for i in range(n):
        assert conn.recv(timeout=30)["rev"] == i + 1
    return texts


def test_snapshot_every_revision(raw):
    a = raw()
    a.join("d", "alice")
    texts = build_history(a, "d", 250)
    reader = raw()  # snapshots don't require joining
    for r in range(len(texts)):
        assert reader.request(snap("d", r)) == {"type": "snapshot", "doc": "d", "rev": r,
                                                "text": texts[r]}


def test_snapshot_of_new_empty_document(raw):
    raw().join("empty", "alice")
    assert raw().request(snap("empty", 0)) == \
        {"type": "snapshot", "doc": "empty", "rev": 0, "text": ""}


@pytest.mark.parametrize("msg, code", [
    (snap("missing", 0), "not_found"),
    (snap("d", 3), "bad_rev"),
    (snap("d", -1), "bad_rev"),
    (snap("d", "1"), "bad_request"),
    (snap("d", None), "bad_request"),
    (snap("d", 1.0), "bad_request"),
    ({"type": "snapshot", "doc": "d"}, "bad_request"),
    ({"type": "snapshot", "rev": 0}, "bad_request"),
    (snap("", 0), "bad_request"),
    (snap("x" * 101, 0), "bad_request"),
])
def test_snapshot_errors(raw, msg, code):
    a = raw()
    a.join("d", "alice")
    a.request(op_msg("d", 0, "1", [{"ins": 0, "text": "ab"}]))
    a.request(op_msg("d", 1, "2", [{"ins": 0, "text": "c"}]))
    c = raw()
    reply = c.request({**msg, "id": "q"})
    assert reply["type"] == "error" and reply["code"] == code and reply["id"] == "q", reply
    assert c.request(snap("d", 1))["text"] == "ab"  # connection still works


def test_snapshot_does_not_create_document(raw):
    c = raw()
    assert c.request(snap("ghost", 0))["code"] == "not_found"
    assert c.request(snap("ghost", 0))["code"] == "not_found"
    assert c.join("ghost", "a")["rev"] == 0


def test_snapshot_after_restart(server, raw):
    a = raw()
    a.join("d", "alice")
    texts = build_history(a, "d", 230, seed=3)
    raw().join("empty", "bob")
    server.restart()
    c = Raw(server.port)
    try:
        for r in [0, 1, 99, 100, 101, 199, 200, 229, 230]:
            assert c.request(snap("d", r))["text"] == texts[r]
        assert c.request(snap("empty", 0))["text"] == ""
        assert c.request(snap("nope", 0))["code"] == "not_found"
        # history keeps growing correctly after the restart
        c.join("d", "alice")
        assert c.request(op_msg("d", 230, "new", [{"ins": 0, "text": "!"}]))["rev"] == 231
        assert c.request(snap("d", 231))["text"] == "!" + texts[230]
        assert c.request(snap("d", 230))["text"] == texts[230]
    finally:
        c.close()


def test_snapshot_is_fast_for_long_history(server, raw):
    a = raw()
    a.join("long", "alice")
    texts = build_history(a, "long", 5000, seed=5)
    reader = raw()
    rng = random.Random(1)
    revs = [0, 1, 99, 4999, 5000] + [rng.randint(0, 5000) for _ in range(30)]
    for r in revs:
        start = time.monotonic()
        reply = reader.request(snap("long", r))
        assert time.monotonic() - start < 0.25
        assert reply["text"] == texts[r]
    server.restart()  # also fast when everything was rebuilt from the log
    reader = Raw(server.port)
    try:
        for r in [4321, 17, 5000]:
            start = time.monotonic()
            reply = reader.request(snap("long", r))
            assert time.monotonic() - start < 1.0  # includes loading the log
            assert reply["text"] == texts[r]
        start = time.monotonic()
        assert reader.request(snap("long", 2500))["text"] == texts[2500]
        assert time.monotonic() - start < 0.25
    finally:
        reader.close()


def test_client_snapshot(server):
    with Client("127.0.0.1", server.port, "d", "alice") as a:
        a.insert(0, "one")
        assert a.wait_synced()
        a.insert(3, " two")
        assert a.wait_synced()
        assert [a.snapshot(r) for r in range(3)] == ["", "one", "one two"]
        with pytest.raises(LookupError):
            a.snapshot(9)
        assert a.snapshot(1) == "one"
