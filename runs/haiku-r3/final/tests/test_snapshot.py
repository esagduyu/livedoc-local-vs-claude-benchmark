import random
import time

import pytest


def join(w, doc, client):
    w.send({"type": "join", "doc": doc, "client": client})
    return w.recv()


def snapshot(w, doc, rev):
    w.send({"type": "snapshot", "doc": doc, "rev": rev})
    return w.recv()


def char_at(i):
    return chr(97 + i % 26)


def append_chars(w, doc, count, start=0, batch=200):
    """Append ``count`` characters as one op each, pipelined in batches.

    Batches keep the server from blocking on a full write buffer while the
    test is still sending.
    """
    for lo in range(start, start + count, batch):
        hi = min(start + count, lo + batch)
        for i in range(lo, hi):
            w.send({"type": "op", "doc": doc, "rev": i, "id": f"c{i}",
                    "op": [{"ins": i, "text": char_at(i)}]})
        for i in range(lo, hi):
            reply = w.recv()
            assert reply == {"type": "ack", "doc": doc, "id": f"c{i}", "rev": i + 1}


def expected(rev):
    return "".join(char_at(i) for i in range(rev))


def test_snapshot_needs_no_join(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    append_chars(w, "d", 3)
    s = connect(server.port)
    assert snapshot(s, "d", 3) == {"type": "snapshot", "doc": "d", "rev": 3, "text": "abc"}


def test_snapshot_at_every_revision_matches_history(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    append_chars(w, "d", 300)
    s = connect(server.port)
    for rev in range(301):
        reply = snapshot(s, "d", rev)
        assert reply == {"type": "snapshot", "doc": "d", "rev": rev, "text": expected(rev)}


def test_snapshot_of_an_empty_document_at_revision_zero(server, connect):
    w = connect(server.port)
    join(w, "empty", "alice")
    assert snapshot(w, "empty", 0) == {"type": "snapshot", "doc": "empty", "rev": 0, "text": ""}


def test_snapshot_of_unknown_document_is_not_found(server, connect):
    w = connect(server.port)
    reply = snapshot(w, "nope", 0)
    assert reply["type"] == "error"
    assert reply["code"] == "not_found"


@pytest.mark.parametrize("rev", [-1, 1])
def test_snapshot_revision_out_of_range_is_bad_rev(server, connect, rev):
    w = connect(server.port)
    join(w, "d", "alice")
    assert snapshot(w, "d", rev)["code"] == "bad_rev"


@pytest.mark.parametrize("msg", [
    {"type": "snapshot", "doc": "d"},
    {"type": "snapshot", "doc": "d", "rev": "0"},
    {"type": "snapshot", "doc": "d", "rev": True},
    {"type": "snapshot", "rev": 0},
    {"type": "snapshot", "doc": "", "rev": 0},
])
def test_malformed_snapshot_is_bad_request(server, connect, msg):
    w = connect(server.port)
    join(w, "d", "alice")
    w.send(msg)
    assert w.recv()["code"] == "bad_request"


def test_document_created_by_join_still_exists_after_restart(server, connect):
    w = connect(server.port)
    join(w, "empty", "alice")
    server.restart()
    assert snapshot(connect(server.port), "empty", 0)["text"] == ""


def test_snapshots_are_correct_after_restart(server, connect):
    w = connect(server.port)
    join(w, "d", "alice")
    append_chars(w, "d", 300)
    server.restart()
    s = connect(server.port)
    for rev in [0, 1, 127, 128, 129, 255, 256, 299, 300]:
        assert snapshot(s, "d", rev)["text"] == expected(rev)


def test_snapshots_of_a_long_history_are_fast(server, connect):
    w = connect(server.port)
    join(w, "long", "alice")
    append_chars(w, "long", 5000)
    s = connect(server.port)
    rng = random.Random(3)
    revs = [0, 5000, 4999, 127, 128] + [rng.randint(0, 5000) for _ in range(15)]
    for rev in revs:
        start = time.perf_counter()
        reply = snapshot(s, "long", rev)
        elapsed = time.perf_counter() - start
        assert reply["text"] == expected(rev)
        assert elapsed < 0.5, f"snapshot at revision {rev} took {elapsed:.3f}s"

    server.restart()
    s = connect(server.port)
    start = time.perf_counter()
    assert snapshot(s, "long", 4321)["text"] == expected(4321)
    assert time.perf_counter() - start < 0.5
