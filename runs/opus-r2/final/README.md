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
c.set_cursor(2)          # publish where we are typing
print(c.cursors)         # {"bob": 7, ...}: others' cursors, as positions in c.text
print(c.snapshot(1))     # the document's text at revision 1
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
- `tests/test_presence.py`: the cursor movement rule. On the server: setting,
  querying and broadcasting cursors, moving stale cursors forward, cursors
  following commits, ordering with op broadcasts, every error, `leave` (only
  when a name's last connection closes, per document, even without a cursor),
  and no persistence across restarts. On the client: remote cursors moved
  through in-flight and buffered edits, through acks, remote ops and `leave`,
  and `set_cursor`. A randomized multi-client test checks that every client's
  `cursors` ends up equal to the server's.
- `tests/test_snapshot.py`: every revision of a random history, an empty
  document, every error, no document is created by a snapshot request,
  snapshots after a restart, and timing on a 5,000-revision document.

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

### Presence (cursors)

- `transform_cursor(c, op, own)` in `livedoc_client.py` implements the cursor
  movement rule. Server and client both use it.
- **On the server.** Each document holds, in memory only, `members` (client name
  → its joined connections) and `cursors` (client name → position at the
  current revision).
  - A `cursor` request is checked against the text length at its revision, then
    moved through `history[rev:]`. It counts as the owner's op wherever the
    record's client matches. The position is stored and broadcast to the
    document's other connections. That happens in the same synchronous step as
    everything else, so it is ordered with op broadcasts.
  - Every commit moves all stored cursors of the document.
  - When a connection closes, it is removed from `members`. If it was the last
    connection for its name, the cursor is dropped and `leave` is broadcast.
    Re-joining a document under a different name on the same connection counts
    as leaving the old name.
- **On the client.** `_remote_cursors` holds other clients' cursors at the
  client's server revision `rev`. They are kept exactly as the server keeps
  them: moved through remote ops (owner = the op's author) and through our own
  op when its ack arrives. `cursors` is derived from them by moving each one
  through `inflight + buffer`, our unacknowledged edits, which never belong to
  the cursor's owner.
  - That derived dict is recomputed in the same locked section as every change
    to `text`, so the two never disagree.
  - After joining, the client sends a `cursors` query to learn the cursors that
    already exist. Messages arrive in order, so the reply's revision equals the
    client's `rev` when it is processed.
  - The client's own name is never listed.
  - `set_cursor(pos)` takes a position in local `text`. The server needs a
    position at a known revision, so the cursor is sent only when the client is
    synced, when local text equals the server text at `rev`. Until then it is
    moved along with local edits (as the owner) and remote edits.

### History (snapshots)

- Every document keeps an in-memory checkpoint of its full text every 100
  revisions (`CHECKPOINT_EVERY`). `text_at(r)` starts from the nearest
  checkpoint at or below `r` and replays at most 99 ops.
- Python strings are immutable, so a checkpoint just shares the text object
  that existed at that revision. The checkpoints are rebuilt as a side effect
  of replaying the log at load, so snapshots work after a restart with nothing
  extra on disk. Any revision of a 5,000-revision document comes back in
  milliseconds.
- The trade-off is memory: one copy of the text per 100 revisions.
- `snapshot` doesn't need a join and never creates a document. A document
  exists if it is in memory or its log file exists.
- So that an empty document still exists after a restart, its log header is
  now written when the document is created (first join), not at its first
  commit.
- `Client.snapshot(rev)` sends the request with an `id`. Errors carry the `id`
  back to identify them. Successful replies are matched in request order.

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
