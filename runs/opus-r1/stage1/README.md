# Livedoc

A real-time collaborative plain-text editing service: a TCP server and a Python client
library. Many participants edit the same documents at once, and everyone ends up with the
same text. Python 3.12, standard library only.

## Running

```sh
python3 server.py --port 9000 --data-dir ./data      # listens on 127.0.0.1:9000 until killed
```

```python
from livedoc_client import Client

c = Client("127.0.0.1", 9000, "notes", "alice")
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

The tests start real servers on free ports with data directories under pytest's temporary
directory, and leave nothing in the project directory (bytecode and the pytest cache are
disabled or cleaned up).

- `tests/test_transform.py`: every row of the transformation table, insert-tie priority,
  random convergence checks, and agreement with a literal copy of the spec's recursive
  pseudocode on thousands of random operation pairs.
- `tests/test_server.py`: join and `since`, ack and broadcast, server-side transformation,
  empty (vanished) operations, exactly-once delivery, every error code, malformed or truncated
  input from one client not affecting others, ordering of broadcasts and acks under concurrent
  writers, and persistence across `SIGKILL`, including a torn final log record.
- `tests/test_client.py`: basic editing, `ValueError` on invalid edits, join timeout, a
  scripted fake server that checks the one-operation-in-flight rule and the exact transformed
  text and ops, and a multi-threaded random fuzz test where all clients must converge.

## Design

**Transformation** (`livedoc_client.py`, shared by the server). `transform_edit(x, y, x_first)`
implements the edit-against-edit table; `x_first` says which side was committed earlier and so
wins insert ties. `xf(A, B)` is the spec's operation-against-operation recursion, with its tail
recursions written as loops so long operations cannot exhaust the stack. A test checks that it
gives exactly the same results as the literal recursive definition.

**Server** (`server.py`). A single-threaded `asyncio` event loop. Because one thread handles
everything, operations on a document are committed one at a time in arrival order without
locks. For each document the server keeps the current text, the committed history, the text
length at every revision, and a map from `(client name, op id)` to revision.

- *Op handling:* check the structure, then answer duplicates with the original revision. Then
  check `rev`, and validate the op against the text length at revision `rev`. Validity only
  depends on lengths, so old texts never need rebuilding. Then transform the op against each
  later committed op, append it to the log, apply it, broadcast it to the other subscribers,
  and finally ack the sender.
- *Ordering:* every outgoing message goes into the connection's own ordered write buffer, and
  broadcasts are queued as each revision commits. A connection therefore sees revisions in
  order, without gaps, and its ack only after every earlier broadcast.
- *Isolation:* each connection buffers its own input and only handles complete lines. Errors
  are answered and nothing is committed. A partial line at disconnect is discarded. A line
  over 64 MiB is rejected. A connection that stops reading and lets 256 MiB of output pile up
  is dropped, so it cannot exhaust memory. Broadcasts never wait for a slow reader.
- *Persistence:* one append-only log, `DIR/oplog.jsonl`, with one JSON record per committed op:
  `{doc, rev, client, id, op}`. Each record is written to the unbuffered file *before* the op
  is applied or acknowledged, so anything acknowledged is in the kernel and survives `SIGKILL`.
  There is no `fsync`, as allowed. On startup the log is replayed to rebuild texts, history and
  the exactly-once map. A torn final record (a write cut short by the kill, so never
  acknowledged) is truncated away. Empty documents need no record: a document that has never
  been seen is created empty at revision 0 anyway.
- JSON is written with ASCII escapes, so any Python string, even one with lone surrogates,
  round-trips safely.

**Client** (`Client`). A background thread reads messages. All state sits behind one lock,
and `text` and `rev` are properties that read under it. The state is: the confirmed revision,
the in-flight op (at most one), and a buffer of edits made since it was sent. Local edits are
validated, applied to `text` at once, and added to the buffer. The buffer is sent as one op
whenever nothing is in flight, so each op is based on the latest known revision. A remote op
`R` was committed before our in-flight op `I` and buffer `L`:
`(I', R1) = xf(I, R)`, then `(L', R2) = xf(L, R1)`, then `R2` is applied to `text`. This is
the same transform the server applies to `I`, so the client ends up equal to the server. An
ack clears the in-flight op, advances `rev`, and sends the buffer. Messages for other
documents are ignored.
