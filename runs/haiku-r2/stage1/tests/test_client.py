import random
import socket
import threading
import time

import pytest

from conftest import Wire
from livedoc_client import Client


def wait_converged(port, doc, clients, timeout=10.0):
    """Wait until every client has acknowledged its edits and holds the final server state."""
    for c in clients:
        assert c.wait_synced(timeout)
    # No client has anything pending, so the server state is final.
    with Client("127.0.0.1", port, doc, "observer") as observer:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(c.rev == observer.rev and c.text == observer.text for c in clients):
                return observer.text
            time.sleep(0.01)
        raise AssertionError(
            f"clients did not converge: server rev {observer.rev}, "
            f"clients {[(c.rev, c.text) for c in clients]}, server {observer.text!r}"
        )


def wait_for_rev(client, rev, timeout=10.0):
    deadline = time.monotonic() + timeout
    while client.rev < rev:
        assert time.monotonic() < deadline, "timed out waiting for a remote revision"
        time.sleep(0.01)


def test_join_and_local_edits(server):
    port, _ = server
    with Client("127.0.0.1", port, "d", "alice") as c:
        c.insert(0, "hello")
        c.insert(5, " world")
        c.delete(0, 1)
        assert c.text == "ello world"
        assert c.wait_synced(5)
        # The second and third edits were buffered behind the first and sent as one operation.
        assert c.rev == 2
        with Client("127.0.0.1", port, "d", "bob") as b:
            assert b.text == "ello world"
            assert b.rev == 2


def test_invalid_local_edits_raise_value_error(server):
    port, _ = server
    with Client("127.0.0.1", port, "d", "alice") as c:
        with pytest.raises(ValueError):
            c.insert(1, "x")
        with pytest.raises(ValueError):
            c.insert(0, "")
        with pytest.raises(ValueError):
            c.delete(0, 1)
        with pytest.raises(ValueError):
            c.delete(0, 0)
        assert c.text == ""
        assert c.wait_synced(5)
        assert c.rev == 0


def test_remote_edits_merge_with_pending_local_edits(server):
    port, _ = server
    with Client("127.0.0.1", port, "d", "alice") as a, Client("127.0.0.1", port, "d", "bob") as b:
        a.insert(0, "abc")
        assert a.wait_synced()
        wait_for_rev(b, 1)
        b.insert(0, "X")
        a.insert(3, "Y")
        a.delete(0, 1)
        # "X" lands before the "a" that is being deleted; "Y" lands at the end.
        assert wait_converged(port, "d", [a, b]) == "XbcY"
        assert a.text == b.text == "XbcY"


def test_join_timeout_when_server_is_silent():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with pytest.raises(OSError):
            Client("127.0.0.1", port, "d", "alice", timeout=0.3)
    finally:
        listener.close()


def test_reading_text_and_rev_from_another_thread_during_edits(server):
    port, _ = server
    stop = threading.Event()
    failures = []

    with Client("127.0.0.1", port, "d", "bob") as b:
        def reader():
            try:
                while not stop.is_set():
                    assert isinstance(b.rev, int)
                    assert isinstance(b.text, str)
            except Exception as exc:
                failures.append(exc)

        t = threading.Thread(target=reader)
        t.start()
        for i in range(50):
            b.insert(0, str(i % 10))
        assert b.wait_synced(10)
        stop.set()
        t.join()
    assert not failures


def test_clients_on_other_documents_are_ignored(server):
    port, _ = server
    with Client("127.0.0.1", port, "d", "alice") as a, Client("127.0.0.1", port, "e", "bob") as b:
        b.insert(0, "elsewhere")
        assert b.wait_synced()
        a.insert(0, "here")
        assert a.wait_synced()
        assert a.text == "here"
        assert a.rev == 1


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_many_clients_converge_under_random_concurrent_edits(server, seed):
    port, _ = server
    clients = [Client("127.0.0.1", port, "doc", f"user{i}") for i in range(4)]
    errors = []

    def editor(c, rng):
        try:
            for _ in range(60):
                if c.text and rng.random() < 0.4:
                    p = rng.randint(0, len(c.text) - 1)
                    c.delete(p, rng.randint(1, min(3, len(c.text) - p)))
                else:
                    p = rng.randint(0, len(c.text))
                    c.insert(p, "".join(rng.choice("abcdef") for _ in range(rng.randint(1, 3))))
                time.sleep(rng.random() * 0.002)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=editor, args=(c, random.Random(seed * 10 + i)))
               for i, c in enumerate(clients)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        wait_converged(port, "doc", clients)
    finally:
        for c in clients:
            c.close()


def test_client_keeps_working_next_to_bad_traffic(server):
    port, _ = server
    with Client("127.0.0.1", port, "d", "alice") as a:
        bad = Wire(port)
        bad.send(b"garbage\n")
        assert bad.recv()["code"] == "bad_json"
        bad.close()
        a.insert(0, "fine")
        assert a.wait_synced()
        assert a.text == "fine"
