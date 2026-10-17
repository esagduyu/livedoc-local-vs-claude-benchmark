# Livedoc

A server and a Python client library for real-time collaborative editing of
plain-text documents. Many participants edit the same document over TCP, and
all of them end up with the same text. Standard library only, Python 3.12.

## Running

Start the server. It listens on `127.0.0.1` and keeps its data under the data
directory, which it creates if missing:

    python3 server.py --port 7000 --data-dir ./livedoc-data

Use the client library from Python:

    from livedoc_client import Client

    with Client("127.0.0.1", 7000, "notes", "alice") as c:
        c.insert(0, "hello")
        c.delete(0, 1)
        c.wait_synced()
        print(c.text, c.rev)

Run the tests. They start their own servers on free ports and keep all data in
pytest temporary directories:

    pytest tests/ -q

## Design

### Files

- `server.py` holds the server. `Service` owns the documents and the protocol
  rules, `Handler` reads the lines of each connection, and `Connection` has a
  writer thread for each connection.
- `livedoc_client.py` holds the `Client` class and the transformation rules. The
  server imports the rules from here, so both sides run the same code.

### Transformation

Operations are lists of edits, and the edit rules follow the spec. `transform(a, b)`
returns `(a2, b2)`, where `a2` applies after `b` and `b2` applies after `a`. Single
edits are handled by `_xf_edit`. Multi-edit operations are handled by splitting
them, in the same way the spec describes. On an insert tie, the earlier-committed
insert stays to the left. A random test checks that both orders converge for
3000 seeded cases.

### Server

- **Commit.** Under one lock the server validates the operation against the
  text at its base revision. It then transforms the operation against each later
  committed operation in order, appends the result to the journal, and advances
  the revision.
- **Ordering.** Acks and broadcasts for a document are queued under that same
  lock. Each connection has a writer thread that drains a queue, so a connection
  always sees broadcasts in revision order and always gets its ack after the
  broadcasts below it. A slow reader only grows its own queue and never blocks
  the others.
- **History.** Committed operations are kept in memory. The text at revision `r`
  is computed from a snapshot taken every 64 revisions. That is how
  `out_of_range` is checked against the base revision, and how `since` replies
  are built.
- **Persistence.** Each commit is appended to `DIR/journal.jsonl` and flushed
  before the ack is sent. A `SIGKILL` therefore cannot lose an acknowledged
  operation. On start-up the journal is replayed. This restores documents,
  revisions, history, and the exactly-once table. An incomplete last line, which
  was never acknowledged, is truncated.
- **Exactly once.** The key is `(document, client name, id)`. A repeated
  operation gets an ack with its original revision and is not committed again.
- **Errors.** A malformed line, a bad request, or a bad operation gets an error
  reply on that connection only. Nothing is committed and the connection stays
  open. A line that is not valid UTF-8 or JSON gets `bad_json`. A connection that
  drops mid-line has its partial data discarded.

### Client

- **One operation in flight.** `insert` and `delete` apply to the local text at
  once. The first edit is sent as an operation right away. Edits made while it
  is in flight are buffered and sent together, as one operation, after its ack.
- **Remote operations.** A remote operation is committed before anything the
  client has pending, so the client transforms its in-flight operation and its
  buffer against it, in that order. The transformed remote operation is then
  applied to the local text.
- **Threads.** A background thread reads messages. `text`, `rev`, and the other
  state are guarded by one lock, and `wait_synced` waits on a condition variable.

### Limitations

- Power-loss durability is not provided (no `fsync`), as the spec allows.
- A client does not reconnect. If the connection drops, the `Client` is closed,
  and a new one has to be created.
- Every document's history and snapshots stay in memory. Large documents with
  long histories use correspondingly more memory.
