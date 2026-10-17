# Livedoc

A server and a Python client library for real-time collaborative editing of
plain-text documents. Many participants can edit the same document at once,
and every participant ends up with the same text however their edits interleave.
Standard library only, Python 3.12.

## Running

Start the server. It listens on `127.0.0.1:PORT` and keeps its state in `DIR`
(created if missing) until killed:

    python3 server.py --port 9000 --data-dir ./livedoc-data

Use the client from Python. Keep `livedoc_client.py` next to `livedoc_ot.py`,
since the client imports the shared transform rules from it:

    from livedoc_client import Client

    with Client("127.0.0.1", 9000, "notes", "alice") as c:
        c.insert(0, "hello")
        c.delete(0, 1)
        c.wait_synced()     # block until the server has acknowledged every edit
        c.set_cursor(0)     # publish our cursor for other participants
        print(c.text, c.rev, c.cursors)   # c.cursors: other participants' cursors

Ask for an earlier revision of a document, without joining it:

    {"type": "snapshot", "doc": "notes", "rev": 3}
    -> {"type": "snapshot", "doc": "notes", "rev": 3, "text": "..."}

Run the tests (they use temporary directories and free ports, and write nothing
into the project directory):

    pytest tests/ -q

## Files

| File | Contents |
|---|---|
| `server.py` | The server: connections, commit path, durable log. |
| `livedoc_ot.py` | Operational transformation rules, shared by server and client. |
| `livedoc_client.py` | The `Client` class. |
| `tests/` | pytest suite: transform rules, server protocol and persistence, cursors, history, client behaviour. |

## Design

**Operations.** A document is a string. An operation is a list of
insert/delete edits applied left to right. The server keeps every committed
operation, so the text at any revision is the result of applying operations
1..r in order.

**Transformation.** Concurrent operations are reconciled with operational
transformation. `livedoc_ot.py` implements the rules exactly as the spec defines
them: single-edit rules for every insert/delete pair (with the earlier-committed
insert keeping the left position on ties), a split when a delete straddles an
insert, and collapse-to-remainder when two deletes overlap. Multi-edit
operations are transformed edit by edit using the recursive `xf` function.
Server and client call the same functions, so they agree character for character.
The tests check each table row and a randomized property: both orders of two
concurrent operations give the same text (TP1).

**Server.** Each connection has a reader thread, which handles its requests in
order, and a writer thread, which drains a queue of outgoing messages. Commits
run under one lock. A commit appends the operation to the log and flushes it, then
enqueues the broadcasts and the ack. Because enqueueing never blocks, a slow or
stalled client cannot delay anyone else. Queue order is commit order, so a
connection receives broadcasts in revision order and an ack only after all earlier
broadcasts.

**Persistence.** The server appends each committed operation as one JSON line to
`DIR/livedoc.log`. On startup it replays the log to rebuild texts, revisions,
history and the exactly-once table. A final line cut off by a crash was never
acknowledged, so it is discarded. The data survives SIGKILL because the write has
reached the OS before the ack is sent. Power-loss durability (`fsync`) is not
provided.

**Live cursors.** A connection publishes a cursor with
`{"type": "cursor", "doc", "rev", "pos"}`. The server stores it under the
connection's client name and moves it forward to the current revision. It
then sends the position to every other connection joined to the document.
Stored cursors live only in memory and are gone after a restart.

Moving a cursor through an edit follows one rule set, in `livedoc_ot.py`
(`move_cursor`). An insert at or before the cursor pushes it forward. The
exception is an insert exactly at the cursor made by someone else, which
leaves it in place. A delete before the cursor moves it back. A cursor inside a
deleted range moves to the start of that range. The server applies these rules
to every committed operation, and the client applies them to remote operations
and to its own pending edits, which count as edits by someone else. Because
both sides run the same function, the positions agree.

When the last connection of a client name closes (or renames itself away from
the document), the server drops that name's cursor and sends `leave` to the
remaining connections. `cursors` returns the current cursors of a document.

**History.** `{"type": "snapshot", "doc", "rev"}` returns the text at any
revision. No join is needed, and an unknown document gives `not_found`. The
server keeps the full text every 128 revisions in memory, and rebuilds those
checkpoints from the log on startup. A snapshot therefore replays at most 127
operations, so any revision of a 5,000-revision document returns in
milliseconds. The tests check this after a restart.

**Client cursors.** `Client.cursors` maps the other participants to positions in
the current local text. The client stores each remote cursor in server
coordinates, at the revision it has incorporated. Pending local edits are moved
over it when the property is read, so the positions always fit `text`.
`Client.set_cursor(pos)` publishes our cursor. While local edits are pending, it
converts `pos` back to server coordinates. The conversion is approximate only
for positions inside text that is still pending, and it is exact once synced.

**Exactly once.** The server records `(client name, id)` for each committed
operation. A repeated operation is acknowledged with its original revision and
is not applied or broadcast again. The table is rebuilt from the log, so this
holds across restarts.

**Client.** The client keeps three things: the server state at its last known
revision, one in-flight operation (sent, not yet acknowledged), and a buffer of
local edits made since. `text` is always server state plus in-flight plus
buffer. When a remote operation arrives, it is transformed against the in-flight
operation and then the buffer, and the results are applied to `text`. On
acknowledgement, the in-flight operation becomes the next revision, and the
buffer, if any, is sent as the next operation. Only one operation is outstanding
at a time, and every operation is based on the latest known revision.

**Limits.** The server never disconnects a client for being slow. A client that
stops reading has its messages queued in memory until it reads again or its
socket fails. There is no
reconnect logic in the client: a dropped connection ends the `Client`, and a new
one must be created. The client never resends an in-flight operation. If the
server ever rejects one, `Client.error` is set and `wait_synced()` returns
`False`.
