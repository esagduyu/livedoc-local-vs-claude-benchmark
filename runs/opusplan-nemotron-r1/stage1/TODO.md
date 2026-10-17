# TODO

## Goal
Build Livedoc per SPEC.md: an asyncio TCP server (`server.py`) with durable per-document operation logs, a threaded Python client (`livedoc_client.py`) using the specified operational transformation, pytest tests, and a README. Standard library only, Python 3.12.

## Tasks

### 1. Environment check
- [ ] Confirm `python3` is 3.12 and `pytest` is installed (stop and report if not)

### 2. Transformation core (in `livedoc_client.py`, imported by `server.py`)
- [ ] `validate_op(op)` structural check: list of dicts, each exactly `ins`+`text` (int, non-empty str) or `del`+`len` (int, int); reject bools as ints → used for `bad_request`
- [ ] `apply_op(text, op)` applying edits left to right; raises on out-of-range position, `len < 1`, empty insert → used for `out_of_range`
- [ ] `xf_edit(a, b)` → `(a_list, b_list)` for all four ins/del combinations in both directions, including insert-tie priority to the earlier-committed side, delete-split around an inserted string, and delete/delete overlap trimming
- [ ] `xf(A, B)` → `(A', B')` implementing the recursive operation-vs-operation definition exactly as written in the spec

### 3. Transformation tests (`tests/test_transform.py`)
- [ ] One table-driven test per spec row (each branch, each boundary: `p == q`, `p == q+k`, strictly inside, etc.) for both `a'` and `b'`
- [ ] Insert-tie test: earlier-committed insert ends up on the left in both orderings
- [ ] Randomized convergence test: for random text and random multi-edit ops A, B: `apply(apply(s, B), A') == apply(apply(s, A), B')`
- [ ] Randomized "equivalence" test: `xf(A, B)` result matches transforming each edit as its own operation (per spec definition)

### 4. Persistence layer (`server.py`)
- [ ] Per-document append-only JSONL log at `DIR/<hex-encoded-name>.log`; each line `{"rev", "client", "id", "op"}`; file name encoding is safe for any name ≤100 chars
- [ ] Append + `flush()` (no fsync) before the ack is sent
- [ ] On startup, lazily load a document's log when first referenced: replay ops to rebuild text, history and the `(client, id) → rev` dedupe map; ignore a truncated/invalid trailing line (and truncate the file to the last good line so later appends stay valid)
- [ ] Create `DIR` if missing

### 5. Server protocol (`server.py`)
- [ ] CLI: `--port`, `--data-dir`; bind `127.0.0.1`; run until killed
- [ ] Per-connection reader with a large line limit; oversized/partial lines and `UnicodeDecodeError` produce `bad_json` (or are dropped on disconnect) without affecting other connections
- [ ] Per-connection outbound queue + writer task, so a slow reader never blocks commits or other clients, and all messages to one connection go out in enqueue order
- [ ] `join`: validate `doc` (non-empty str, ≤100 chars), `client` (non-empty str), optional `since` (int); reply `joined` with `text` or `ops`; subscribe connection to broadcasts (re-join on same connection does not double-subscribe)
- [ ] `op`: validate fields (`bad_request`) → `not_joined` → duplicate `(client, id)` returns ack with original rev → `bad_rev` → apply against text at `rev` (`out_of_range`) → transform against ops after `rev` → append to log → enqueue broadcasts to other joined connections → enqueue ack to sender; done synchronously with no `await` between commit and enqueue so ordering guarantees hold
- [ ] Validating against "text at revision R": reconstruct by validating the op against `text@R` (kept via history replay from a snapshot cache or by inverse-free recompute) — implement by caching text per revision lazily from the log, bounded to what's needed
- [ ] Error replies with `code`, `message`, and `id` when the request had one; unknown `type` → `bad_request`; connection stays open
- [ ] Clean up subscriptions on disconnect

### 6. Client library (`livedoc_client.py`)
- [ ] `Client.__init__`: connect, send `join`, start background reader thread, wait for `joined` up to `timeout` else close and raise `TimeoutError`
- [ ] State under a lock: `text`, `rev`, `inflight` (op or None), `buffer` (list of edits)
- [ ] `insert`/`delete`: validate against local text (`ValueError`), apply locally, append to buffer, send buffer as a new op (uuid4 id, base `rev`) if nothing is in flight
- [ ] On `ack`: set `rev`, clear `inflight`, send buffer as next op if non-empty
- [ ] On remote `op`: `(inflight', r1) = xf(inflight, r)`; `(buffer', r2) = xf(buffer, r1)`; apply `r2` to `text`; set `rev`
- [ ] Ignore messages for other docs; `wait_synced` via condition variable; `close` shuts socket and joins thread

### 7. Server and client tests (`tests/`)
- [ ] `conftest.py`: fixture starting `python3 -B server.py` on a free port with a `tmp_path` data dir; waits until port accepts; kills on teardown; raw-socket helper for line-JSON
- [ ] Protocol tests: join/joined text, join with `since`, ack + broadcast shape, sender doesn't receive own broadcast, all error codes with `id` echoing, connection survives errors, half-line disconnect doesn't affect others, empty-op commit, vanishing op committed as `[]`
- [ ] Exactly-once tests: duplicate id acks original rev, also after SIGKILL + restart
- [ ] Persistence test: SIGKILL, restart, text/rev/`since` history preserved; truncated trailing log line tolerated
- [ ] Client tests: basic sync, concurrent random edits from 3+ clients converge to server text, buffering keeps ≤1 op in flight, `ValueError` on invalid edits, join timeout raises, unicode (non-BMP) positions
- [ ] Tests leave nothing behind: `pytest.ini` with `-p no:cacheprovider`, `sys.dont_write_bytecode = True` in conftest, servers run with `-B`, and a `pytest_sessionfinish` hook removing any `__pycache__` in the project
- [ ] `pytest tests/ -q` passes and `ls -a` afterwards shows no new artifacts

### 8. Documentation
- [ ] `README.md`: run instructions (server, tests), design summary (OT rules, server commit pipeline, log format/recovery, client state machine)

## Notes
- Assumption (Phase 2 skipped): OT functions live in `livedoc_client.py` and `server.py` imports them, so the deliverable set stays exactly as listed in the spec and both sides share one implementation.
- Assumption: error-check order for `op` is `bad_request` → `not_joined` → duplicate-ack → `bad_rev` → `out_of_range`, so a retransmitted already-committed op always gets its original ack.
- Assumption: re-joining the same doc on the same connection just re-sends `joined` and keeps a single subscription.
- Assumption: extra unknown fields in messages are ignored; `bool` is never accepted where an int is required.
- Assumption: client raises `TimeoutError` (or `ConnectionError` if the connection fails) from `__init__`; reconnection is not required by the spec and is out of scope.
- Risk: "text at revision R" for validation — simplest correct approach is replaying the log prefix; acceptable for test-scale histories, noted in README as a known limitation if not cached.
- Durability: `flush()` to the OS before ack is sufficient for SIGKILL; no fsync per spec.
