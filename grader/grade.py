#!/usr/bin/env python3
"""Hidden grader for the Livedoc benchmark.

Usage: python3 grade.py <run_dir> [<run_dir> ...] [--stage 1|2] [--json out.json] [--only S1,T3,...]

--stage 1 grades only the stage-1 checks (39); --stage 2 (default) grades all 51.
Each run directory is copied to a temp dir first; nothing in it is modified.
Must run with the same Python the agents used (3.12).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server_checks as sc  # noqa: E402
from lg_wire import ServerProc  # noqa: E402

CLIENT_CHECKS = [
    ("L1", 1, "client: constructor joins and exposes text/rev"),
    ("L2", 1, "client: edits apply locally at once and are sent against the latest rev"),
    ("L3", 1, "client: invalid edits raise ValueError and send nothing"),
    ("L4", 1, "client: a remote op arriving while an op is in flight is merged correctly"),
    ("L5", 1, "client: one op in flight; buffered edits are sent after the ack"),
    ("L6", 1, "client: in-flight + buffered edits + several remote ops converge with the server"),
    ("L7", 1, "client: ignores messages for other documents"),
    ("L8", 1, "client: 4 concurrent clients converge against the reference server"),
    ("L9", 1, "end-to-end: 4 concurrent clients converge against the agent's own server"),
    ("K1", 2, "client: remote cursors appear in Client.cursors and update"),
    ("K2", 2, "client: cursors move through pending local edits and later remote ops"),
    ("K3", 2, "client: leave removes a cursor; own name never listed"),
]
QUALITY = [
    ("Q1", 1, "agent's own tests pass (pytest tests/ -q)"),
    ("Q2", 1, "tests leave nothing behind in the project directory"),
    ("Q3", 1, "README explains how to run the server and the tests"),
]
SERVER_TIMEOUT = 120
CLIENT_TIMEOUT = 120


def listing(root):
    out = set()
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", ".pytest_cache")]
        for f in files:
            if not f.endswith(".pyc"):
                out.add(os.path.relpath(os.path.join(d, f), root))
        for x in dirs:
            out.add(os.path.relpath(os.path.join(d, x), root) + "/")
    return out


def quality(proj, python):
    res = {}
    before = listing(proj)
    has_tests = os.path.isdir(os.path.join(proj, "tests"))
    tests_ok, detail, tests_count = False, "no tests/ directory", None
    if has_tests:
        try:
            p = subprocess.run([python, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"], cwd=proj,
                               capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
            tests_ok, detail = p.returncode == 0, tail[0][:200]
            import re
            m = re.search(r"(\d+) passed", p.stdout)
            tests_count = int(m.group(1)) if m else 0
        except subprocess.TimeoutExpired:
            detail = "pytest timed out after 300 s"
    res["Q1"] = (tests_ok, "" if tests_ok else detail)
    after = listing(proj)
    new = sorted(after - before)
    res["Q2"] = (has_tests and not new, "" if has_tests and not new else ("no tests" if not has_tests else f"left behind: {new[:5]}"))
    readme = ""
    for name in ("README.md", "readme.md", "README"):
        if os.path.exists(os.path.join(proj, name)):
            readme = open(os.path.join(proj, name), encoding="utf-8", errors="replace").read()
            break
    ok = "server.py" in readme and "pytest" in readme
    res["Q3"] = (ok, "" if ok else ("no README" if not readme else "README lacks server.py / pytest instructions"))
    return res, tests_count


def run_server_check(fn, ctx):
    box = {}

    def target():
        try:
            fn(ctx)
            box["ok"] = True
        except Exception as e:  # noqa: BLE001
            box["err"] = f"{type(e).__name__}: {e}"[:300]
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(SERVER_TIMEOUT)
    if th.is_alive():
        return False, f"check timed out after {SERVER_TIMEOUT}s"
    return box.get("ok", False), box.get("err", "")


def run_client_check(cid, proj, python, ctx):
    srv = None
    args = [python, os.path.join(HERE, "client_driver.py"), cid, proj]
    if cid == "L9":
        try:
            srv = ctx.new_server()
        except sc.StartFail as e:
            return False, f"agent server did not start: {e}"[:300]
        args.append(str(srv.port))
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=CLIENT_TIMEOUT, stdin=subprocess.DEVNULL)
        line = [x for x in p.stdout.splitlines() if x.startswith("__RESULT__")]
        if not line:
            return False, ("driver crashed: " + (p.stderr or p.stdout).strip()[-250:])
        r = json.loads(line[-1][len("__RESULT__"):])
        return r["ok"], r["detail"]
    except subprocess.TimeoutExpired:
        return False, f"timed out after {CLIENT_TIMEOUT}s"
    finally:
        if srv:
            srv.stop()


def kill_strays(ctx_tmp):
    # Kill any agent server processes started from this temp tree that a timed-out check left running.
    try:
        out = subprocess.run(["pgrep", "-f", ctx_tmp], capture_output=True, text=True).stdout.split()
        for pid in out:
            if int(pid) != os.getpid():
                subprocess.run(["kill", "-9", pid], capture_output=True)
    except Exception:
        pass


def grade(run_dir, stage, only, python):
    tmp = tempfile.mkdtemp(prefix="livedoc-grade-")
    proj = os.path.join(tmp, "project")
    shutil.copytree(run_dir, proj, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".git"))
    ctx = sc.Ctx(proj, python, tmp)
    results = []
    t0 = time.time()
    checks = [(c, s, t, "server", fn) for c, s, t, fn in sc.CHECKS] + \
             [(c, s, t, "client", None) for c, s, t in CLIENT_CHECKS] + \
             [(c, s, t, "quality", None) for c, s, t in QUALITY]
    checks = [c for c in checks if c[1] <= stage and (not only or c[0] in only)]
    q = None
    for cid, st, title, kind, fn in checks:
        if not os.path.exists(os.path.join(proj, "server.py")) and kind == "server":
            ok, detail = False, "no server.py"
        elif kind == "server":
            ok, detail = run_server_check(fn, ctx)
            kill_strays(tmp)
        elif kind == "client":
            if not os.path.exists(os.path.join(proj, "livedoc_client.py")):
                ok, detail = False, "no livedoc_client.py"
            else:
                ok, detail = run_client_check(cid, proj, python, ctx)
            kill_strays(tmp)
        else:
            if q is None:
                q, tests_count = quality(proj, python)
            ok, detail = q[cid]
        results.append({"id": cid, "stage": st, "title": title, "ok": ok, "detail": detail})
    shutil.rmtree(tmp, ignore_errors=True)
    s1 = [r for r in results if r["stage"] == 1]
    s2 = [r for r in results if r["stage"] == 2]
    loc = 0
    for d, dirs, files in os.walk(run_dir):
        dirs[:] = [x for x in dirs if x not in ("tests", "__pycache__", ".pytest_cache", ".git")]
        loc += sum(sum(1 for _ in open(os.path.join(d, f), errors="replace")) for f in files if f.endswith(".py"))
    return {"run": run_dir, "stage1": sum(r["ok"] for r in s1), "stage1_total": len(s1),
            "stage2": sum(r["ok"] for r in s2), "stage2_total": len(s2),
            "passed": sum(r["ok"] for r in results), "total": len(results),
            "tests": (q and tests_count) if q is not None else None, "runtime_loc": loc,
            "grade_seconds": round(time.time() - t0, 1), "checks": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--stage", type=int, default=2, choices=(1, 2))
    ap.add_argument("--json")
    ap.add_argument("--only")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("-v", action="store_true", help="show passing checks too")
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None
    out = []
    for run in a.runs:
        g = grade(run, a.stage, only, a.python)
        out.append(g)
        s2 = f"  stage2 {g['stage2']:2d}/{g['stage2_total']}" if g["stage2_total"] else ""
        print(f"{g['passed']:3d}/{g['total']}  stage1 {g['stage1']:2d}/{g['stage1_total']}{s2}  {run}  "
              f"tests={g['tests']} loc={g['runtime_loc']} ({g['grade_seconds']}s)", flush=True)
        for r in g["checks"]:
            if not r["ok"] or a.v:
                print(f"     {'✓' if r['ok'] else '✗'} {r['id']} {r['title']}" + (f" — {r['detail']}" if r["detail"] else ""))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
