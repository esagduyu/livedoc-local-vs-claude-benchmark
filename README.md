# Livedoc: a collaborative-editor benchmark for local vs Claude coding agents

v3 of [qwen-pi-plan-vs-haiku-benchmark](https://github.com/esagduyu/qwen-pi-plan-vs-haiku-benchmark) and [nemotron-pi-plan-vs-haiku-benchmark](https://github.com/esagduyu/nemotron-pi-plan-vs-haiku-benchmark).

v2's task, a CLI todo app, was saturated: with an Opus-written plan, Qwen3.6 scored 19/19 on every run, the same as Haiku. That left an open question. Were the local models really that close, or was the task just too easy to tell them apart? v3 replaces it with a task that needs real engineering, and grades it with a hidden, adversarial grader.

## The task

Build **Livedoc**, a small Google Docs-style system for plain text, from a [spec](spec/SPEC.md), in Python 3.12 with the standard library only. Every run builds it from scratch.

- **`server.py`**, a TCP server with a newline-delimited JSON protocol. Clients join a document and send edits (character inserts and deletes) tagged with the revision they were made against.
- **Operational transformation.**
  - Concurrent edits are rebased onto everything committed since their base revision.
  - The rules are exact and tie-broken: concurrent inserts at the same position, a delete spanning someone else's insert (it must split), overlapping deletes, multi-edit operations.
  - Every client has to converge to the same text, character for character.
- **Delivery guarantees.**
  - Edits are acked to the sender and broadcast to everyone else in a strict total order.
  - A resent edit ID is applied exactly once, even across restarts.
- **Durability.** Acked edits survive `SIGKILL` and a restart.
- **`livedoc_client.py`**, a threaded client library.
  - Local edits are optimistic.
  - At most one edit is in flight; later ones are buffered.
  - Remote edits are transformed against the client's own pending edits.
- Tests and a README written by the agent itself.

After stage 1 (60 min), the same agent session gets a [change request](spec/CHANGE_REQUEST.md) with 30 more minutes and no warning:

- **Live cursors.** Remote cursors move correctly through every edit, including the client's own unacked ones, and are removed on `leave`.
- **Document history.** Return any old revision's text, fast even with 5,000 revisions.

### Grading

The [grader](grader/) is never visible to the agents. It runs 51 checks:

| Group | Checks | What they probe |
|---|---|---|
| S | 8 | Basic protocol, broadcasts, Unicode code-point positions, `since` catch-up |
| T | 6 | Each transformation rule, against hand-computed results |
| E | 5 | Error codes, echoed IDs, nothing committed on error, a client dying mid-line |
| X, P | 4 | Exactly-once and durability across `SIGKILL` + restart |
| C | 4 | Fuzzing: 6 clients × 50 ops with stale bases checked against a reference; total ordering; 40 clients × 30 ops in 30 s; a 200k-char document |
| L | 9 | Client library against a scripted fake server, against the reference server, and end to end against its own server |
| Q | 3 | The agent's own tests pass, leave nothing behind; README |
| R, H, K | 12 | Stage 2: cursors (server and client) and history |

[Eleven mutants](mutants/) of the [reference solution](reference/) check that the grader catches the bug classes it targets. Each one plants a single bug: a wrong tie-break, no delete split, no persistence, no dedupe, several ops in flight, no client transform, validating against the current revision, echoing to the sender, crashing on bad JSON, no stage 2, and a wrong own-cursor rule. Each mutant fails exactly the checks it should, and the reference scores 51/51 ([selftest](selftest/)).

## Arms

| Arm | Planner | Builder | Harness |
|---|---|---|---|
| **haiku** | — | Claude Haiku 5.5 | Claude Code 2.1.300 |
| **opus** | — | Claude Opus 5.5 | Claude Code 2.1.300 |
| **nemotron** | — | Nemotron 3.5 Lightning 30B-A3B (IQ4_XS) | pi 1.2.0 |
| **opusplan-nemotron** | Opus 5.5 ([`plan-first`](skills/plan-first.md) → TODO.md) | Nemotron | pi |
| **qwen** | — | Qwen3.6-35B-A3B (UD-Q4_K_XL) | pi |
| **opusplan-qwen** | Opus 5.5 (same three plans) | Qwen3.6 | pi |

- **Repetitions:** 3 per arm.
- **Hardware:** the local models ran on one RTX 2080 Ti (11 GB), using llama.cpp b11641 with 128k context.
- **Plans:** Opus wrote three plans ([`opus-plans/`](opus-plans/)), and the n-th plan went to both local models' n-th run.
- **Sandboxing:** every agent ran in a bubblewrap sandbox that could see only its own working directory. That fixes v2's cross-run contamination.

## Results

| Arm | Final score /51 (per run) | Median | Stage 1 at 60-min deadline /39 | Wall time per run | Cost per run |
|---|---|---|---|---|---|
| **Haiku 5.5** | 51, 51, 51 | **51** | 39, 39, 39 | **~13 min** | **$0.65** |
| **Opus 5.5** | 51, 51, 51 | **51** | 39, 39, 39 | ~13 min | $4.00 |
| Nemotron | 1, 1, 1 | 1 | 2, 1, 1 | 90 min (timed out) | local |
| Opus plan → Nemotron | 1, 4, 14 | 4 | 1, 1, 4 | 90 min (timed out) | local + $0.30 plan |
| Qwen3.6 | 29, 19, 19 | 19 | 16, 16, 17 | 90 min (timed out) | local |
| Opus plan → Qwen3.6 | 11, 7, **34** | 11 | 9, 5, 26 | 90 min (timed out) | local + $0.30 plan |

Full per-run and per-check tables: [results/RESULTS.md](results/RESULTS.md).

### What it shows

- **The task separates the models now.** Every Claude run scored 51/51 in about 13 minutes, including Haiku. No local run went above 34/51, and **all 12 local runs used their full 60 + 30 minutes**.
- **No local run produced a working collaborative editor.**
  - Both end-to-end convergence checks (L8, L9: four concurrent clients converging) failed in all 12 local runs.
  - Only one local run (Opus plan → Qwen, run 3) passed the fuzz tests.
  - All 12 failed the error-reporting checks E2–E4, which require echoing the edit's ID on errors.
- **Qwen3.6 is clearly the stronger local model.**
  - Alone, it reliably builds a server that handles the basic protocol, scoring 19–29.
  - Nemotron alone never got past a server that starts and accepts connections (1/51 on every run).
- **An Opus plan is a gamble, not a fix.**
  - With Qwen: one run was the best local result (34/51, the only one passing the fuzz tests), but the other two came in below Qwen alone. Both low runs failed on one line in the server's input validation, which rejected every valid edit and so hid everything behind it. In run 1, Qwen required a `client` field on every op message, which the spec doesn't define; its own tests sent that field too, so they passed. In run 2, it checked that an insert's keys were exactly `{"ins"}` instead of `{"ins", "text"}`; it never wrote tests or started its server. Run 3 started its server 13 times during stage 1, and the other two never did.
  - **Counterfactual (not part of the scores above).** Fixing just the one rejecting check in each low run and re-grading in the same sandbox ([counterfactual/](counterfactual/)):
    - Run 1: 11 → **37/51**. That's the best local result, ahead of run 3's 34. Its server passed every transformation, fuzz, exactly-once and durability check. Its client still didn't converge (L5, L6, L8, L9).
    - Run 2: 7 → 19/51, about Qwen alone. With the check fixed, more bugs showed: no persistence, two transformation-rule bugs, a client that hangs, and broadcasts carrying `client: null` because, like run 1, it read the client name from the op message.
    - So the Opus-plan runs were less far apart than 7–34 suggests: 19, 34 and 37 once the first bug is fixed. The score is gated, and one input-validation bug hides everything behind it.
  - With Nemotron: the plan lifted the median from 1 to 4 (best 14), but it never got conflict resolution or a client working.
  - In v2, the plan reliably closed the gap. Here it changes *what* the model works on more than how well.
- **It isn't only about speed.**
  - Haiku produced 121k–155k output tokens per run in about 13 minutes. Qwen produced 125k–140k in 90 minutes, a similar volume of output for far lower quality.
  - Claude Code's token counts include thinking, and the local models ran with reasoning off, as in v1/v2.
  - More time would probably raise the local scores somewhat. But the failures are correctness failures in transformation and concurrency, not unfinished boilerplate.

Answering the question that started this: on v2's task the bottleneck was the task. On a task with real concurrency and correctness requirements, there is a large gap between these local models and even Haiku 5.5, and an Opus plan does not reliably close it.

## Repository layout

```
spec/            SPEC.md and CHANGE_REQUEST.md, exactly as given to the agents
grader/          hidden grader: grade.py, server_checks.py, client_driver.py, wire helpers, reference server copy
reference/       reference solution (51/51)
mutants/         11 deliberately broken copies of the reference, used to validate the grader
selftest/ref     the reference laid out as an agent deliverable
skills/          plan-first skill used for the Opus plans
opus-plans/      the three Opus TODO.md plans
runs/<run>/      stage1/ (snapshot at the 60-min deadline) and final/ (after the change request)
logs/            agent transcripts (.jsonl.gz), runner.log, regrade output; logs/invalid/ = the killed qwen-r2 attempt
counterfactual/  opusplan-qwen r1/r2 with their one validation bug fixed (marked # PATCH), and their grades
results/         grades-stage1.json, grades-final.json, RESULTS.md, summary.json
run_v3.sh        runner (sandboxing, both stages, all arms); smoke.sh = sandbox smoke test
grade_sb.sh      runs the grader inside its own sandbox
summarize.py     builds results/ from the grades + logs (metrics.py parses the transcripts)
```

## Reproducing

The models, llama.cpp build and pi/Claude Code setup are the same as v2; see its [Reproducing](https://github.com/esagduyu/nemotron-pi-plan-vs-haiku-benchmark#reproducing) section. In addition, v3 needs:

- `bubblewrap` and `sudo`. On Ubuntu 24.04, AppArmor blocks unprivileged user namespaces, so `run_v3.sh` builds the sandbox as root and drops back to the user with `setpriv --no-new-privs`.
- A Python 3.12 venv at `~/.venvs/livedoc` with `pytest==7.4.4`, shared read-only with the agents and used by the grader.

```bash
./smoke.sh                                          # sandbox sanity check (pi + Qwen, Claude Haiku)
./run_v3.sh plans haiku opus nemotron qwen          # ~19 h with the local timeouts
./grade_sb.sh runs/*/stage1 --stage 1 --json results/grades-stage1.json
./grade_sb.sh runs/*/final --json results/grades-final.json
python3 summarize.py
```

## Known caveats

- **n = 3 per arm**, and the local arms vary a lot from run to run (Opus plan → Qwen: 7 to 34).
- **Time-boxed.**
  - Every local run hit both timeouts; no Claude run came close.
  - Qwen generates about 40 tokens/s on this GPU, and the 90-minute budget was set before the runs.
  - A larger budget would likely help the local models somewhat.
- **Different harnesses and thinking settings.** Claude ran in Claude Code with its default thinking; the local models ran in pi with reasoning off. This mirrors v1/v2, but it isn't a like-for-like comparison of the models alone.
- **The grader's fake server was fixed twice mid-run.**
  - Two client checks assumed clients never send the stage-2 `cursors` query, or never send it from their background thread. Haiku run 2 and Opus run 2 exposed this.
  - The fake server now answers the query the way a real server would.
  - All runs were re-graded with the final grader after the runs (`logs/regrade-*.txt`), so every score in this README comes from the same grader.
- **One run was invalidated by the grader and rerun.**
  - The first qwen-r2 attempt was killed (SIGKILL) at 2.4 min. I was grading another run on the host at the time, and one of that run's own tests ran `pgrep -f server.py` and killed every match, including the live agent, whose prompt mentions `server.py`.
  - The grader now runs in its own sandbox (`grade_sb.sh`).
  - qwen-r2 was rerun from scratch after the other runs (`rerun_qwen_r2.sh`); the killed attempt's logs are in `logs/invalid/`.
- **Some agent code is racy, so a few checks are flaky.** qwen-r1's stage-1 snapshot scored 14/39 in its first grading and 16/39 in the re-grade. The tables use the re-grade.
- **Quants differ.** Qwen used UD-Q4_K_XL (22.4 GB) and Nemotron IQ4_XS (18.9 GB), each with its vendor-recommended sampling, as in v2.
- **The sandbox isolated the filesystem and processes, not the network.** Agents needed `localhost` (llama-server) and the Claude API, so they shared the host's network namespace. One Nemotron run listed the host's listening ports; that output is redacted in its transcript. Nothing in the task depends on the network beyond those two endpoints.
- **Transcripts are lightly anonymized.** The host's username and home directory are replaced with `user` / `/home/user` (including inside agent code that hard-coded the sandbox path), the host name with "the GPU host", and Tailscale addresses with `100.x.x.x` / `fd7a:x::x`. Nothing else in the logs was edited.
- **This repo publishes the hidden grader and reference solution.** Future models may have seen them, so a fresh variant of the task is the safer comparison for them.

## License

Benchmark artifacts (spec, grader, reference, runs, logs, scripts) are released under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). The `plan-first` skill is from a [public Reddit post](https://www.reddit.com/r/LocalLLaMA/comments/1stjwg5/); credit to its author.
