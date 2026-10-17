# Change request: live cursors and document history

Livedoc is in use, and two features are needed. Extend the existing server, client library, tests and README. Everything in `SPEC.md` still applies and must keep working.

## 1. Live cursors (presence)

People want to see where others are typing.

### Protocol

- **Set a cursor.** Client → server: `{"type": "cursor", "doc": D, "rev": R, "pos": P}`. `P` is a position in the text at revision `R` (`0 <= P <= len(text)`). The connection must have joined `D`.
  - The server moves the cursor forward to the current revision `N` (see "Moving cursors"), and stores it under the connection's client name, replacing any previous cursor for that name.
  - Every **other** connection joined to `D` receives `{"type": "cursor", "doc": D, "client": NAME, "rev": N, "pos": P2}`, where `P2` is a position in the text at revision `N`. Like everything else, this message is delivered in order with the op broadcasts.
  - The sender gets no reply on success. On failure it gets an error: `not_joined`, `bad_rev`, `out_of_range` (position outside the text at `R`), or `bad_request`.
- **Query cursors.** Client → server: `{"type": "cursors", "doc": D}`. Reply: `{"type": "cursors", "doc": D, "rev": N, "cursors": {NAME: POS, ...}}`, with positions valid at the current revision `N`. The connection must have joined `D`.
- **Keeping cursors current.** Every time an operation is committed, the server moves every stored cursor of that document through it.
- **Leaving.** When the last connection of a client name that has joined `D` closes, that name's cursor is removed. Every remaining connection joined to `D` receives `{"type": "leave", "doc": D, "client": NAME}`. This happens whether or not the name had set a cursor.
- Cursors are **not** persisted. They are gone after a restart.

### Moving cursors

A cursor at `c` moves through each edit of an operation in turn:

- Insert of length `k` at `p`: if `p < c`, or `p == c` **and the operation was made by the cursor's owner**, the cursor becomes `c + k`. Otherwise it stays. Your own typing pushes your cursor forward. Someone else typing exactly at your cursor does not.
- Delete of `n` characters at `p`: if `c > p`, the cursor becomes `max(p, c - n)`. A cursor inside a deleted range moves to its start.

### Client library

- `Client.cursors` is a `dict` mapping other client names to cursor positions in **this client's current `text`**. Remote cursor positions are expressed at a server revision, so the client must also move them through its own unacknowledged local edits. Those edits count as made by someone other than the cursor's owner. Remote operations move cursors the same way the server does. A `leave` message removes the entry. The client never lists its own name.
- `Client.cursors` must be safe to read from other threads, and it must never be out of step with `text`.

## 2. Document history

People want to see an old version of a document.

- Client → server: `{"type": "snapshot", "doc": D, "rev": R}`. Reply: `{"type": "snapshot", "doc": D, "rev": R, "text": T}`, where `T` is the document's text at revision `R`.
- This does **not** require joining `D`.
- Errors: `not_found` if the document does not exist, `bad_rev` if `R < 0` or `R` is greater than the current revision, `bad_request` for malformed requests.
- It must work after a restart, and stay fast for long histories. Any revision of a 5,000-revision document must come back in well under a second.

Update the README and add tests for both features. `pytest tests/ -q` must pass.
