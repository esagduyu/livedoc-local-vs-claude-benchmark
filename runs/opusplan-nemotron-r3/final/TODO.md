# TODO

## Goal
Build Livedoc as described in SPEC.md: a stdlib-only Python 3.12 OT server (`server.py`), a client library (`livedoc_client.py`), pytest tests (`tests/`) and a `README.md`.

## Change Request Features

### 1. Live cursors (presence)

- **Set a cursor.** Client → server: `{"type": "cursor", "doc": D, "rev": R, "pos": P}`. `P` is a position in the text at revision `R` (`0 <= P <= len(text)`). The connection must have joined `D`.
  - The server moves the cursor forward to the current revision `N`, and stores it under the connection's client name, replacing any previous cursor for that name.
  - Every **other** connection joined to `D` receives `{"type": "cursor", "doc": D, "client": NAME, "rev": N, "pos": P2}`, where `P2` is a position in the text at revision `N`. Delivered in order with op broadcasts.
  - The sender gets no reply on success. On failure: `not_joined`, `bad_rev`, `out_of_range`, or `bad_request`.
- **Query cursors.** Client → server: `{"type": "cursors", "doc": D}`. Reply: `{"type": "cursors", "doc": D, "rev": N, "cursors": {NAME: POS, ...}}`, positions valid at current revision `N`. Connection must have joined `D`.
- **Keeping cursors current.** Every time an operation is committed, the server moves every stored cursor of that document through it.
- **Leaving.** When the last connection of a client name that has joined `D` closes, that name's cursor is removed. Every remaining connection receives `{"type": "leave", "doc": D, "client": NAME}`. This happens whether or not the name had set a cursor.
- Cursors are **not** persisted. They are gone after a restart.

### Moving cursors

A cursor at `c` moves through each edit of an operation in turn:

- Insert of length `k` at `p`: if `p < c`, or `p == c` **and the operation was made by the cursor's owner**, the cursor becomes `c + k`. Otherwise it stays. Your own typing pushes your cursor forward. Someone else typing exactly at your cursor does not.
- Delete of `n` characters at `p`: if `c > p`, the cursor becomes `max(p, c - n)`. A cursor inside a deleted range moves to its start.

### Client library

- `Client.cursors` is a `dict` mapping other client names to cursor positions in **this client's current `text`**. Remote cursor positions are expressed at a server revision, so the client must also move them through its own unacknowledged local edits. Those edits count as made by someone other than the cursor's owner. Remote operations move cursors the same way the server does. A `leave` message removes the entry. The client never lists its own name.
- `Client.cursors` must be safe to read from other threads, and it must never be out of step with `text`.

### 2. Document history

- Client → server: `{"type": "snapshot", "doc": D, "rev": R}`. Reply: `{"type": "snapshot", "doc": D, "rev": R, "text": T}`, where `T` is the document's text at revision `R`.
- This does **not** require joining `D`.
- Errors: `not_found` if the document does not exist, `bad_rev` if `R < 0` or `R` is greater than the current revision, `bad_request` for malformed requests.
- It must work after a restart, and stay fast for long histories. Any revision of a 5,000-revision document must come back in well under a second.

## Original TODO Tasks (1-8)

### 1. Shared transformation core (`livedoc_ot.py`)
- [x] Implement `validate_op(op, length) -> int | error`: check structure (list of dicts with exactly `ins`/`text` or `del`/`len`, int not bool, non-empty text, `len >= 1`) and bounds against a running text length; return the resulting length
- [x] Implement `apply_op(text, op) -> str`
- [x] Implement edit-vs-edit transform for all four cases (ins/ins, ins/del, del/ins, del/del), returning `(a', b')` with both directions, earlier-committed side winning insert ties
- [x] Implement recursive `xf(A, B) -> (A', B')` exactly as in the spec pseudocode
- [x] Implement `transform_against_history(op, ops)` (fold `xf` over committed ops in order, keep only `A'`)

### 2. Unit tests for transformation (`tests/test_ot.py`)
- [x] One test per row of the edit-vs-edit table, including tie cases and boundary positions (`p == q`, `p == q + k`)
- [x] del/del overlap cases: disjoint left/right, contained, containing, partial overlaps, full cancel → `[]`
- [x] Multi-edit `xf` cases checked against hand-computed expected outputs
- [x] Randomized convergence test: `apply(apply(s, B), A') == apply(apply(s, A), B')` for random ops (seeded)
- [x] Validation tests: malformed edits → bad_request category, bounds errors → out_of_range category

### 3. Persistence layer (in `server.py`)
- [x] Map document names to safe filenames (hex of UTF-8 name) under `--data-dir`; create the dir if missing
- [x] Append-only JSONL log per document: one line `{"rev", "client", "id", "op"}` per commit, written and flushed **before** ack/broadcast
- [x] On startup, load every log: rebuild text, history, per-revision text lengths, and the `(client, id) -> rev` map; ignore a truncated trailing line (crash mid-write) and truncate the file back to the last complete line

### 4. Server networking and protocol (`server.py`)
- [x] CLI: `--port`, `--data-dir`; listen on `127.0.0.1` with `asyncio` streams and a large per-line limit
- [x] Per-connection read loop: decode line, `bad_json` for invalid JSON / non-object; handle oversized lines and abrupt disconnects without affecting others
- [x] `join` handling: validate fields, create doc lazily, reply `joined` with `text` or with `ops` since `R` (`bad_rev` if out of range), register connection for broadcasts
- [x] `op` handling: field validation (`bad_request`), `not_joined`, `bad_rev`, duplicate `(client, id)` → ack original rev, validate against length at rev `R` (`out_of_range`), transform against history, apply, persist, ack sender, broadcast to other joined connections
- [x] Errors include `id` when the request had one; nothing committed on error
- [x] Commit + persist + ack + broadcast done synchronously inside the event loop (no `await` in between) so per-doc ordering and "ack after earlier broadcasts" hold; writes go to each connection's transport in order
- [x] Clean up connection registrations on disconnect

### 5. Server tests (`tests/test_server.py`, `tests/conftest.py`)
- [x] Fixture: start `server.py` as a subprocess on a free port with a `tmp_path` data dir; wait until it accepts connections; kill on teardown
- [x] Join (fresh doc, existing doc, `since`), op ack + broadcast, concurrent ops from stale revisions transform correctly
- [x] Every error code, plus "connection keeps working after an error"
- [x] Exactly-once: resend same id → original rev, no new commit; still true after restart
- [x] SIGKILL + restart preserves text, rev and `since` history
- [x] Client disconnecting halfway through a line doesn't disturb other clients
- [x] Ordering: broadcasts arrive in revision order with no gaps; ack arrives after earlier broadcasts

### 6. Client library (`livedoc_client.py`)
- [x] `Client.__init__`: connect, send `join`, start background reader thread, wait for `joined` (raise `TimeoutError`/`ConnectionError` on failure)
- [x] State under a lock: `text`, `rev`, `inflight` op (+ its id), `buffer` op; `text`/`rev` exposed as thread-safe properties
- [x] `insert`/`delete`: validate against local text (`ValueError`), apply locally, append to buffer, send if nothing in flight
- [x] On `ack`: set `rev`, clear inflight, send buffer as next op based on new `rev`; signal sync condition when nothing pending
- [x] On remote `op`: `xf(inflight, remote)` then `xf(buffer, remote')`, apply the final remote op to `text`, set `rev`
- [x] Ignore messages for other docs; `wait_synced(timeout)` via `threading.Condition`; `close()` shuts the socket and joins the thread

### 7. Client tests (`tests/test_client.py`)
- [x] Single client edits sync and match server text (verified via a fresh join)
- [x] Several clients making concurrent random edits in threads all converge to the same text as the server
- [x] Only one op in flight (inspect via a fake/raw server or by observing server-side messages)
- [x] `ValueError` on invalid edits; join timeout raises; messages for other docs ignored

### 8. Documentation and final check
- [ ] Write `README.md`: how to run the server, the client, and the tests; design summary (OT rules, server event loop, persistence format, client state machine)
- [ ] Run `pytest tests/ -q`; confirm all pass and nothing is left in the project dir (no data dirs, `__pycache__` aside — use `-p no:cacheprovider` guidance or document)