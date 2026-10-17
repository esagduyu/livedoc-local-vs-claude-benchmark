# Livedoc

A real-time collaborative plain-text editing service: a TCP server and a Python
client library. Many participants can edit the same document at once. Every
participant ends up with the same text, however their edits interleave.
Python 3.12, standard library only.

## Running

```sh
python3 server.py --port 9000 --data-dir ./data
```

The server listens on `127.0.0.1:PORT`, creates `--data-dir` if it is missing,
and runs until killed.

```python
from livedoc_client import Client

c = Client("127.0.0.1", 9000, doc="notes", name="alice")
c.insert(0, "hello")
c.delete(0, 1)
c.wait_synced()          # True once the server has acknowledged every local edit
print(c.text, c.rev)
c.close()
```

## Tests

```sh
pytest tests/ -q
```

The tests start real server processes on free ports with data directories under
pytest's temporary directory. They leave nothing in the project directory:
`pytest.ini` turns off `.pytest_cache`, and `tests/conftest.py` turns off
bytecode writing and removes its own `__pycache__`.

- `tests/test_transform.py`: each edit-vs-edit rule, insert-tie priority in both
  directions, multi-edit operations. Randomized checks compare the
  implementation with a literal transcription of the spec's recursive `xf`, and
  check convergence (`B·A' == A·B'`).
- `tests/test_server.py`: join, `since` history, ack and broadcast, server-side
  transformation, ops whose edits all vanish, exactly-once (including after a
  restart), persistence across `SIGKILL` (including a kill while ops are still
  streaming in, and a torn log tail), every error code, partial lines and
  disconnects, ordering guarantees, and slow readers.
- `tests/test_client.py`: the client against the real server, including a
  randomized multi-threaded test with several clients. A scripted fake server
  checks one-op-in-flight buffering, transformation of in-flight and buffered
  edits, ignoring other documents, and timeouts.

## Design

### Transformation (`livedoc_client.py`)

The spec's transformation is implemented once, in `livedoc_client.py`.
`server.py` imports it from there, so client and server always agree.

- `transform_edit(x, y, x_wins_tie)` moves one edit past another and returns
  0, 1 or 2 edits. The tie flag is the only difference between the two
  directions: the earlier-committed side keeps priority.
- `transform(A, B)` is the spec's `xf`. The tail recursion over multi-edit
  operations is written as loops, so long operations cannot hit Python's
  recursion limit. A randomized test checks that it matches the literal
  recursive definition.

### Server (`server.py`)

- **Concurrency.** The server is a single-threaded `asyncio` server. Each request
  is handled synchronously on the event loop: parse, validate, transform, write
  to the log, queue the broadcasts, queue the ack. So commits on a document
  happen one at a time in arrival order, and every message to a connection is
  queued in revision order. Because broadcasts are queued before the sender's
  ack, a connection always gets the broadcasts for revisions below `r` before
  the ack for `r`. Joining subscribes the connection and queues the `joined`
  reply in the same step, so the connection misses no revisions.
- **Isolation.** Each connection buffers its own partial line, so a client that
  disconnects mid-line affects no one else. Lines over 16 MiB are rejected and
  skipped. A connection whose unread output grows past 64 MiB is dropped, so a
  client that stops reading cannot stall other clients or exhaust memory. Any
  unexpected exception while handling a request becomes an error reply and does
  not crash the server.
- **Validation.** An operation is checked against the text length at its base
  revision. The server keeps a per-revision length table, so it never needs old
  text snapshots. It then transforms the operation against
  `history[rev:]` in order, and commits the result even if it is now `[]`.
  Checks run in this order: shape (`bad_request`), `not_joined`, the
  exactly-once lookup, `bad_rev`, `out_of_range`. So a retried op that was
  already committed is re-acknowledged even if its `rev` is now stale.
- **Persistence.** Each document has an append-only JSON-lines log under the
  data directory. The file name is the SHA-256 of the document name, so any
  name is safe on disk. The first line is a header `{"doc": NAME}`. Each
  following line is a committed record `{"rev", "client", "id", "op"}`. These
  are exactly the entries returned by `join` with `since`. A record is written
  (no `fsync`) before the ack is sent. A process kill can't lose data that
  `write()` has handed to the kernel, so every acknowledged op survives
  `SIGKILL`. A document is loaded lazily on first join by replaying its log,
  which rebuilds the text, the history, the length table and the
  `(client, id) -> rev` map used for exactly-once. If the last line is torn,
  it was never acknowledged, so it is truncated away.

### Client (`livedoc_client.py`)

The client keeps four pieces of state, all guarded by one lock and condition
variable:

- `rev`: the last server revision it has incorporated.
- `text`: the server text plus local edits.
- `inflight`: the single op that has been sent but not yet acknowledged.
- `buffer`: edits made since `inflight` was sent.

- A local edit is checked against `text` (a bad edit raises `ValueError`) and
  applied to `text` right away. It is then added to `buffer`. If nothing is in
  flight, the buffer is sent as one op based on `rev`.
- A remote op `O` was committed before our pending work, so the client computes
  `(inflight', O1) = xf(inflight, O)`, then `(buffer', O2) = xf(buffer, O1)`.
  It applies `O2` to `text` and sets `rev`. This is the same sequence of
  transforms the server applies to `inflight`, so both sides converge.
- An ack clears `inflight`, sets `rev` to the acked revision, and sends the
  buffer, if there is one, as the next op.
- A background thread reads messages and ignores any for other documents. A
  separate writer thread sends them, so a full socket buffer never blocks a
  thread that holds the state lock. `text` and `rev` are properties that read
  under the lock.
