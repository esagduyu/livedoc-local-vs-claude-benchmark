#!/usr/bin/env bash
# qwen-r2 was SIGKILLed at 145 s by an agent test (pgrep -f server.py) run by the unsandboxed grader on the host.
# Rerun it once the main runner (pid given as $1) has finished.
while kill -0 "$1" 2>/dev/null; do sleep 30; done
V3_SOURCE_ONLY=1 source "$HOME/llama-bench-v3/run_v3.sh"
log "=== rerun qwen-r2 start $(date -Is) (original killed by grader; see logs/invalid)"
local_run qwen qwen-r2 ""
sudo -n systemctl stop llama-qwen 2>/dev/null
log "RERUN_QWEN_R2_DONE $(date -Is)"
