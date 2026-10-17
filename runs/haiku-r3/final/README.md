# Livedoc

A real-time collaborative plain-text editing service: a TCP server and a Python
client library. Every participant ends up with the same text, however their
edits interleave. Python 3.12, standard library only.

## Running

Start the server. It listens on `127.0.0.1:PORT` and runs until killed.

```sh
python3 server.py --port 9000 --data-dir ./data
```

`DIR` is created if missing. Everything the server must remember lives there.

Use the client:

```python
from livedoc_client import Client

c = Client("127.0.0.1", 9000, "notes", "alice")
c.insert(0, "hello")
c.delete(0, 1)
c.wait_synced()
c.set_cursor(len(c.text))  # publish our cursor (needs everything acknowledged)
print(c.text, c.rev, c.cursors)  # c.cursors: other people's cursors, in c.text
c.close()
```

Old versions can be read without joining, through the wire protocol's
`snapshot` message (see below).

Run the tests from the project directory:

```sh
pytest tests/ -q
```

The tests start their own servers on free ports, keep their data in pytest
`tmp_path` directories, and leave nothing in the project directory.

## Files

| File | Purpose |
|---|---|
| `server.py` | The server: protocol handling, exactly-once commits, persistence. |
| `livedoc_client.py` | The `Client` class. |
| `livedoc_ot.py` | The transformation rules, shared by server and client. |
| `tests/` | pytest suite: transform rules, wire protocol, client behaviour. |

`livedoc_ot.py` is a separate module on purpose. The server and the client must
compute transforms identically, character for character, so both import the same
code rather than keeping two copies.

## Design

### Server

- **Concurrency model.** One asyncio event loop. Each connection is read in
  order and handled to completion before its next line is read. Committing an
  operation (validate, transform, log, update state, broadcast, ack) is
  synchronous, so commits on all documents are naturally serialized in arrival
  order.
- **Per-document state.** The current text, `lengths[r]` (the text length at
  each revision, which is all that is needed to validate an operation against
  its base revision), the history `(client, id, edits)` for `since` replies, a
  map `(client, id) -> revision` for exactly-once, and the set of subscribed
  connections.
- **Commit.** An incoming operation based at revision `R` is validated against
  the length at `R`, then transformed against each committed operation after
  `R` in order. The result is appended to the log, applied, broadcast to the
  other subscribers, and acknowledged to the sender.
- **Ordering.** Broadcasts and acks are written to each connection's transport
  synchronously in commit order. A connection therefore sees broadcasts in
  revision order, and its ack for revision `r` always follows the broadcasts
  below `r`.
- **Exactly-once.** Keyed by `(document, client name, id)`. A repeated operation
  gets an ack with the original revision and is not applied again. The map is
  rebuilt from the log on start-up, so this survives restarts.
- **Persistence.** One append-only file, `DIR/oplog.jsonl`. Each committed
  operation is one JSON line, written with a single `write` before the ack is
  sent. Creating a document appends a `{"doc": D, "created": true}` line, so a
  document that was joined but never edited still exists after a restart. A SIGKILL cannot lose an acknowledged operation, because the kernel
  already has the bytes. (No `fsync`, as the spec allows.) On start-up the log
  is replayed. A final line with no newline is a record cut short by a crash.
  It was never acknowledged, so it is truncated away. An exclusive `flock` on the
  log stops two servers from sharing one data directory.
- **Snapshots.** The text is kept every 128 revisions (`CHECKPOINT_EVERY`).
  A snapshot starts from the nearest checkpoint and applies at most 127
  operations. The checkpoints are rebuilt during replay, so they are not
  written to the log.
- **Cursors.** Stored per document, keyed by client name, in memory only.
  Each commit moves every stored cursor through the operation, with the
  owner rule from the spec. A cursor is removed, and `leave` is broadcast,
  when the last connection using that name on the document goes away
  (closes, or rejoins under another name).
- **Robustness.** Each connection has its own read buffer. Partial lines are
  discarded when the connection closes. A line longer than 8 MiB is rejected with
  `bad_json`, and its remainder is skipped without closing the connection.
  Unexpected exceptions are logged to stderr and do not stop the server.

### Transformation

An edit is `("ins", pos, text)` or `("del", pos, length)`. Edit-against-edit
follows the table in the spec. Where a `delete` is split by an insert, it
becomes two edits. Insert ties are resolved in favour of the earlier-committed
insert, and the tie rule is applied on whichever side is the earlier one.

Operation-against-operation uses the recursive decomposition from the spec.
Operations that vanish become `[]` and are still committed.

`tests/test_ot.py` checks each table row, and checks convergence on 2000 seeded
random pairs of multi-edit operations. The rules were also checked on 20000
random pairs during development.

### Client

- The client keeps three pieces of state: `_server_text` (the text at `rev`),
  `_inflight` (the one unacknowledged operation, against `_server_text`), and
  `_buffer` (local edits made while `_inflight` is outstanding, against
  `_server_text + _inflight`). `text` is always
  `_server_text + _inflight + _buffer`.
- A local edit goes into `_inflight` if nothing is in flight, and otherwise into
  `_buffer`. When the ack arrives, `_inflight` is folded into `_server_text`, and
  the buffer, if any, becomes the next in-flight operation.
- A remote operation `B` is applied to `_server_text`. Then
  `(_inflight, B1) = xf(_inflight, B)` and `(_buffer, _) = xf(_buffer, B1)`.
- **Cursors.** Other clients' cursors are kept in the same coordinates as
  `_server_text`, so they move exactly as the server moves them. `cursors`
  maps them into `text` on each read by moving them through `_inflight` and
  then `_buffer`, as non-owner edits. When our own op is acknowledged, the
  cursors also move through it, since we never receive our own op as a
  broadcast. The client never lists its own name.
- `set_cursor(pos)` sends our cursor at `rev`. The protocol takes positions
  at a revision, and while edits are pending `text` is not at any revision,
  so it raises `RuntimeError` until `wait_synced()` returns.
- A reader thread processes messages under a lock. `text`, `rev` and the
  public methods all take that lock, so they are safe to call from any thread.
- If the server rejects an in-flight operation, or sends something out of
  sequence, the client stops synchronising. `wait_synced` then returns `False`,
  and further edits raise `RuntimeError`. A correct server never does this.

## Protocol additions

Besides `join` and `op` (see `SPEC.md`):

| Client → server | Reply |
|---|---|
| `{"type":"cursor","doc":D,"rev":R,"pos":P}` (must have joined D) | none on success. Others get `{"type":"cursor","doc":D,"client":NAME,"rev":N,"pos":P2}`. Errors: `not_joined`, `bad_rev`, `out_of_range`, `bad_request`. |
| `{"type":"cursors","doc":D}` (must have joined D) | `{"type":"cursors","doc":D,"rev":N,"cursors":{NAME: POS, ...}}` |
| `{"type":"snapshot","doc":D,"rev":R}` (no join needed) | `{"type":"snapshot","doc":D,"rev":R,"text":T}`. Errors: `not_found`, `bad_rev`, `bad_request`. |

Joined connections also receive `{"type":"leave","doc":D,"client":NAME}` when
a name's last connection on D goes away. Cursors are not persisted.

## Tests

- `test_cursor_rules.py`: the cursor moving rule, including random checks
  that a cursor stays inside the text.
- `test_cursor_protocol.py`: broadcasts, owner and non-owner typing, deletions,
  stale revisions, errors, `leave`, and that cursors are not persisted.
- `test_snapshot.py`: every revision of a 300-revision document, restart
  survival, and a 5,000-revision document (timed, well under 0.5 s per read).
- `test_client_cursors.py`: the client's cursor view, against a scripted server
  and a real one, including a randomized run that checks all clients agree with
  the server.

## Limits

- A document's entire history is held in memory, and the log is never
  compacted, so the log grows with the total number of operations.
- The checkpoints add memory: one copy of the document text per 128
  revisions. A large document with a long history uses roughly
  `(revisions / 128) × text size` for them.
- Every `ins` text is limited to valid UTF-8 (lone surrogates are rejected).
- The server has no authentication. It binds to `127.0.0.1` only.
