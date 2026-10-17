#!/usr/bin/env python3
"""Extract per-run metrics from agent logs.

pi --mode json logs (local arms): one JSON event per line; assistant `message_end`
events carry per-turn usage {input, output, cacheRead}.
claude -p --output-format stream-json logs (Claude arms): the final `result`
event carries duration, turn count, usage and cost.

Usage: python3 metrics.py <log.jsonl> [...]   -> JSON list on stdout
"""
import gzip
import json
import sys
from pathlib import Path


def pi_metrics(lines):
    m = {"harness": "pi", "turns": 0, "tool_calls": 0, "tool_errors": 0, "input_tokens": 0,
         "cache_read_tokens": 0, "output_tokens": 0, "skill_file_read": False, "stop": None}
    first_ts = last_ts = None
    for ev in lines:
        t = ev.get("type")
        msg = ev.get("message") or {}
        ts = msg.get("timestamp")
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if t == "message_end" and msg.get("role") == "assistant":
            m["turns"] += 1
            u = msg.get("usage") or {}
            m["input_tokens"] += u.get("input", 0)
            m["cache_read_tokens"] += u.get("cacheRead", 0)
            m["output_tokens"] += u.get("output", 0)
            for c in msg.get("content") or []:
                if c.get("type") == "toolCall":
                    m["tool_calls"] += 1
                    args = json.dumps(c.get("arguments", {}))
                    if "plan-first" in args:
                        m["skill_file_read"] = True
            m["stop"] = msg.get("stopReason") or m["stop"]
        if t == "message_end" and msg.get("role") == "toolResult" and msg.get("isError"):
            m["tool_errors"] += 1
    if first_ts and last_ts:
        m["model_span_s"] = round((last_ts - first_ts) / 1000, 1)
    return m


def claude_metrics(lines):
    m = {"harness": "claude-code", "tool_calls": 0}
    for ev in lines:
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    m["tool_calls"] += 1
        if ev.get("type") == "result":
            u = ev.get("usage") or {}
            m.update({
                "turns": ev.get("num_turns"),
                "duration_s": round(ev.get("duration_ms", 0) / 1000, 1),
                "api_duration_s": round(ev.get("duration_api_ms", 0) / 1000, 1),
                "cost_usd": ev.get("total_cost_usd"),
                "input_tokens": u.get("input_tokens", 0),
                "cache_read_tokens": u.get("cache_read_input_tokens", 0),
                "cache_write_tokens": u.get("cache_creation_input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "is_error": ev.get("is_error"),
                "models": list((ev.get("modelUsage") or {}).keys()),
            })
    return m


def read_log(path: Path) -> str:
    """Read a log, falling back to its gzipped copy (the published repo stores logs as .jsonl.gz)."""
    if not path.exists() and Path(str(path) + ".gz").exists():
        path = Path(str(path) + ".gz")
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode(errors="replace")
    return path.read_text(errors="replace")


def metrics(path: Path):
    lines = []
    for raw in read_log(path).splitlines():
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    is_claude = any(e.get("type") == "result" and "num_turns" in e for e in lines)
    out = claude_metrics(lines) if is_claude else pi_metrics(lines)
    out["log"] = Path(str(path).removesuffix(".gz")).name
    return out


if __name__ == "__main__":
    print(json.dumps([metrics(Path(p)) for p in sys.argv[1:]], indent=2))
