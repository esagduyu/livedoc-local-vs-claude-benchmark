"""Black-box checks against the agent's server.py, using raw protocol clients."""
import contextlib
import os
import random
import tempfile
import threading
import time
import uuid

import lg_ot as ot
from lg_wire import RawClient, ServerProc, wait_until

CHECKS = []


def check(cid, stage, title):
    def deco(fn):
        CHECKS.append((cid, stage, title, fn))
        return fn
    return deco


class StartFail(Exception):
    pass


class Ctx:
    def __init__(self, project, python, tmp):
        self.project, self.python, self.tmp = project, python, tmp
        self.start_failed = False

    def new_server(self, data_dir=None, port=None):
        if self.start_failed:
            raise StartFail("server did not start earlier")
        data_dir = data_dir or tempfile.mkdtemp(prefix="data-", dir=self.tmp)
        s = ServerProc(self.project, data_dir, self.python, port=port)
        if not s.start():
            self.start_failed = True
            raise StartFail("server did not start: " + s.tail(300))
        return s


@contextlib.contextmanager
def server(ctx):
    s = ctx.new_server()
    clients = []

    def connect():
        c = RawClient(s.port)
        clients.append(c)
        return c
    try:
        yield s, connect
    finally:
        for c in clients:
            with contextlib.suppress(Exception):
                c.close()
        s.stop()


def uid():
    return uuid.uuid4().hex[:12]


def text_of(connect, doc):
    c = connect()
    m = c.join(doc, "inspector-" + uid())
    return m["text"], m["rev"]


def assert_eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, want {want!r}")


def setup_doc(connect, doc, text):
    """Create doc with `text` at rev 1 (or rev 0 if empty)."""
    c = connect()
    c.join(doc, "setup")
    if text:
        c.op(doc, 0, [{"ins": 0, "text": text}], uid())
    return c


# ---------------------------------------------------------------- basics

@check("S1", 1, "server starts and accepts connections")
def s1(ctx):
    with server(ctx) as (s, connect):
        connect()


@check("S2", 1, "join creates an empty doc at rev 0")
def s2(ctx):
    with server(ctx) as (s, connect):
        m = connect().join("fresh", "alice")
        assert_eq((m.get("type"), m.get("doc"), m.get("rev"), m.get("text")), ("joined", "fresh", 0, ""), "joined")


@check("S3", 1, "sequential ops are acked with consecutive revs and applied")
def s3(ctx):
    with server(ctx) as (s, connect):
        a = connect()
        a.join("d", "alice")
        r1, _ = a.op("d", 0, [{"ins": 0, "text": "hello"}], "i1")
        r2, _ = a.op("d", 1, [{"ins": 5, "text": " world"}], "i2")
        assert_eq((r1, r2), (1, 2), "ack revs")
        assert_eq(text_of(connect, "d"), ("hello world", 2), "text,rev")


@check("S4", 1, "ops are broadcast to other joined connections (not the sender)")
def s4(ctx):
    with server(ctx) as (s, connect):
        a, b = connect(), connect()
        a.join("d", "alice")
        b.join("d", "bob")
        _, skipped = a.op("d", 0, [{"ins": 0, "text": "hi"}], "x1")
        assert_eq([x for x in skipped if x.get("type") == "op"], [], "sender received its own broadcast")
        m = b.recv()
        assert_eq({k: m.get(k) for k in ("type", "doc", "rev", "client", "id")},
                  {"type": "op", "doc": "d", "rev": 1, "client": "alice", "id": "x1"}, "broadcast")
        assert_eq(ot.apply("", m["op"]), "hi", "broadcast op effect")
        extra = [x for x in a.quiet(0.4) if x.get("type") == "op"]
        assert_eq(extra, [], "sender received its own broadcast")


@check("S5", 1, "deletes and multi-edit ops apply left to right")
def s5(ctx):
    with server(ctx) as (s, connect):
        a = setup_doc(connect, "d", "abcdef")
        a.op("d", 1, [{"del": 1, "len": 2}, {"ins": 2, "text": "XY"}, {"del": 0, "len": 1}], uid())
        assert_eq(text_of(connect, "d"), ("dXYef", 2), "text,rev")


@check("S6", 1, "positions are Unicode code points")
def s6(ctx):
    with server(ctx) as (s, connect):
        a = setup_doc(connect, "u", "h😀llo wörld")
        a.op("u", 1, [{"ins": 2, "text": "X"}], uid())
        a.op("u", 2, [{"del": 1, "len": 1}, {"ins": 11, "text": "!"}], uid())
        assert_eq(text_of(connect, "u")[0], "hXllo wörld!", "text")


@check("S7", 1, "documents are isolated")
def s7(ctx):
    with server(ctx) as (s, connect):
        a, b = connect(), connect()
        a.join("one", "alice")
        b.join("two", "bob")
        a.op("one", 0, [{"ins": 0, "text": "only in one"}], uid())
        leaked = [m for m in b.quiet(0.4) if m.get("type") == "op"]
        assert_eq(leaked, [], "broadcast leaked to other doc")
        assert_eq(text_of(connect, "two"), ("", 0), "other doc")


@check("S8", 1, "join with `since` returns the committed ops after that rev")
def s8(ctx):
    with server(ctx) as (s, connect):
        a = connect()
        a.join("d", "alice")
        ops = [[{"ins": 0, "text": "abc"}], [{"ins": 3, "text": "def"}], [{"del": 0, "len": 2}], [{"ins": 1, "text": "Z"}]]
        for i, op in enumerate(ops):
            a.op("d", i, op, f"op{i}")
        m = connect().join("d", "late", since=1)
        assert_eq(m.get("rev"), 4, "rev")
        got = m.get("ops")
        assert_eq([(o.get("rev"), o.get("client"), o.get("id")) for o in got],
                  [(2, "alice", "op1"), (3, "alice", "op2"), (4, "alice", "op3")], "ops metadata")
        t = "abc"
        for o in got:
            t = ot.apply(t, o["op"])
        assert_eq(t, "cZdef", "replayed text")
        assert_eq(connect().join("d", "late2", since=4).get("ops"), [], "since=current")


# ---------------------------------------------------------------- transformation

def concurrent(connect, base_text, first, second, doc="t"):
    """Commit `first` (by alice) and then `second` (by bob), both based on rev 1 (rev 0 if base empty).
    Returns (final text, alice's broadcast of bob's op)."""
    setup_doc(connect, doc, base_text)
    base = 1 if base_text else 0
    a, b = connect(), connect()
    a.join(doc, "alice")
    b.join(doc, "bob")
    a.op(doc, base, first, uid())
    rb, _ = b.op(doc, base, second, uid())
    assert_eq(rb, base + 2, "second op's rev")
    m = [x for x in a.quiet(0.5) if x.get("type") == "op" and x.get("rev") == base + 2]
    if not m:
        raise AssertionError("alice did not receive bob's transformed op")
    return text_of(connect, doc)[0], m[0]["op"]


def expect_concurrent(connect, base_text, first, second, want):
    ref_first = ot.apply(base_text, first)
    ref = ot.apply(ref_first, ot.transform_over(second, [first]))
    assert ref == want, f"grader self-check failed: {ref!r} != {want!r}"
    got, bop = concurrent(connect, base_text, first, second)
    assert_eq(got, want, "server text")
    assert_eq(ot.apply(ref_first, bop), want, "broadcast op applied to alice's text")


@check("T1", 1, "concurrent inserts at the same position: earlier-committed stays left")
def t1(ctx):
    with server(ctx) as (s, connect):
        expect_concurrent(connect, "ab", [{"ins": 1, "text": "E"}], [{"ins": 1, "text": "L"}], "aELb")


@check("T2", 1, "insert inside a concurrently deleted range lands at the deletion point")
def t2(ctx):
    with server(ctx) as (s, connect):
        expect_concurrent(connect, "abcdef", [{"del": 1, "len": 4}], [{"ins": 3, "text": "X"}], "aXf")


@check("T3", 1, "delete spanning a concurrent insert splits and keeps the inserted text")
def t3(ctx):
    with server(ctx) as (s, connect):
        expect_concurrent(connect, "abcdef", [{"ins": 3, "text": "XY"}], [{"del": 1, "len": 4}], "aXYf")


@check("T4", 1, "overlapping deletes; a fully covered delete commits as []")
def t4(ctx):
    with server(ctx) as (s, connect):
        setup_doc(connect, "t", "abcdefgh")
        a, b, c = connect(), connect(), connect()
        a.join("t", "alice")
        b.join("t", "bob")
        c.join("t", "carol")
        a.op("t", 1, [{"del": 2, "len": 4}], uid())
        b.op("t", 1, [{"del": 4, "len": 3}], uid())
        assert_eq(text_of(connect, "t"), ("abh", 3), "after partial overlap")
        r, _ = c.op("t", 1, [{"del": 3, "len": 2}], "covered")
        assert_eq(r, 4, "covered delete still commits")
        assert_eq(text_of(connect, "t"), ("abh", 4), "after covered delete")
        m = [x for x in a.quiet(0.5) if x.get("type") == "op" and x.get("rev") == 4]
        assert m, "no broadcast for rev 4"
        assert_eq(m[0]["op"], [], "broadcast of vanished op")


@check("T5", 1, "an op based on a much older revision is transformed through all later ops")
def t5(ctx):
    with server(ctx) as (s, connect):
        a = setup_doc(connect, "t", "0123456789")
        committed = [[{"ins": 0, "text": "AB"}], [{"del": 4, "len": 3}], [{"ins": 9, "text": "cd"}],
                     [{"del": 0, "len": 1}, {"ins": 5, "text": "e"}], [{"ins": 3, "text": "ff"}], [{"del": 7, "len": 2}]]
        t = "0123456789"
        for i, op in enumerate(committed):
            a.op("t", 2 + i - 1, op, uid())
            t = ot.apply(t, op)
        stale = [{"ins": 5, "text": "Q"}, {"del": 8, "len": 2}, {"ins": 0, "text": "^"}]
        want = ot.apply(t, ot.transform_over(stale, committed))
        b = connect()
        b.join("t", "bob")
        r, _ = b.op("t", 1, stale, uid())
        assert_eq(r, 8, "rev")
        assert_eq(text_of(connect, "t")[0], want, "text")


@check("T6", 1, "multi-edit ops transform against multi-edit ops")
def t6(ctx):
    with server(ctx) as (s, connect):
        base = "the quick brown fox"
        first = [{"ins": 4, "text": "very "}, {"del": 15, "len": 6}, {"ins": 0, "text": ">"}]
        second = [{"del": 0, "len": 4}, {"ins": 6, "text": "!!"}, {"del": 10, "len": 3}]
        want = ot.apply(ot.apply(base, first), ot.transform_over(second, [first]))
        got, bop = concurrent(connect, base, first, second)
        assert_eq(got, want, "server text")
        assert_eq(ot.apply(ot.apply(base, first), bop), want, "broadcast op")


# ---------------------------------------------------------------- errors

def expect_error(c, code, id_=None):
    m, _ = c.recv_type("error")
    assert_eq(m.get("code"), code, "error code")
    if id_ is not None:
        assert_eq(m.get("id"), id_, "error id")


@check("E1", 1, "malformed JSON gets bad_json and the connection keeps working")
def e1(ctx):
    with server(ctx) as (s, connect):
        c = connect()
        c.send_raw(b"{not json\n")
        expect_error(c, "bad_json")
        c.send_raw(b"[1, 2]\n")
        expect_error(c, "bad_json")
        m = c.join("d", "alice")
        assert_eq(m.get("rev"), 0, "join after errors")


@check("E2", 1, "malformed requests get bad_request (with id echoed)")
def e2(ctx):
    with server(ctx) as (s, connect):
        c = connect()
        c.join("d", "alice")
        c.send({"type": "frobnicate", "doc": "d"})
        expect_error(c, "bad_request")
        c.send({"type": "op", "doc": "d", "id": "m1", "op": [{"ins": 0, "text": "x"}]})
        expect_error(c, "bad_request", "m1")
        c.send({"type": "op", "doc": "d", "rev": 0, "id": "m2", "op": {"ins": 0, "text": "x"}})
        expect_error(c, "bad_request", "m2")
        c.send({"type": "op", "doc": "d", "rev": 0, "id": "m3", "op": [{"move": 0}]})
        expect_error(c, "bad_request", "m3")
        c.send({"type": "join", "doc": "e"})
        expect_error(c, "bad_request")
        r, _ = c.op("d", 0, [{"ins": 0, "text": "ok"}], "m4")
        assert_eq((r, text_of(connect, "d")[0]), (1, "ok"), "still works")


@check("E3", 1, "op on a doc not joined gets not_joined")
def e3(ctx):
    with server(ctx) as (s, connect):
        setup_doc(connect, "d", "abc")
        c = connect()
        c.send({"type": "op", "doc": "d", "rev": 1, "id": "n1", "op": [{"ins": 0, "text": "x"}]})
        expect_error(c, "not_joined", "n1")
        assert_eq(text_of(connect, "d"), ("abc", 1), "nothing committed")


@check("E4", 1, "bad_rev and out_of_range (validated against the base revision); nothing committed")
def e4(ctx):
    with server(ctx) as (s, connect):
        c = setup_doc(connect, "d", "abc")
        c.op("d", 1, [{"ins": 3, "text": "def"}], uid())     # rev 2: abcdef
        cases = [(99, [{"ins": 0, "text": "x"}], "bad_rev"),
                 (-1, [{"ins": 0, "text": "x"}], "bad_rev"),
                 (1, [{"ins": 5, "text": "x"}], "out_of_range"),   # valid at rev 2, not at rev 1
                 (2, [{"del": 4, "len": 3}], "out_of_range"),
                 (2, [{"del": 0, "len": 0}], "out_of_range"),
                 (2, [{"ins": 0, "text": ""}], "out_of_range"),
                 (2, [{"ins": 0, "text": "ok"}, {"del": 7, "len": 2}], "out_of_range")]
        for i, (rev, op, code) in enumerate(cases):
            c.send({"type": "op", "doc": "d", "rev": rev, "id": f"e{i}", "op": op})
            expect_error(c, code, f"e{i}")
        assert_eq(text_of(connect, "d"), ("abcdef", 2), "nothing committed")


@check("E5", 1, "a client dying mid-line doesn't affect others")
def e5(ctx):
    with server(ctx) as (s, connect):
        a = connect()
        a.join("d", "alice")
        for _ in range(3):
            bad = connect()
            bad.join("d", "mallory")
            bad.send_raw(b'{"type": "op", "doc": "d", "rev": 0, "id": "half", "op": [{"ins": 0, "te')
            bad.close()
        time.sleep(0.2)
        r, _ = a.op("d", 0, [{"ins": 0, "text": "fine"}], uid())
        assert_eq((r, text_of(connect, "d")[0]), (1, "fine"), "after bad clients")
        assert s.alive(), "server died"


# ---------------------------------------------------------------- exactly-once & persistence

@check("X1", 1, "resending an op id is acked with the original rev and not re-applied")
def x1(ctx):
    with server(ctx) as (s, connect):
        a = connect()
        a.join("d", "alice")
        a.op("d", 0, [{"ins": 0, "text": "ab"}], "once")
        a.op("d", 1, [{"ins": 2, "text": "cd"}], "two")
        r, _ = a.op("d", 0, [{"ins": 0, "text": "ab"}], "once")
        assert_eq(r, 1, "duplicate ack rev")
        b = connect()
        b.join("d", "bob")
        r, _ = b.op("d", 2, [{"ins": 0, "text": "Z"}], "once")    # same id, different client: a new op
        assert_eq(r, 3, "same id from another client")
        assert_eq(text_of(connect, "d"), ("Zabcd", 3), "text,rev")


def persisted_history(connect):
    a = connect()
    a.join("p1", "alice")
    b = connect()
    b.join("p2", "bob")
    rng = random.Random(7)
    texts = {"p1": "", "p2": ""}
    for i in range(20):
        for doc, c in (("p1", a), ("p2", b)):
            t = texts[doc]
            if t and rng.random() < 0.35:
                p = rng.randrange(len(t))
                op = [{"del": p, "len": min(rng.randint(1, 3), len(t) - p)}]
            else:
                op = [{"ins": rng.randint(0, len(t)), "text": rng.choice(["ab", "ü", "😀", "xyz"])}]
            c.op(doc, i, op, f"{doc}-{i}")
            texts[doc] = ot.apply(t, op)
    return texts


@check("P1", 1, "acked ops survive SIGKILL + restart")
def p1(ctx):
    data = tempfile.mkdtemp(prefix="data-", dir=ctx.tmp)
    s = ctx.new_server(data)
    try:
        texts = persisted_history(lambda: RawClient(s.port))
    finally:
        s.kill9()
    s2 = ctx.new_server(data)
    try:
        for doc, want in texts.items():
            c = RawClient(s2.port)
            m = c.join(doc, "after")
            assert_eq((m.get("text"), m.get("rev")), (want, 20), doc)
    finally:
        s2.stop()


@check("P2", 1, "history (`since`) survives restart")
def p2(ctx):
    data = tempfile.mkdtemp(prefix="data-", dir=ctx.tmp)
    s = ctx.new_server(data)
    try:
        c = RawClient(s.port)
        c.join("h", "alice")
        for i, op in enumerate([[{"ins": 0, "text": "one"}], [{"ins": 3, "text": " two"}], [{"del": 0, "len": 4}]]):
            c.op("h", i, op, f"h{i}")
    finally:
        s.kill9()
    s2 = ctx.new_server(data)
    try:
        m = RawClient(s2.port).join("h", "later", since=0)
        ops = m.get("ops") or []
        assert_eq([(o.get("rev"), o.get("client"), o.get("id")) for o in ops],
                  [(1, "alice", "h0"), (2, "alice", "h1"), (3, "alice", "h2")], "history")
        t = ""
        for o in ops:
            t = ot.apply(t, o["op"])
        assert_eq(t, "two", "replayed text")
    finally:
        s2.stop()


@check("X2", 1, "exactly-once survives restart")
def x2(ctx):
    data = tempfile.mkdtemp(prefix="data-", dir=ctx.tmp)
    s = ctx.new_server(data)
    try:
        c = RawClient(s.port)
        c.join("d", "alice")
        c.op("d", 0, [{"ins": 0, "text": "abc"}], "keep")
        c.op("d", 1, [{"ins": 3, "text": "def"}], "keep2")
    finally:
        s.kill9()
    s2 = ctx.new_server(data)
    try:
        c = RawClient(s2.port)
        c.join("d", "alice")
        r, _ = c.op("d", 0, [{"ins": 0, "text": "abc"}], "keep")
        assert_eq(r, 1, "duplicate ack after restart")
        m = RawClient(s2.port).join("d", "x")
        assert_eq((m["text"], m["rev"]), ("abcdef", 2), "not re-applied")
    finally:
        s2.stop()


# ---------------------------------------------------------------- concurrency

class FuzzClient(threading.Thread):
    """Sends random ops (one in flight), sometimes based on a stale revision, and tracks the doc
    from broadcasts using the reference transform."""

    def __init__(self, port, doc, name, n_ops, seed, stale=True, pause=0.0):
        super().__init__(daemon=True)
        self.c = RawClient(port)
        self.doc, self.name, self.n, self.stale, self.pause = doc, name, n_ops, stale, pause
        self.rng = random.Random(seed)
        self.sent = []        # (commit rev, base rev, original op)
        self.error = None
        self.revs_seen = []

    def run(self):
        try:
            m = self.c.join(self.doc, self.name)
            self.texts = {m["rev"]: m["text"]}
            self.log = {}     # rev -> committed op
            self.rev = m["rev"]
            for i in range(self.n):
                base = self.rev
                if self.stale and self.rng.random() < 0.4:
                    base = self.rng.choice([r for r in self.texts if r >= self.rev - 6])
                t = self.texts[base]
                op = self.rand_op(t)
                id_ = f"{self.name}-{i}"
                self.c.send({"type": "op", "doc": self.doc, "rev": base, "id": id_, "op": op})
                while True:
                    msg = self.c.recv(20)
                    if msg.get("type") == "op":
                        self.absorb(msg["rev"], msg["op"])
                    elif msg.get("type") in ("leave", "cursor"):
                        continue
                    elif msg.get("type") == "ack" and msg.get("id") == id_:
                        r = msg["rev"]
                        mine = ot.transform_over(op, [self.log[x] for x in range(base + 1, r)])
                        self.absorb(r, mine)
                        self.sent.append((r, base, op))
                        break
                    else:
                        raise AssertionError(f"{self.name}: unexpected {msg}")
                if self.pause:
                    time.sleep(self.rng.random() * self.pause)
        except Exception as e:  # noqa: BLE001
            self.error = f"{self.name}: {type(e).__name__}: {e}"

    def absorb(self, r, op):
        if r != self.rev + 1:
            raise AssertionError(f"{self.name}: got rev {r} after {self.rev} (gap or reorder)")
        self.revs_seen.append(r)
        self.log[r] = op
        self.texts[r] = ot.apply(self.texts[self.rev], op)
        self.rev = r
        for old in [x for x in self.texts if x < r - 8]:
            del self.texts[old]

    def rand_op(self, t):
        op = []
        for _ in range(self.rng.choice([1, 1, 1, 2, 3])):
            if t and self.rng.random() < 0.45:
                p = self.rng.randrange(len(t))
                e = {"del": p, "len": min(self.rng.randint(1, 4), len(t) - p)}
            else:
                e = {"ins": self.rng.randint(0, len(t)), "text": self.rng.choice(["a", "bc", "D", "éf", "😀", "xyz"])}
            op.append(e)
            t = ot.apply(t, [e])
        return op

    def drain_to(self, rev, timeout=10):
        deadline = time.time() + timeout
        while self.rev < rev:
            msg = self.c.recv(max(0.01, deadline - time.time()))
            if msg.get("type") == "op":
                self.absorb(msg["rev"], msg["op"])
            elif msg.get("type") not in ("leave", "cursor"):
                raise AssertionError(f"{self.name}: unexpected {msg}")


def run_fuzz(connect, port, doc, n_clients, n_ops, stale, timeout, pause=0.0):
    seed_c = connect()
    seed_c.join(doc, "seed")
    seed_c.op(doc, 0, [{"ins": 0, "text": "The quick brown fox jumps over the lazy dog."}], uid())
    fs = [FuzzClient(port, doc, f"c{i}", n_ops, seed=100 + i, stale=stale, pause=pause) for i in range(n_clients)]
    t0 = time.time()
    for f in fs:
        f.start()
    for f in fs:
        f.join(max(0.1, timeout - (time.time() - t0)))
    elapsed = time.time() - t0
    errs = [f.error for f in fs if f.error] + [f"{f.name}: still running" for f in fs if f.is_alive()]
    if errs:
        raise AssertionError("; ".join(errs[:3]))
    # Reference replay in the server's commit order.
    commits = sorted((r, base, op) for f in fs for (r, base, op) in f.sent)
    final_rev = 1 + len(commits)
    assert_eq([r for r, _, _ in commits], list(range(2, final_rev + 1)), "committed revs")
    log = {1: [{"ins": 0, "text": "The quick brown fox jumps over the lazy dog."}]}
    t = ot.apply("", log[1])
    for r, base, op in commits:
        log[r] = ot.transform_over(op, [log[x] for x in range(base + 1, r)])
        t = ot.apply(t, log[r])
    server_text, server_rev = text_of(connect, doc)
    assert_eq(server_rev, final_rev, "final rev")
    assert_eq(server_text, t, "server text vs reference replay")
    for f in fs:
        f.drain_to(final_rev)
        assert_eq(f.texts[f.rev], t, f"{f.name}'s view")
    return elapsed, fs


@check("C1", 1, "fuzz: 6 clients x 50 concurrent ops with stale bases converge to the reference result")
def c1(ctx):
    with server(ctx) as (s, connect):
        run_fuzz(connect, s.port, "fuzz", 6, 50, stale=True, timeout=60, pause=0.01)


@check("C2", 1, "ordering: every client sees each revision exactly once, in order, acks after earlier broadcasts")
def c2(ctx):
    with server(ctx) as (s, connect):
        _, fs = run_fuzz(connect, s.port, "order", 4, 40, stale=False, timeout=60)
        for f in fs:
            start = f.revs_seen[0] if f.revs_seen else None
            assert_eq(f.revs_seen, list(range(start, start + len(f.revs_seen))), f"{f.name} revs")


@check("C3", 1, "scale: 40 clients x 30 ops finish within 30 s and converge")
def c3(ctx):
    with server(ctx) as (s, connect):
        elapsed, _ = run_fuzz(connect, s.port, "scale", 40, 30, stale=True, timeout=30)
        if elapsed > 30:
            raise AssertionError(f"took {elapsed:.1f}s")


@check("C4", 1, "large document: 200k-char doc + 300 edits within 20 s")
def c4(ctx):
    with server(ctx) as (s, connect):
        rng = random.Random(3)
        big = "".join(rng.choice("abcdefghij \n") for _ in range(200_000))
        a = connect()
        a.join("big", "alice")
        t0 = time.time()
        a.op("big", 0, [{"ins": 0, "text": big}], uid())
        t = big
        for i in range(300):
            if i % 3 == 2:
                p = rng.randrange(len(t) - 10)
                op = [{"del": p, "len": 5}]
            else:
                op = [{"ins": rng.randint(0, len(t)), "text": "Q"}]
            a.op("big", 1 + i, op, uid())
            t = ot.apply(t, op)
        got, rev = text_of(connect, "big")
        elapsed = time.time() - t0
        assert_eq(rev, 301, "rev")
        assert got == t, "text mismatch"
        if elapsed > 20:
            raise AssertionError(f"took {elapsed:.1f}s")


# ---------------------------------------------------------------- stage 2: cursors

def cursor_pair(connect, text="hello world"):
    setup_doc(connect, "c", text)
    a, b = connect(), connect()
    a.join("c", "alice")
    b.join("c", "bob")
    return a, b


def next_of(c, typ, timeout=3):
    deadline = time.time() + timeout
    while True:
        m = c.recv(max(0.01, deadline - time.time()))
        if m.get("type") == typ:
            return m


@check("R1", 2, "cursor updates are broadcast to others")
def r1(ctx):
    with server(ctx) as (s, connect):
        a, b = cursor_pair(connect)
        a.send({"type": "cursor", "doc": "c", "rev": 1, "pos": 5})
        m = next_of(b, "cursor")
        assert_eq({k: m.get(k) for k in ("doc", "client", "rev", "pos")},
                  {"doc": "c", "client": "alice", "rev": 1, "pos": 5}, "cursor broadcast")
        own = [x for x in a.quiet(0.3) if x.get("type") in ("cursor", "error")]
        assert_eq(own, [], "sender got a reply")


@check("R2", 2, "stale cursors are moved forward (own insert at the cursor pushes it, others' doesn't)")
def r2(ctx):
    with server(ctx) as (s, connect):
        a, b = cursor_pair(connect)
        b.op("c", 1, [{"ins": 5, "text": "XX"}], uid())       # rev 2, other's insert exactly at 5
        a.op("c", 2, [{"ins": 0, "text": "Y"}], uid())        # rev 3, own insert before
        a.send({"type": "cursor", "doc": "c", "rev": 1, "pos": 5})
        m = next_of(b, "cursor")
        assert_eq((m.get("rev"), m.get("pos")), (3, 6), "moved cursor")
        a.op("c", 3, [{"ins": 6, "text": "ZZZ"}], uid())      # rev 4, own insert at cursor
        b.op("c", 4, [{"ins": 9, "text": "w"}], uid())        # rev 5, other's insert at cursor
        b.send({"type": "cursors", "doc": "c"})
        m = next_of(b, "cursors")
        assert_eq((m.get("rev"), (m.get("cursors") or {}).get("alice")), (5, 9), "after own/other inserts")


@check("R3", 2, "cursors query; cursors move with later deletes")
def r3(ctx):
    with server(ctx) as (s, connect):
        a, b = cursor_pair(connect, "0123456789")
        a.send({"type": "cursor", "doc": "c", "rev": 1, "pos": 6})
        b.send({"type": "cursor", "doc": "c", "rev": 1, "pos": 9})
        next_of(b, "cursor")
        next_of(a, "cursor")
        a.op("c", 1, [{"del": 4, "len": 4}], uid())            # alice's cursor 6 -> 4, bob's 9 -> 5
        b.send({"type": "cursors", "doc": "c"})
        m = next_of(b, "cursors")
        assert_eq((m.get("rev"), m.get("cursors")), (2, {"alice": 4, "bob": 5}), "cursors")


@check("R4", 2, "leave is broadcast when a name's last connection closes")
def r4(ctx):
    with server(ctx) as (s, connect):
        a, b = cursor_pair(connect)
        c1, c2 = connect(), connect()
        c1.join("c", "carol")
        c2.join("c", "carol")
        c1.send({"type": "cursor", "doc": "c", "rev": 1, "pos": 2})
        next_of(a, "cursor")
        c1.close()
        early = [x for x in a.quiet(0.5) if x.get("type") == "leave"]
        assert_eq(early, [], "leave sent while carol still connected")
        c2.close()
        m = next_of(a, "leave")
        assert_eq((m.get("doc"), m.get("client")), ("c", "carol"), "leave")
        a.send({"type": "cursors", "doc": "c"})
        assert "carol" not in (next_of(a, "cursors").get("cursors") or {}), "carol's cursor kept"
        b.close()
        assert_eq(next_of(a, "leave").get("client"), "bob", "leave without a cursor")


@check("R5", 2, "cursor errors: not_joined, bad_rev, out_of_range")
def r5(ctx):
    with server(ctx) as (s, connect):
        a, b = cursor_pair(connect, "abc")
        x = connect()
        x.send({"type": "cursor", "doc": "c", "rev": 1, "pos": 0})
        expect_error(x, "not_joined")
        x.send({"type": "cursors", "doc": "c"})
        expect_error(x, "not_joined")
        a.send({"type": "cursor", "doc": "c", "rev": 7, "pos": 0})
        expect_error(a, "bad_rev")
        a.send({"type": "cursor", "doc": "c", "rev": 1, "pos": 4})
        expect_error(a, "out_of_range")
        a.send({"type": "cursor", "doc": "c", "rev": 0, "pos": 1})
        expect_error(a, "out_of_range")


# ---------------------------------------------------------------- stage 2: history

def build_history(c, doc, n, seed):
    rng = random.Random(seed)
    c.join(doc, "hist")
    texts = [""]
    t = ""
    for i in range(n):
        if t and rng.random() < 0.3:
            p = rng.randrange(len(t))
            op = [{"del": p, "len": min(rng.randint(1, 3), len(t) - p)}]
        else:
            op = [{"ins": rng.randint(0, len(t)), "text": rng.choice(["a", "bb", "ç", "😀"])}]
        c.send({"type": "op", "doc": doc, "rev": i, "id": f"{doc}{i}", "op": op})
        t = ot.apply(t, op)
        texts.append(t)
    for i in range(n):
        m, _ = c.recv_type("ack", timeout=60)
    return texts


def snap(c, doc, rev):
    c.send({"type": "snapshot", "doc": doc, "rev": rev})
    m = c.recv(5)
    return m


@check("H1", 2, "snapshot returns the text at any revision")
def h1(ctx):
    with server(ctx) as (s, connect):
        texts = build_history(connect(), "h", 150, 11)
        q = connect()
        for r in [0, 1, 2, 63, 64, 65, 100, 149, 150] + random.Random(1).sample(range(151), 10):
            m = snap(q, "h", r)
            assert_eq((m.get("type"), m.get("rev"), m.get("text")), ("snapshot", r, texts[r]), f"rev {r}")


@check("H2", 2, "snapshots work after restart")
def h2(ctx):
    data = tempfile.mkdtemp(prefix="data-", dir=ctx.tmp)
    s = ctx.new_server(data)
    try:
        texts = build_history(RawClient(s.port), "h", 80, 12)
    finally:
        s.kill9()
    s2 = ctx.new_server(data)
    try:
        q = RawClient(s2.port)
        for r in (0, 17, 40, 79, 80):
            assert_eq(snap(q, "h", r).get("text"), texts[r], f"rev {r}")
    finally:
        s2.stop()


@check("H3", 2, "snapshot errors: not_found, bad_rev; no join needed")
def h3(ctx):
    with server(ctx) as (s, connect):
        setup_doc(connect, "h", "abc")
        q = connect()
        assert_eq(snap(q, "h", 1).get("text"), "abc", "unjoined snapshot")
        assert_eq(snap(q, "nope", 0).get("code"), "not_found", "unknown doc")
        assert_eq(snap(q, "h", 2).get("code"), "bad_rev", "future rev")
        assert_eq(snap(q, "h", -1).get("code"), "bad_rev", "negative rev")


@check("H4", 2, "snapshots of a 5,000-revision document are fast (each < 1 s)")
def h4(ctx):
    with server(ctx) as (s, connect):
        texts = build_history(connect(), "long", 5000, 13)
        q = connect()
        worst = 0.0
        for r in random.Random(2).sample(range(5001), 20):
            t0 = time.time()
            m = snap(q, "long", r)
            worst = max(worst, time.time() - t0)
            assert_eq(m.get("text"), texts[r], f"rev {r}")
        if worst >= 1.0:
            raise AssertionError(f"slowest snapshot {worst:.2f}s")
