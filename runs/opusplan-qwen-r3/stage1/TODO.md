# TODO

## Goal
Build Livedoc as described in SPEC.md: a stdlib-only Python 3.12 OT server (`server.py`), a client library (`livedoc_client.py`), pytest tests (`tests/`) and a `README.md`.

## Tasks

### 1. Shared transformation core (`livedoc_ot.py`)
- [ ] Implement `validate_op(op, length) -> int | error`: check structure (list of dicts with exactly `ins`/`text` or `del`/`len`, int not bool, non-empty text, `len >= 1`) and bounds against a running text length; return the resulting length
- [ ] Implement `apply_op(text, op) -> str`
- [ ] Implement edit-vs-edit transform for all four cases (ins/ins, ins/del, del/ins, del/del), returning `(a', b')` with both directions, earlier-committed side winning insert ties
- [ ] Implement recursive `xf(A, B) -> (A', B')` exactly as in the spec pseudocode
- [ ] Implement `transform_against_history(op, ops)` (fold `xf` over committed ops in order, keep only `A'`)

### 2. Unit tests for transformation (`tests/test_ot.py`)
- [ ] One test per row of the edit-vs-edit table, including tie cases and boundary positions (`p == q`, `p == q + k`)
- [ ] del/del overlap cases: disjoint left/right, contained, containing, partial overlaps, full cancel → `[]`
- [ ] Multi-edit `xf` cases checked against hand-computed expected outputs
- [ ] Randomized convergence test: `apply(apply(s, B), A') == apply(apply(s, A), B')` for random ops (seeded)
- [ ] Validation tests: malformed edits → bad_request category, bounds errors → out_of_range category

### 3. Persistence layer (in `server.py`)
- [ ] Map document names to safe filenames (hex of UTF-8 name) under `--data-dir`; create the dir if missing
- [ ] Append-only JSONL log per document: one line `{"rev", "client", "id", "op"}` per commit, written and flushed **before** ack/broadcast
- [ ] On startup, load every log: rebuild text, history, per-revision text lengths, and the `(client, id) -> rev` map; ignore a truncated trailing line (crash mid-write) and truncate the file back to the last complete line

### 4. Server networking and protocol (`server.py`)
- [ ] CLI: `--port`, `--data-dir`; listen on `127.0.0.1` with `asyncio` streams and a large per-line limit
- [ ] Per-connection read loop: decode line, `bad_json` for invalid JSON / non-object; handle oversized lines and abrupt disconnects without affecting others
- [ ] `join` handling: validate fields, create doc lazily, reply `joined` with `text` or with `ops` since `R` (`bad_rev` if out of range), register connection for broadcasts
- [ ] `op` handling: field validation (`bad_request`), `not_joined`, `bad_rev`, duplicate `(client, id)` → ack original rev, validate against length at rev `R` (`out_of_range`), transform against history, apply, persist, ack sender, broadcast to other joined connections
- [ ] Errors include `id` when the request had one; nothing committed on error
- [ ] Commit + persist + ack + broadcast done synchronously inside the event loop (no `await` in between) so per-doc ordering and "ack after earlier broadcasts" hold; writes go to each connection's transport in order
- [ ] Clean up connection registrations on disconnect

### 5. Server tests (`tests/test_server.py`, `tests/conftest.py`)
- [ ] Fixture: start `server.py` as a subprocess on a free port with a `tmp_path` data dir; wait until it accepts connections; kill on teardown
- [ ] Join (fresh doc, existing doc, `since`), op ack + broadcast, concurrent ops from stale revisions transform correctly
- [ ] Every error code, plus "connection keeps working after an error"
- [ ] Exactly-once: resend same id → original rev, no new commit; still true after restart
- [ ] SIGKILL + restart preserves text, rev and `since` history
- [ ] Client disconnecting halfway through a line doesn't disturb other clients
- [ ] Ordering: broadcasts arrive in revision order with no gaps; ack arrives after earlier broadcasts

### 6. Client library (`livedoc_client.py`)
- [ ] `Client.__init__`: connect, send `join`, start background reader thread, wait for `joined` (raise `TimeoutError`/`ConnectionError` on failure)
- [ ] State under a lock: `text`, `rev`, `inflight` op (+ its id), `buffer` op; `text`/`rev` exposed as thread-safe properties
- [ ] `insert`/`delete`: validate against local text (`ValueError`), apply locally, append to buffer, send if nothing in flight
- [ ] On `ack`: set `rev`, clear inflight, send buffer as next op based on new `rev`; signal sync condition when nothing pending
- [ ] On remote `op`: `xf(inflight, remote)` then `xf(buffer, remote')`, apply the final remote op to `text`, set `rev`
- [ ] Ignore messages for other docs; `wait_synced(timeout)` via `threading.Condition`; `close()` shuts the socket and joins the thread

### 7. Client tests (`tests/test_client.py`)
- [ ] Single client edits sync and match server text (verified via a fresh join)
- [ ] Several clients making concurrent random edits in threads all converge to the same text as the server
- [ ] Only one op in flight (inspect via a fake/raw server or by observing server-side messages)
- [ ] `ValueError` on invalid edits; join timeout raises; messages for other docs ignored

### 8. Documentation and final check
- [ ] Write `README.md`: how to run the server, the client, and the tests; design summary (OT rules, server event loop, persistence format, client state machine)
- [ ] Run `pytest tests/ -q`; confirm all pass and nothing is left in the project dir (no data dirs, `__pycache__` aside — use `-p no:cacheprovider` guidance or document)

## Notes
- Assumptions (Phase 2 skipped):
  - Shared OT code lives in a helper module `livedoc_ot.py` imported by both server and client (spec lists required files, not exclusive ones).
  - Validation against revision `R` only needs text **lengths**, so the server stores the length after each revision instead of old texts.
  - The client does not auto-reconnect; on connection loss it stops and `wait_synced` returns False.
  - Re-joining a doc already joined on the same connection replies `joined` again without double-registering.
  - Integers must be real `int` (JSON `true`/`false` rejected as `bad_request`); doc name length >100 or empty → `bad_request`.
  - Unbounded per-connection output buffering is acceptable (no slow-consumer eviction).
- Risk: transformation must match the spec character-for-character — the del/ins split and tie rules in reverse direction are the most error-prone; covered by dedicated tests.
- Tests leave nothing in the project directory: all data dirs use `tmp_path`; pytest cache disabled via `pytest.ini`/`conftest` if needed.
