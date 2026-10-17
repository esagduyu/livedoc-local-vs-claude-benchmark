"""Client-library checks. Run in a subprocess: python client_driver.py CHECK PROJECT_DIR [AGENT_SERVER_PORT]
Prints one JSON line: {"ok": bool, "detail": str}."""
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import traceback

GRADER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, GRADER)
import lg_ot as ot  # noqa: E402
from lg_wire import FakeServer, RawClient, free_port, wait_until  # noqa: E402

CHECKS = {}


def check(cid):
    def deco(fn):
        CHECKS[cid] = fn
        return fn
    return deco


def load_client(project):
    sys.path.insert(1, project)
    os.chdir(project)
    import livedoc_client
    return livedoc_client.Client


def eq(got, want, what):
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, want {want!r}")


def connect_fake(Client, text, rev, doc="d", name="ann"):
    fs = FakeServer()
    box = {}

    def make():
        try:
            box["c"] = Client("127.0.0.1", fs.port, doc, name, timeout=5.0)
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=make, daemon=True)
    th.start()
    m = fs.recv()
    eq({k: m.get(k) for k in ("type", "doc", "client")}, {"type": "join", "doc": doc, "client": name}, "join message")
    fs.send({"type": "joined", "doc": doc, "rev": rev, "text": text})
    th.join(6)
    if "e" in box:
        raise box["e"]
    if "c" not in box:
        raise AssertionError("Client() did not return after joined")
    return fs, box["c"]


class Engine:
    """Reference server state for a fake server: transforms incoming client ops like the real server."""

    def __init__(self, text, rev):
        self.text, self.rev = text, rev
        self.log = {}
        self.base_rev = rev
        self.base_text = text

    def text_at(self, r):
        t = self.base_text
        for x in range(self.base_rev + 1, r + 1):
            t = ot.apply(t, self.log[x])
        return t

    def commit(self, op):
        self.rev += 1
        self.log[self.rev] = op
        self.text = ot.apply(self.text, op)
        return self.rev

    def receive(self, base, op):
        ot.apply(self.text_at(base), op)          # must be valid at its base
        return self.commit(ot.transform_over(op, [self.log[x] for x in range(base + 1, self.rev + 1)]))


def expect_op(fs, eng, timeout=3.0):
    m = fs.recv(timeout)
    eq(m.get("type"), "op", "message type")
    eq(m.get("doc"), "d", "doc")
    if not isinstance(m.get("id"), str):
        raise AssertionError("op id is not a string")
    if not 0 <= m.get("rev", -1) <= eng.rev:
        raise AssertionError(f"op based on rev {m.get('rev')} but server is at {eng.rev}")
    return m


def ack(fs, eng, m):
    r = eng.receive(m["rev"], m["op"])
    fs.send({"type": "ack", "doc": "d", "id": m["id"], "rev": r})
    return r


def remote(fs, eng, op, who="bob"):
    r = eng.commit(op)
    fs.send({"type": "op", "doc": "d", "rev": r, "client": who, "id": f"{who}-{r}", "op": op})
    return r


def settle(fs, c, eng, timeout=5.0):
    """Ack whatever the client sends (checking one-in-flight) until it is synced."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if c.wait_synced(0.05) and c.text == eng.text:
            return
        try:
            m = fs.recv(0.3)
        except TimeoutError:
            continue
        extra = fs.no_message(0.25)
        if extra is not None:
            raise AssertionError("client sent a second op before the first was acked")
        if m.get("type") != "op":
            raise AssertionError(f"unexpected message {m}")
        ack(fs, eng, m)
    raise AssertionError(f"not synced: client {c.text!r} vs server {eng.text!r}")


# ------------------------------------------------------------------ stage 1

@check("L1")
def l1(Client, project, port):
    fs, c = connect_fake(Client, "abc", 3)
    eq((c.text, c.rev), ("abc", 3), "text,rev after join")


@check("L2")
def l2(Client, project, port):
    fs, c = connect_fake(Client, "abc", 3)
    eng = Engine("abc", 3)
    c.insert(1, "X")
    eq(c.text, "aXbc", "optimistic local text")
    m = expect_op(fs, eng)
    eq((m["rev"], ot.apply("abc", m["op"])), (3, "aXbc"), "op sent")
    ack(fs, eng, m)
    if not c.wait_synced(3):
        raise AssertionError("wait_synced returned False after ack")
    eq(c.rev, 4, "rev after ack")
    c.delete(0, 2)
    eq(c.text, "bc", "after delete")
    settle(fs, c, eng)
    eq(eng.text, "bc", "server text")


@check("L3")
def l3(Client, project, port):
    fs, c = connect_fake(Client, "abc", 1)
    for fn, args in ((c.insert, (10, "x")), (c.insert, (-1, "x")), (c.delete, (2, 5)), (c.delete, (0, 0)), (c.insert, (0, ""))):
        try:
            fn(*args)
        except ValueError:
            continue
        raise AssertionError(f"{fn.__name__}{args} did not raise ValueError")
    eq(c.text, "abc", "text after rejected edits")
    m = fs.no_message(0.4)
    if m is not None:
        raise AssertionError(f"client sent {m} for an invalid edit")


@check("L4")
def l4(Client, project, port):
    fs, c = connect_fake(Client, "hello world", 5)
    eng = Engine("hello world", 5)
    c.insert(0, "[")
    m = expect_op(fs, eng)
    remote(fs, eng, [{"ins": 5, "text": ","}, {"del": 7, "len": 5}, {"ins": 7, "text": "there"}])
    if not wait_until(lambda: c.text == "[hello, there", 3):
        raise AssertionError(f"text after remote op: {c.text!r}")
    eq(c.rev, 6, "rev after remote op")
    ack(fs, eng, m)
    if not c.wait_synced(3):
        raise AssertionError("not synced after ack")
    eq(c.rev, 7, "rev after ack")
    c.insert(13, "]")
    m = expect_op(fs, eng)
    eq(m["rev"], 7, "next op's base rev")
    ack(fs, eng, m)
    eq(eng.text, "[hello, there]", "server text")


@check("L5")
def l5(Client, project, port):
    fs, c = connect_fake(Client, "abc", 1)
    eng = Engine("abc", 1)
    c.insert(3, "1")
    m = expect_op(fs, eng)
    c.insert(4, "2")
    c.insert(0, "0")
    eq(c.text, "0abc12", "local text")
    extra = fs.no_message(0.6)
    if extra is not None:
        raise AssertionError("client sent a second op while one was in flight")
    ack(fs, eng, m)
    settle(fs, c, eng)
    eq(eng.text, "0abc12", "server text")


@check("L6")
def l6(Client, project, port):
    fs, c = connect_fake(Client, "The cat sat on the mat", 10)
    eng = Engine("The cat sat on the mat", 10)
    c.insert(4, "big ")                       # in flight
    m1 = expect_op(fs, eng)
    c.delete(0, 4)                            # buffered
    c.insert(len(c.text), "!")                # buffered
    remote(fs, eng, [{"ins": 8, "text": "black "}], "bob")
    remote(fs, eng, [{"del": 0, "len": 1}, {"ins": 0, "text": "t"}], "carol")
    remote(fs, eng, [{"del": 12, "len": 4}], "bob")
    if fs.no_message(0.4) is not None:
        raise AssertionError("client sent a second op while one was in flight")
    ack(fs, eng, m1)
    m2 = expect_op(fs, eng)
    remote(fs, eng, [{"ins": len(eng.text), "text": " today"}], "dan")
    remote(fs, eng, [{"ins": 0, "text": "(" }], "dan")
    ack(fs, eng, m2)
    settle(fs, c, eng)
    eq(c.text, eng.text, "client vs server")


@check("L7")
def l7(Client, project, port):
    fs, c = connect_fake(Client, "abc", 2)
    eng = Engine("abc", 2)
    fs.send({"type": "op", "doc": "other", "rev": 3, "client": "z", "id": "z1", "op": [{"ins": 0, "text": "NO"}]})
    remote(fs, eng, [{"ins": 3, "text": "d"}])
    if not wait_until(lambda: c.text == "abcd", 3):
        raise AssertionError(f"text: {c.text!r}")
    eq(c.rev, 3, "rev")


def start_ref_server():
    port = free_port()
    data = tempfile.mkdtemp(prefix="refdata-")
    p = subprocess.Popen([sys.executable, os.path.join(GRADER, "refserver", "server.py"), "--port", str(port),
                          "--data-dir", data], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_until(lambda: RawClient(port).close() is None, 10, 0.1):
        raise AssertionError("reference server failed to start")
    return p, port


def e2e(Client, port, n_clients=4, n_edits=60):
    seed = RawClient(port)
    seed.join("e2e", "seed")
    seed.op("e2e", 0, [{"ins": 0, "text": "Lorem ipsum dolor sit amet."}], "s0")
    clients = [Client("127.0.0.1", port, "e2e", f"u{i}") for i in range(n_clients)]
    errors = []

    def work(i, c):
        rng = random.Random(i)
        try:
            for _ in range(n_edits):
                t = c.text
                if t and rng.random() < 0.4:
                    p = rng.randrange(len(t))
                    try:
                        c.delete(p, min(rng.randint(1, 3), len(t) - p))
                    except ValueError:
                        pass      # text changed underneath us; that's fine
                else:
                    try:
                        c.insert(rng.randint(0, len(t)), rng.choice(["a", "bc", "Ü", "😀"]))
                    except ValueError:
                        pass
                time.sleep(rng.random() * 0.01)
        except Exception as e:  # noqa: BLE001
            errors.append(f"u{i}: {type(e).__name__}: {e}")
    ths = [threading.Thread(target=work, args=(i, c)) for i, c in enumerate(clients)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(60)
    if errors:
        raise AssertionError("; ".join(errors[:2]))
    for c in clients:
        if not c.wait_synced(15):
            raise AssertionError("a client never synced")
    probe = RawClient(port)
    m = probe.join("e2e", "probe")
    if not wait_until(lambda: all(c.rev == m["rev"] for c in clients), 10):
        raise AssertionError(f"client revs {[c.rev for c in clients]} vs server {m['rev']}")
    texts = [c.text for c in clients]
    if len(set(texts)) != 1:
        raise AssertionError("clients diverged")
    eq(texts[0], m["text"], "client vs server text")
    if m["rev"] < 1 + n_clients * 5:
        raise AssertionError(f"only {m['rev']} revisions committed")


@check("L8")
def l8(Client, project, port):
    p, rport = start_ref_server()
    try:
        e2e(Client, rport)
    finally:
        p.kill()


@check("L9")
def l9(Client, project, port):
    e2e(Client, int(port))


# ------------------------------------------------------------------ stage 2

def cursors(c):
    return dict(c.cursors)


@check("K1")
def k1(Client, project, port):
    fs, c = connect_fake(Client, "hello", 2)
    fs.send({"type": "cursor", "doc": "d", "client": "bob", "rev": 2, "pos": 3})
    if not wait_until(lambda: cursors(c) == {"bob": 3}, 3):
        raise AssertionError(f"cursors: {cursors(c)!r}")
    fs.send({"type": "cursor", "doc": "d", "client": "bob", "rev": 2, "pos": 1})
    fs.send({"type": "cursor", "doc": "d", "client": "cy", "rev": 2, "pos": 5})
    if not wait_until(lambda: cursors(c) == {"bob": 1, "cy": 5}, 3):
        raise AssertionError(f"cursors after update: {cursors(c)!r}")


@check("K2")
def k2(Client, project, port):
    fs, c = connect_fake(Client, "hello world", 2)
    eng = Engine("hello world", 2)
    c.insert(0, "XYZ")                                   # unacked
    m = expect_op(fs, eng)
    fs.send({"type": "cursor", "doc": "d", "client": "bob", "rev": 2, "pos": 6})
    if not wait_until(lambda: cursors(c).get("bob") == 9, 3):
        raise AssertionError(f"cursor not moved through pending edit: {cursors(c)!r}")
    ack(fs, eng, m)                                      # rev 3, server text XYZhello world
    c.wait_synced(3)
    remote(fs, eng, [{"ins": 9, "text": "B"}], "bob")    # bob types at his cursor -> 10
    if not wait_until(lambda: cursors(c).get("bob") == 10, 3):
        raise AssertionError(f"own insert at cursor should push it: {cursors(c)!r}")
    remote(fs, eng, [{"ins": 10, "text": "C"}], "carol")  # someone else at bob's cursor -> stays 10
    remote(fs, eng, [{"del": 0, "len": 2}], "carol")      # delete before -> 8
    if not wait_until(lambda: cursors(c).get("bob") == 8 and c.text == eng.text, 3):
        raise AssertionError(f"cursors {cursors(c)!r}, text {c.text!r}")


@check("K3")
def k3(Client, project, port):
    fs, c = connect_fake(Client, "hello", 2, name="ann")
    fs.send({"type": "cursor", "doc": "d", "client": "ann", "rev": 2, "pos": 1})
    fs.send({"type": "cursor", "doc": "d", "client": "bob", "rev": 2, "pos": 2})
    if not wait_until(lambda: cursors(c) == {"bob": 2}, 3):
        raise AssertionError(f"cursors: {cursors(c)!r}")
    fs.send({"type": "leave", "doc": "d", "client": "bob"})
    if not wait_until(lambda: cursors(c) == {}, 3):
        raise AssertionError(f"cursor kept after leave: {cursors(c)!r}")


def main():
    cid, project = sys.argv[1], sys.argv[2]
    port = sys.argv[3] if len(sys.argv) > 3 else None
    try:
        Client = load_client(project)
        CHECKS[cid](Client, project, port)
        out = {"ok": True, "detail": ""}
    except Exception as e:  # noqa: BLE001
        tb = traceback.extract_tb(e.__traceback__)
        where = f" [{tb[-1].name}:{tb[-1].lineno}]" if tb else ""
        out = {"ok": False, "detail": f"{type(e).__name__}: {e}"[:300] + where}
    sys.stdout.write("\n__RESULT__" + json.dumps(out) + "\n")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
