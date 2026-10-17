# Livedoc: a real-time collaborative plain-text editing service

Build a server and a Python client library that let many people edit the same plain-text documents at the same time over a network. Every participant must end up with exactly the same text, no matter how their edits interleave.

## Deliverables

All in the current directory:

| Path | What |
|---|---|
| `server.py` | The server. Started as `python3 server.py --port PORT --data-dir DIR`. Listens on `127.0.0.1:PORT`. Keeps everything it must persist under `DIR` (create it if missing). Runs until killed. |
| `livedoc_client.py` | The client library: a class `Client` (see "Client library"). |
| `tests/` | Your pytest tests. `pytest tests/ -q` must pass. |
| `README.md` | How to run it, and a short description of the design. |

Python 3.12, **standard library only**.

## Data model

- A **document** is identified by a non-empty string name (at most 100 characters). It is created empty, at revision 0, the first time anyone joins it.
- A document's text is a Python `str`. **Positions are indices into that string** (Unicode code points, not bytes).
- The **revision** of a document is the number of operations committed to it. The text at revision `r` is the result of applying committed operations 1..r in order to the empty string.
- An **operation** is a JSON list of **edits**, applied left to right. Each edit applies to the text produced by the previous one:
  - `{"ins": P, "text": T}` inserts the non-empty string `T` before position `P` (`0 <= P <= len(text)`).
  - `{"del": P, "len": N}` deletes `N >= 1` characters starting at position `P` (`P + N <= len(text)`).
  - The empty list `[]` is a valid operation that changes nothing.

## Wire protocol

TCP. Each message is one JSON object on a single line, UTF-8 encoded and terminated by `\n`. A single connection may join any number of documents. Every message about a document carries its name in `"doc"`.

### join

Client → server: `{"type": "join", "doc": D, "client": NAME}`, optionally with `"since": R`.

- `NAME` is a non-empty string identifying the participant. The same name may connect again later, for example after a disconnect.
- Without `since`, the server replies `{"type": "joined", "doc": D, "rev": N, "text": T}`, where `N` is the current revision and `T` the current text.
- With `since` (`0 <= R <= N`), the server replies `{"type": "joined", "doc": D, "rev": N, "ops": [...]}` **instead of** `text`. `ops` lists every operation committed after revision `R`, in order. Each entry is `{"rev": r, "client": C, "id": ID, "op": OP}`, where `OP` is the operation exactly as it was committed (after transformation).
- From the moment it has joined, the connection receives broadcasts for `D`.

### op

Client → server: `{"type": "op", "doc": D, "rev": R, "id": ID, "op": OP}`.

- `OP` was written against the text at revision `R`, which may be older than the current revision `N`.
- `ID` is a string. It must be unique among all operations sent under this client name; clients should use something like a UUID.
- The server **transforms** `OP` against every operation committed after `R`, in commit order (see "Transformation"). It then applies the result and commits it as revision `N + 1`.
- The sender receives `{"type": "ack", "doc": D, "id": ID, "rev": N + 1}`.
- Every **other** connection joined to `D` receives `{"type": "op", "doc": D, "rev": N + 1, "client": NAME, "id": ID, "op": OP2}`, where `OP2` is the transformed operation.
- **Exactly once.** If an operation with the same client name and `ID` was already committed to `D`, the server must not apply it again. It replies with an `ack` carrying the **original** revision. This must still hold after the server restarts.

### Ordering guarantees

- Operations on one document are committed one at a time, in the order the server receives them.
- Each connection's messages are processed in the order they were sent.
- For each document, every connection receives broadcasts in revision order, with no gaps from the moment it joined.
- A connection receives the `ack` for revision `r` only after every broadcast for revisions below `r` that it is due to receive.

### Errors

The server replies `{"type": "error", "code": CODE, "message": "..."}`, plus `"id"` when the offending request had one. Codes:

| Code | When |
|---|---|
| `bad_json` | The line is not a JSON object. |
| `bad_request` | Unknown `type`, a missing or wrongly typed field, or a malformed operation. |
| `not_joined` | An `op` for a document this connection has not joined. |
| `bad_rev` | `rev` (or `since`) is negative or greater than the current revision. |
| `out_of_range` | The operation is not valid against the text at revision `R`: an insert or delete outside the text, `len < 1`, or empty insert text. |

After an error nothing is committed, and the connection stays open and keeps working. One misbehaving client, including one that disconnects halfway through a line, must never affect anyone else.

### Persistence

Every operation the server has acknowledged must survive the server process being killed with `SIGKILL` and restarted with the same `--data-dir`. After a restart, documents, revisions, history (for `since`) and the exactly-once guarantee are all preserved. Power-loss durability (`fsync`) is not required.

## Transformation

Transformation must produce **exactly** the result defined here, because every client and the server must agree on it character for character.

### Edit against edit

`a` is the edit being transformed and `b` is an edit that was committed **earlier**. Both were written against the same text. The transformed `a` applies after `b`, and may become zero, one or two edits. `len(t)` is the length of an inserted string.

| `a` | `b` | Result |
|---|---|---|
| ins at `p`, text `t` | ins at `q`, text `u` | `p < q`: unchanged. `p >= q`: ins at `p + len(u)`. **On a tie, the earlier-committed insert stays to the left.** |
| ins at `p`, text `t` | del `[q, q+k)` | `p <= q`: unchanged. `p >= q + k`: ins at `p - k`. Otherwise (strictly inside): ins at `q`. |
| del `[p, p+m)` | ins at `q`, text `u` | `q <= p`: del at `p + len(u)`, same length. `q >= p + m`: unchanged. Otherwise `b` landed strictly inside the range, so `a` splits and must not delete `u`: `[{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]`. |
| del `[p, p+m)` | del `[q, q+k)` | `a` deletes only the characters `b` has not already deleted, which is one contiguous range afterwards. If nothing remains, the result is no edits. |

When an earlier operation is moved past a later one (the second half of a transform, needed for multi-edit operations), the same rules apply with roles reversed. The earlier-committed side always keeps priority on insert ties: moving an earlier insert at `q` past a later insert at `p` leaves it unchanged when `q <= p` and shifts it when `q > p`.

### Operation against operation

Transforming operation `A = [a1, ..., an]` against an earlier-committed operation `B = [b1, ..., bm]` gives the same result as treating every edit as its own operation: first all of `B`'s edits were committed in order, then `A`'s edits arrive in order. Equivalently:

```
xf(A, B) -> (A', B')       # A' applies after B, B' applies after A. Both orderings give the same text.
  if A or B is empty:            return (A, B)
  if both have one edit:         return edit-vs-edit rule (both directions)
  if A has more than one edit:   (a1', B1) = xf([a1], B);   (rest', B2) = xf(A[1:], B1);   return (a1' + rest', B2)
  if B has more than one edit:   (A1, b1') = xf(A, [b1]);   (A2, rest') = xf(A1, B[1:]);   return (A2, b1' + rest')
```

The server transforms an incoming operation against each operation committed after its base revision, one after another, in commit order.

An operation whose edits all vanish is still committed. It becomes the next revision, is acknowledged and is broadcast, as `[]`.

## Client library

`livedoc_client.py` exposes:

```python
class Client:
    def __init__(self, host: str, port: int, doc: str, name: str, timeout: float = 5.0): ...
        # Connects, joins `doc` as `name`, and returns once `joined` has been received.
        # Raises an exception if that does not happen within `timeout` seconds.
    text: str            # the local text: server state plus this client's not-yet-acknowledged edits
    rev: int             # the last server revision this client has incorporated
    def insert(self, pos: int, text: str) -> None: ...
    def delete(self, pos: int, length: int) -> None: ...
        # Both apply to `self.text` immediately (optimistically), then send the edit to the server.
        # Both raise ValueError if the edit is invalid for the current local text.
    def wait_synced(self, timeout: float = 5.0) -> bool: ...
        # Blocks until every local edit has been acknowledged.
        # Returns True when synced, or False on timeout.
    def close(self) -> None: ...
```

Requirements:

- **One operation in flight.** A client never has more than one unacknowledged `op` outstanding. Edits made while one is in flight are buffered locally and sent after its `ack` arrives, as one or more operations, still one at a time. Every operation sent is based on the client's latest known revision.
- **Remote operations** can arrive while local edits are in flight or buffered. The client transforms them as defined above (the server's operation was committed earlier), so `text` is always the correct merge. Once everything is acknowledged and quiet, every client's `text` equals the server's.
- Messages are received on a background thread. `text` and `rev` must be safe to read from other threads at any time. `insert` and `delete` are called from the user's thread.
- The client ignores messages for documents other than its own.

## Tests and README

Write pytest tests covering the transformation rules, the server and the client. Tests must leave nothing behind in the project directory. They may start servers on free ports. The README explains how to run the server and the tests, and summarizes the design.
