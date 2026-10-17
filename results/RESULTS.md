# Livedoc (v3) results

Scores are hidden-grader checks passed: stage 1 = SPEC.md (39 checks), stage 2 = CHANGE_REQUEST.md (12 checks).
"Stage 1 @ deadline" is the stage-1 snapshot taken when the 60-min stage-1 budget ended; "final" is after stage 2
(30 min more, same session), so final stage-1 scores include fixes made during stage 2.

## Per arm (median of 3, range in brackets)

| Arm | Final /51 | Stage 1 @ deadline /39 | Final stage 1 /39 | Final stage 2 /12 | Wall s1+s2 | Cost |
|---|---|---|---|---|---|---|
| Haiku 5.5 (Claude Code) | 51 [51–51] | 39 [39–39] | 39 [39–39] | 12 [12–12] | 13 min | $1.96 total |
| Opus 5.5 (Claude Code) | 51 [51–51] | 39 [39–39] | 39 [39–39] | 12 [12–12] | 13 min | $11.99 total |
| Nemotron 3.5 Lightning (pi) | 1 [1–1] | 1 [1–2] | 1 [1–1] | 0 [0–0] | 90 min | local |
| Opus plan → Nemotron (pi) | 4 [1–14] | 1 [1–4] | 4 [1–14] | 0 [0–0] | 90 min | local |
| Qwen3.6-35B-A3B (pi) | 19 [19–29] | 16 [16–17] | 19 [17–21] | 2 [0–8] | 90 min | local |
| Opus plan → Qwen3.6 (pi) | 11 [7–34] | 9 [5–26] | 9 [5–26] | 2 [2–8] | 90 min | local |

## Per run

| Run | Final /51 | S1 @ deadline | Final S1 | Final S2 | s1 wall (exit) | s2 wall (exit) | Turns | Output tok | Agent tests | LOC | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| haiku-r1 | 51 | 39 | 39 | 12 | 7.3 min (0) | 7.6 min (0) | 55 | 132,350 | 54 | 788 | $0.74 |
| haiku-r2 | 51 | 39 | 39 | 12 | 7.5 min (0) | 5.9 min (0) | 29 | 121,258 | 3066 | 724 | $0.46 |
| haiku-r3 | 51 | 39 | 39 | 12 | 7.1 min (0) | 5.0 min (0) | 52 | 155,330 | 2141 | 815 | $0.76 |
| opus-r1 | 51 | 39 | 39 | 12 | 6.7 min (0) | 5.1 min (0) | 40 | 65,132 | 91 | 783 | $3.66 |
| opus-r2 | 51 | 39 | 39 | 12 | 9.7 min (0) | 8.5 min (0) | 38 | 81,860 | 145 | 870 | $4.67 |
| opus-r3 | 51 | 39 | 39 | 12 | 5.9 min (0) | 6.8 min (0) | 41 | 65,337 | 69 | 791 | $3.66 |
| nemotron-r1 | 1 | 2 | 1 | 0 | 60.0 min (timeout) | 30.0 min (timeout) | 239 | 87,824 | – | 348 | – |
| nemotron-r2 | 1 | 1 | 1 | 0 | 60.0 min (timeout) | 30.0 min (timeout) | 68 | 46,279 | – | 1202 | – |
| nemotron-r3 | 1 | 1 | 1 | 0 | 60.0 min (timeout) | 30.0 min (timeout) | 36 | 36,700 | – | 1159 | – |
| opusplan-nemotron-r1 | 1 | 1 | 1 | 0 | 60.0 min (timeout) | 30.0 min (timeout) | 214 | 155,266 | 19 | 407 | – |
| opusplan-nemotron-r2 | 4 | 1 | 4 | 0 | 60.0 min (timeout) | 30.0 min (timeout) | 263 | 162,762 | 0 | 1598 | – |
| opusplan-nemotron-r3 | 14 | 4 | 14 | 0 | 60.0 min (timeout) | 30.0 min (timeout) | 114 | 95,772 | 0 | 750 | – |
| qwen-r1 | 29 | 16 | 21 | 8 | 60.0 min (timeout) | 30.0 min (timeout) | 77 | 139,815 | 46 | 1150 | – |
| qwen-r2 | 19 | 16 | 19 | 0 | 60.0 min (timeout) | 30.0 min (timeout) | 254 | 138,604 | 50 | 1054 | – |
| qwen-r3 | 19 | 17 | 17 | 2 | 60.0 min (timeout) | 30.0 min (timeout) | 88 | 124,855 | – | 1509 | – |
| opusplan-qwen-r1 | 11 | 9 | 9 | 2 | 60.0 min (timeout) | 30.0 min (timeout) | 204 | 129,187 | 67 | 1224 | – |
| opusplan-qwen-r2 | 7 | 5 | 5 | 2 | 60.0 min (timeout) | 30.0 min (timeout) | 94 | 136,128 | – | 961 | – |
| opusplan-qwen-r3 | 34 | 26 | 26 | 8 | 60.0 min (timeout) | 30.0 min (timeout) | 167 | 138,819 | 66 | 1292 | – |

## Check pass counts (final snapshots, out of 3 runs per arm)

| Check | haiku | opus | nemotron | opusplan-nemotron | qwen | opusplan-qwen |
|---|---|---|---|---|---|---|
| S1 | 3 | 3 | 3 | 2 | 3 | 3 |
| S2 | 3 | 3 | 0 | 2 | 3 | 3 |
| S3 | 3 | 3 | 0 | 1 | 3 | 1 |
| S4 | 3 | 3 | 0 | 0 | 3 | 1 |
| S5 | 3 | 3 | 0 | 1 | 3 | 1 |
| S6 | 3 | 3 | 0 | 1 | 3 | 1 |
| S7 | 3 | 3 | 0 | 1 | 3 | 1 |
| S8 | 3 | 3 | 0 | 1 | 2 | 1 |
| T1 | 3 | 3 | 0 | 0 | 2 | 1 |
| T2 | 3 | 3 | 0 | 0 | 2 | 1 |
| T3 | 3 | 3 | 0 | 0 | 1 | 1 |
| T4 | 3 | 3 | 0 | 0 | 2 | 1 |
| T5 | 3 | 3 | 0 | 0 | 1 | 1 |
| T6 | 3 | 3 | 0 | 0 | 0 | 1 |
| E1 | 3 | 3 | 0 | 1 | 3 | 3 |
| E2 | 3 | 3 | 0 | 0 | 0 | 0 |
| E3 | 3 | 3 | 0 | 0 | 0 | 0 |
| E4 | 3 | 3 | 0 | 0 | 0 | 0 |
| E5 | 3 | 3 | 0 | 1 | 3 | 1 |
| X1 | 3 | 3 | 0 | 1 | 0 | 1 |
| P1 | 3 | 3 | 0 | 1 | 1 | 1 |
| P2 | 3 | 3 | 0 | 1 | 1 | 1 |
| X2 | 3 | 3 | 0 | 1 | 0 | 1 |
| C1 | 3 | 3 | 0 | 0 | 0 | 1 |
| C2 | 3 | 3 | 0 | 0 | 0 | 1 |
| C3 | 3 | 3 | 0 | 0 | 0 | 1 |
| C4 | 3 | 3 | 0 | 0 | 0 | 0 |
| R1 | 3 | 3 | 0 | 0 | 1 | 1 |
| R2 | 3 | 3 | 0 | 0 | 0 | 1 |
| R3 | 3 | 3 | 0 | 0 | 1 | 1 |
| R4 | 3 | 3 | 0 | 0 | 0 | 0 |
| R5 | 3 | 3 | 0 | 0 | 1 | 1 |
| H1 | 3 | 3 | 0 | 0 | 1 | 1 |
| H2 | 3 | 3 | 0 | 0 | 1 | 1 |
| H3 | 3 | 3 | 0 | 0 | 1 | 1 |
| H4 | 3 | 3 | 0 | 0 | 0 | 1 |
| L1 | 3 | 3 | 0 | 0 | 3 | 3 |
| L2 | 3 | 3 | 0 | 0 | 3 | 1 |
| L3 | 3 | 3 | 0 | 0 | 3 | 3 |
| L4 | 3 | 3 | 0 | 0 | 1 | 1 |
| L5 | 3 | 3 | 0 | 0 | 1 | 0 |
| L6 | 3 | 3 | 0 | 0 | 0 | 0 |
| L7 | 3 | 3 | 0 | 0 | 2 | 1 |
| L8 | 3 | 3 | 0 | 0 | 0 | 0 |
| L9 | 3 | 3 | 0 | 0 | 0 | 0 |
| K1 | 3 | 3 | 0 | 0 | 2 | 2 |
| K2 | 3 | 3 | 0 | 0 | 0 | 0 |
| K3 | 3 | 3 | 0 | 0 | 2 | 2 |
| Q1 | 3 | 3 | 0 | 0 | 1 | 0 |
| Q2 | 3 | 3 | 0 | 3 | 3 | 2 |
| Q3 | 3 | 3 | 0 | 1 | 1 | 0 |
