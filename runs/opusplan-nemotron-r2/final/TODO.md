# TODO

## Goal
Build Livedoc per SPEC.md: an asyncio OT server (`server.py`), a threaded Python client library (`livedoc_client.py`), pytest tests (`tests/`) and a `README.md`, using only the Python 3.12 standard library.

## Tasks

### 1. Transformation core (in `livedoc_client.py`, imported by `server.py`)
- [ ] Add `validate_op(op, length)`: check structure (list of dicts with exactly `ins`+`text` or `del`+`len`, ints not bools, non-empty text, `len >= 1`), return `"bad_request"` / `"out_of_range"` / `None` and the resulting length
- [ ] Add `apply_op(text, op) -> str`
- [ ] Add `xf_edit(a, b) -> (a_list, b_list)` implementing all four edit-vs-edit cases in both directions (earlier-committed `b` wins insert ties; del/del leaves the one contiguous remainder or nothing)
- [ ] Add recursive `xf(A, B) -> (A', B')` exactly as the pseudocode in the spec
- [ ] Add `transform_against_history(op, ops)` that folds `xf` over committed ops in order

### 2. Server: storage and persistence
- [ ] `Document` class holding text, list of committed entries `{rev, client, id, op}`, per-revision text lengths (to validate ops against revision `R` without rebuilding old text), and `(client, id) -> rev` dedup map
- [ ] Map document names to safe file names (hex of UTF-8 name) under `--data-dir`, creating the dir if missing
- [ ] Append each committed entry as one JSON line to the doc's log file and flush (no fsync) **before** sending the ack/broadcasts
- [ ] On startup, replay every log file; ignore a truncated/invalid trailing line left by SIGKILL (and truncate it off so later appends stay well-formed)
- [ ] Create a doc lazily on first join (empty log file written so it persists at rev 0)

### 3. Server: protocol handling
- [ ] CLI `--port`, `--data-dir`; asyncio server on `127.0.0.1`, runs until killed
- [ ] Per-connection reader: `readline` with a large limit; handle over-long lines and partial last line on disconnect without affecting other connections; replies via a per-connection ordered writer
- [ ] Parse & validate messages → `bad_json` / `bad_request` (unknown type, missing/wrong-typed fields, empty or >100-char doc, empty client name, malformed op); include `"id"` in errors when present
- [ ] `join`: register connection for the doc, reply `joined` with `text`, or with `ops` when `since` given (`bad_rev` if out of range); re-joining same doc on same connection just replies again
- [ ] `op`: order of checks `bad_request` → `not_joined` → duplicate `(client, id)` (ack with original rev) → `bad_rev` → `out_of_range` against revision `R`'s length → transform → apply → persist → broadcast to other joined connections → ack sender
- [ ] Ensure ordering guarantees: all commits/broadcasts done synchronously on the single event loop thread, so each connection's outgoing queue sees revisions in order and ack after earlier broadcasts
- [ ] Clean up connection registrations on disconnect; a write error to one client never raises into others

### 4. Client library
- [ ] `Client.__init__`: connect, start background reader thread, send `join`, wait for `joined` up to `timeout` else close and raise `TimeoutError`
- [ ] Thread-safe state under a lock: `text`, `rev`, `inflight` (op + id), `buffer` (pending edits, concatenated into one op)
- [ ] `insert` / `delete`: validate against local text (`ValueError`), apply locally, then send immediately if nothing in flight, otherwise append to buffer
- [ ] On `ack`: set `rev`, clear inflight, send buffer (based on latest `rev`) as the next op
- [ ] On remote `op`: transform it against inflight then buffer with `xf` (remote = earlier-committed), update inflight/buffer to their transformed versions, apply to `text`, set `rev`
- [ ] Ignore messages for other docs; handle `error` replies (record them; don't crash the reader thread)
- [ ] `wait_synced(timeout)` via a condition variable; `close()` shuts socket and joins thread

### 5. Tests (`tests/`)
- [ ] Test hygiene: `conftest.py` sets `sys.dont_write_bytecode`, `pytest.ini`/`addopts` disables cache provider, all data dirs in `tmp_path`; verify nothing is left in the project dir after `pytest tests/ -q`
- [ ] Fixture that starts `server.py` as a subprocess on a free port with a temp data dir, waits until it accepts, and kills it after
- [ ] Unit tests for each edit-vs-edit table row incl. ties, split delete, overlapping deletes, vanishing edits, and reversed-role rule
- [ ] Randomized property test: for random base text and ops, `apply(apply(S,B),A') == apply(apply(S,A),B')`, and multi-edit `xf` equals edit-by-edit sequential commit
- [ ] Server tests: join (text and `since`), op ack/broadcast, transform of stale op, empty-result op committed as `[]`, every error code, connection survives errors, half-line disconnect doesn't affect others, exactly-once duplicate ack
- [ ] Persistence test: commit ops, SIGKILL server, restart on same dir, check text/rev/history/dedup preserved
- [ ] Client tests: optimistic local edits, `ValueError`, one-op-in-flight (check via server history), multiple clients doing concurrent random edits converge to server text, Unicode positions

### 6. Documentation
- [ ] `README.md`: how to run the server, client usage example, how to run tests, design summary (OT, single-threaded event loop ordering, append-only logs, dedup, client state machine)
- [ ] Final run of `pytest tests/ -q` passes and project dir is clean

## Notes
- Assumptions (Phase 2 skipped):
  - OT functions live in `livedoc_client.py` and `server.py` imports them, avoiding an extra module beyond the listed deliverables.
  - Duplicate `(client, id)` is checked before `bad_rev`/`out_of_range`, so a resend after reconnect is always acked with its original revision.
  - Extra unknown fields in messages are ignored; booleans are not accepted as ints.
  - Client does not auto-reconnect (not required by spec); join timeout raises `TimeoutError`.
  - Storage: one append-only JSONL log per document; history kept in memory (fine for expected sizes).
- Risks:
  - Pytest/assertion rewriting can create `__pycache__` / `.pytest_cache`; must be suppressed to satisfy "leave nothing behind".
  - A client that never reads could grow server-side write buffers; acceptable, but writes must never block the event loop for others.
