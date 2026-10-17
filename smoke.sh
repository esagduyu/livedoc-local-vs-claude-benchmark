#!/usr/bin/env bash
# Sandbox smoke test: one tiny pi+Qwen turn plus a -c continuation, and one tiny Claude Haiku turn plus --resume.
V3_SOURCE_ONLY=1 source "$HOME/llama-bench-v3/run_v3.sh"
log() { echo "$*" | tee -a "$B/logs/smoke.log"; }
T=300
run=smoke-pi; fresh "$run"; w="$B/work/$run"; st="$B/state/$run"
server qwen || exit 1
P=("${PI_ISO[@]}" --provider llama-qwen --model qwen3.6-35b-a3b --session-dir /home/user/.pi-bench/pisessions)
sb_pi "$w" "$st" "${P[@]}" "Create hello.py that prints hi, run it with python, and also run: ls /home/user; ls /home/user/llama-bench-v3 (report whether it exists). Be brief."
timed "$run:s1" $T "$B/logs/$run-s1.jsonl" "${CMD[@]}"
sb_pi "$w" "$st" "${P[@]}" -c "What file did you create earlier? Append a line printing bye to it and run it."
timed "$run:s2" $T "$B/logs/$run-s2.jsonl" "${CMD[@]}"
ls -la "$w"; find "$st" -maxdepth 3 | head; tail -c 600 "$B/logs/$run-s1.jsonl"; echo; tail -c 600 "$B/logs/$run-s2.jsonl"; echo; cat "$B/logs/$run-s1.stderr" "$B/logs/$run-s2.stderr"
sudo -n systemctl stop llama-qwen
run=smoke-claude; fresh "$run"; w="$B/work/$run"; st="$B/state/$run"; sid=$(cat /proc/sys/kernel/random/uuid)
sb_claude "$w" "$st" -p --model claude-haiku-5-5 "${CLAUDE_ISO[@]}" --max-budget-usd 0.5 --session-id "$sid" \
  "Create hello.py that prints hi and run it with python. Also run: ls /home/user /home/user/llama-bench-v3; cat ~/.claude/CLAUDE.md | head -3. Be brief."
timed "$run:s1" $T "$B/logs/$run-s1.jsonl" "${CMD[@]}"
sb_claude "$w" "$st" -p --model claude-haiku-5-5 "${CLAUDE_ISO[@]}" --max-budget-usd 0.5 --resume "$sid" "What file did you create earlier? Answer in one line."
timed "$run:s2" $T "$B/logs/$run-s2.jsonl" "${CMD[@]}"
ls -la "$w"; find "$st" -maxdepth 3 | head
for f in s1 s2; do grep '"type":"result"' "$B/logs/$run-$f.jsonl" | python3 -c 'import sys,json;[print(json.loads(l).get("result","")[:500], json.loads(l).get("total_cost_usd")) for l in sys.stdin]'; cat "$B/logs/$run-$f.stderr"; done
