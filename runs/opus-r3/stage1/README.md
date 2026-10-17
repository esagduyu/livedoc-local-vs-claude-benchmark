# Livedoc

A real-time collaborative plain-text editing service: a TCP server plus a Python
client library. Python 3.12, standard library only. The full specification is in
`SPEC.md`.

## Running

```sh
python3 server.py --port 7000 --data-dir ./data
```

The server listens on `127.0.0.1:PORT` and runs until killed. It keeps all state
in `DIR/ops.jsonl` and creates `DIR` if it is missing.

```python
from livedoc_client import Client

c = Client("127.0.0.1", 7000, "notes", "alice")
c.insert(0, "hello")
c.delete(0, 1)
c.wait_synced()
print(c.text, c.rev)
c.close()
```

## Tests

```sh
pytest tests/ -q
```

The tests cover the transformation rules (including a randomized check against
the spec's recursive definition and a convergence check), the wire protocol and
error codes, persistence across `SIGKILL`, and the client, which is tested
against a real server and a scripted fake server. Each test starts servers on free
ports with data directories under pytest's temp dir, so nothing is left in the
project. `pytest.ini` turns off the pytest cache for the same reason.

## Design

**Transformation (`livedoc_client.py`).** `transform_edit(a, b)` implements the
edit-vs-edit table in both directions, and `transform(A, B)` implements the
operation-level `xf` from the spec. The tail recursion over `A` and over `B` is
unrolled into loops, so long operations don't hit Python's recursion limit. The
result is still identical to the recursive definition, and a test checks this.
The server imports the same code, so client and server can't disagree.
`check_op` validates an operation against a text length and classifies failures
as `bad_request` (wrong shape) or `out_of_range` (doesn't fit the text).

**Server (`server.py`).** The server is a single-threaded `asyncio` protocol
server. Each request is handled to completion with no `await` in between, so for
each document the steps "transform, persist, apply, broadcast, ack" happen
atomically and in arrival order. Broadcasts and acks are written to each
connection's transport in commit order. That gives the ordering guarantees: no
gaps, revision order, and acks only after earlier broadcasts. A joining
connection gets its `joined` reply and is subscribed in the same step, so it
can't miss a revision.

For each document the server keeps in memory the current text, the full
committed history (for `since` and for transforming stale operations), the text
length at every revision (so an op based on revision `R` can be bounds-checked
without rebuilding old text), and a `(client, id) -> rev` map for exactly-once
delivery. A duplicate gets an `ack` with the original revision and nothing else.

Requests are checked in this order: malformed fields (`bad_request`), then
`not_joined`, then duplicate detection, then `bad_rev`, then `out_of_range`.

**Persistence.** Every commit is appended to `ops.jsonl` as one JSON line with
a single `os.write` (`doc`, `rev`, `client`, `id`, transformed `op`). This
happens before the commit is applied in memory, broadcast or acked. The file
survives `SIGKILL` because the data is already in the kernel; it is not
`fsync`ed. On startup the log is replayed to rebuild every document, its history
and the exactly-once map. A torn final line, left by a crash mid-write, is
truncated away. Documents with no operations need no storage, because a new
document is empty at revision 0.

**Isolation.** The server buffers partial lines per connection, so a client that
disconnects mid-line affects nobody else. A line longer than 16 MiB gets a
`bad_request` error and is skipped up to the next newline. A connection whose
unread output exceeds 64 MiB (a client that never reads) is dropped instead of
using up memory. Errors never commit anything and never close the connection.

**Client.** This is the standard OT client state machine. It keeps at most one
operation in flight. Edits made meanwhile are applied to `text` right away and
collected in a buffer, which is sent as one operation (based on the latest
revision) when the ack arrives. An incoming remote operation was committed
before our pending edits. The client transforms it past the in-flight operation
and then past the buffer, which also rebases both of those, and applies the
result. A background thread reads from the socket. A condition variable guards
the state and backs `wait_synced`. Messages for other documents are ignored.
