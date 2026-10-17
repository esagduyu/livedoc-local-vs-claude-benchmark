#!/usr/bin/env python3
"""Summarize the Livedoc (v3) benchmark: grades + agent metrics -> results/RESULTS.md and results/summary.json.

Inputs (relative to the repo root):
  results/grades-stage1.json   grade.py --stage 1 --json over runs/*/stage1
  results/grades-final.json    grade.py --json over runs/*/final
  logs/<run>-s1.jsonl[.gz], logs/<run>-s2.jsonl[.gz], logs/runner.log
"""
import json
import re
import statistics
from pathlib import Path

from metrics import metrics

ROOT = Path(__file__).resolve().parent
ARMS = [
    ("haiku", "Haiku 5.5 (Claude Code)"),
    ("opus", "Opus 5.5 (Claude Code)"),
    ("nemotron", "Nemotron 3.5 Lightning (pi)"),
    ("opusplan-nemotron", "Opus plan → Nemotron (pi)"),
    ("qwen", "Qwen3.6-35B-A3B (pi)"),
    ("opusplan-qwen", "Opus plan → Qwen3.6 (pi)"),
]
REPS = 3


def load_grades(name):
    out = {}
    for g in json.loads((ROOT / "results" / name).read_text()):
        run = Path(g["run"]).parent.name          # runs/<run>/<stage1|final>
        out[run] = g
    return out


def runner_walls():
    walls, exits = {}, {}
    for line in (ROOT / "logs" / "runner.log").read_text().splitlines():
        m = re.match(r"(\S+):(s[12]) exit=(\d+) wall=([\d.]+)s", line)
        if m:
            walls[(m[1], m[2])] = float(m[4])     # a rerun overwrites the killed attempt
            exits[(m[1], m[2])] = int(m[3])
    return walls, exits


def stage_metrics(run, stage):
    p = ROOT / "logs" / f"{run}-{stage}.jsonl"
    if not p.exists() and not Path(str(p) + ".gz").exists():
        return {}
    return metrics(p)


def main():
    s1, fin = load_grades("grades-stage1.json"), load_grades("grades-final.json")
    walls, exits = runner_walls()
    records = []
    for prefix, label in ARMS:
        for n in range(1, REPS + 1):
            run = f"{prefix}-r{n}"
            if run not in fin:
                continue
            m1, m2 = stage_metrics(run, "s1"), stage_metrics(run, "s2")
            f = fin[run]
            rec = {
                "arm": prefix, "label": label, "run": run,
                "stage1_at_deadline": s1[run]["stage1"] if run in s1 else None,
                "final_stage1": f["stage1"], "final_stage2": f["stage2"], "final_total": f["passed"],
                "total": f["total"], "agent_tests": f["tests"], "runtime_loc": f["runtime_loc"],
                "wall_s1": walls.get((run, "s1")), "wall_s2": walls.get((run, "s2")),
                "exit_s1": exits.get((run, "s1")), "exit_s2": exits.get((run, "s2")),
                "cost_usd": round(sum(x.get("cost_usd") or 0 for x in (m1, m2)), 2) if m1.get("harness") == "claude-code" else 0.0,
                "turns": sum(x.get("turns") or 0 for x in (m1, m2)),
                "output_tokens": sum(x.get("output_tokens") or 0 for x in (m1, m2)),
                "failed_final": [c["id"] for c in f["checks"] if not c["ok"]],
            }
            records.append(rec)

    lines = ["# Livedoc (v3) results", "",
             "Scores are hidden-grader checks passed: stage 1 = SPEC.md (39 checks), stage 2 = CHANGE_REQUEST.md (12 checks).",
             "\"Stage 1 @ deadline\" is the stage-1 snapshot taken when the 60-min stage-1 budget ended; \"final\" is after stage 2",
             "(30 min more, same session), so final stage-1 scores include fixes made during stage 2.", "",
             "## Per arm (median of 3, range in brackets)", "",
             "| Arm | Final /51 | Stage 1 @ deadline /39 | Final stage 1 /39 | Final stage 2 /12 | Wall s1+s2 | Cost |",
             "|---|---|---|---|---|---|---|"]

    def med(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return "–"
        return f"{statistics.median(vals):g} [{min(vals):g}–{max(vals):g}]"

    for prefix, label in ARMS:
        rs = [r for r in records if r["arm"] == prefix]
        if not rs:
            continue
        wall = [((r["wall_s1"] or 0) + (r["wall_s2"] or 0)) / 60 for r in rs]
        cost = sum(r["cost_usd"] for r in rs)
        lines.append(f"| {label} | {med([r['final_total'] for r in rs])} | {med([r['stage1_at_deadline'] for r in rs])} | "
                     f"{med([r['final_stage1'] for r in rs])} | {med([r['final_stage2'] for r in rs])} | "
                     f"{statistics.median(wall):.0f} min | " + (f"${cost:.2f} total" if cost else "local") + " |")
    lines += ["", "## Per run", "",
              "| Run | Final /51 | S1 @ deadline | Final S1 | Final S2 | s1 wall (exit) | s2 wall (exit) | Turns | Output tok | Agent tests | LOC | Cost |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        def w(s, e):
            return "–" if s is None else f"{s/60:.1f} min ({'timeout' if e == 124 else e})"
        lines.append(f"| {r['run']} | {r['final_total']} | {r['stage1_at_deadline']} | {r['final_stage1']} | {r['final_stage2']} | "
                     f"{w(r['wall_s1'], r['exit_s1'])} | {w(r['wall_s2'], r['exit_s2'])} | {r['turns']} | {r['output_tokens']:,} | "
                     f"{r['agent_tests'] if r['agent_tests'] is not None else '–'} | {r['runtime_loc']} | "
                     + (f"${r['cost_usd']:.2f}" if r['cost_usd'] else "–") + " |")

    # Which checks each local arm ever passed (final snapshots).
    lines += ["", "## Check pass counts (final snapshots, out of 3 runs per arm)", ""]
    ids = [c["id"] for c in next(iter(fin.values()))["checks"]]
    lines.append("| Check | " + " | ".join(p for p, _ in ARMS) + " |")
    lines.append("|---|" + "---|" * len(ARMS))
    for cid in ids:
        row = []
        for prefix, _ in ARMS:
            rs = [r for r in records if r["arm"] == prefix]
            row.append(str(sum(cid not in r["failed_final"] for r in rs)))
        lines.append(f"| {cid} | " + " | ".join(row) + " |")

    (ROOT / "results" / "RESULTS.md").write_text("\n".join(lines) + "\n")
    (ROOT / "results" / "summary.json").write_text(json.dumps(records, indent=1))
    print("\n".join(lines[:16]))


if __name__ == "__main__":
    main()
