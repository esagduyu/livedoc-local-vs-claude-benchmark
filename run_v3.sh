#!/usr/bin/env bash
# Livedoc benchmark (v3) runner, on the GPU host.
# Every agent runs in a bubblewrap sandbox. It sees only its own working directory (mounted at /home/user/work),
# the read-only toolchain (Python venv, Node/pi or Claude Code), and its own agent state.
# Other runs, the grader and the reference solution are invisible.
#
# Arms (3 reps each):
#   haiku-rN               Claude Haiku 5.5 builds it (Claude Code)
#   opus-rN                Claude Opus 5.5 builds it (Claude Code)
#   opusplan-rN            Opus 5.5 writes TODO.md with the plan-first skill (planning only; shared by the two arms below)
#   nemotron-rN            Nemotron 3.5 Lightning builds it (pi)
#   opusplan-nemotron-rN   Nemotron executes opusplan-rN's TODO.md
#   qwen-rN                Qwen3.6-35B-A3B builds it (pi)
#   opusplan-qwen-rN       Qwen executes opusplan-rN's TODO.md
# Every run has two stages in one agent session: stage 1 = SPEC.md, stage 2 = CHANGE_REQUEST.md (unplanned for all arms).
# Usage: run_v3.sh <group>...   groups: plans haiku opus nemotron qwen   (REPS=3, T1=3600, T2=1800)
set -u
B="$HOME/llama-bench-v3"
VENV="$HOME/.venvs/livedoc"
NODE_BIN="$HOME/.nvm/versions/node/v22.22.2/bin"
CLAUDE_BIN="$HOME/.local/share/claude/versions/2.1.300"
REPS="${REPS:-3}"
T1="${T1:-3600}"
T2="${T2:-1800}"
W=/home/user/work
mkdir -p "$B/work" "$B/runs" "$B/logs" "$B/state" "$B/opus-plans"

SPEC_PROMPT="$(cat "$B/spec/SPEC.md")

Work in the current directory (SPEC.md is there too). Run \`pytest tests/ -q\` and make sure it passes before you finish."
CR_PROMPT="$(cat "$B/spec/CHANGE_REQUEST.md")

This change request is also in CHANGE_REQUEST.md. Run \`pytest tests/ -q\` and make sure it passes before you finish."
PLAN_PROMPT="Read SPEC.md. Apply the plan-first skill. Skip Phase 2 questions; make reasonable assumptions. Pause after writing TODO.md."
EXEC_FRESH_PROMPT="Read SPEC.md and TODO.md. The plan in TODO.md has been reviewed and approved: YES, the plan looks correct. Execute every task in TODO.md in order, then run pytest tests/ -q."

# Ubuntu 24.04's AppArmor blocks unprivileged user namespaces, so root (sudo) builds the sandbox and
# setpriv drops back to this user with no_new_privs (setuid binaries such as sudo don't work inside).
SB=(sudo -n bwrap --ro-bind / / --dev /dev --proc /proc --perms 1777 --tmpfs /tmp --perms 1777 --tmpfs /var/tmp
    --perms 1777 --tmpfs /home/user --perms 0755 --dir /home/user/.venvs --perms 0755 --dir /home/user/.local
    --perms 0755 --dir /home/user/.local/bin
    --ro-bind "$HOME/.nvm" "$HOME/.nvm" --ro-bind "$VENV" "$VENV"
    --setenv HOME /home/user --setenv PATH "$VENV/bin:$NODE_BIN:/usr/local/bin:/usr/bin:/bin"
    --setenv LANG C.UTF-8 --setenv USER user --unshare-pid --die-with-parent)
DROP=(setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups --no-new-privs)

sb_pi() {      # sb_pi <workdir> <statedir> <pi args...>  -> sets CMD
  local w="$1" st="$2"; shift 2
  CMD=("${SB[@]}" --bind "$w" "$W" --bind "$st" /home/user/.pi-bench --setenv PI_CODING_AGENT_DIR /home/user/.pi-bench \
    --chdir "$W" "${DROP[@]}" pi "$@")
}

sb_claude() {  # sb_claude <workdir> <statedir> <claude args...>  -> sets CMD
  local w="$1" st="$2"; shift 2
  CMD=("${SB[@]}" --ro-bind "$CLAUDE_BIN" "$HOME/.local/bin/claude" \
    --bind "$HOME/.claude" "$HOME/.claude" --bind "$HOME/.claude.json" "$HOME/.claude.json" \
    --ro-bind "$B/empty.md" "$HOME/.claude/CLAUDE.md" --ro-bind "$B/empty.json" "$HOME/.claude/settings.json" \
    --ro-bind "$B/empty.json" "$HOME/.claude/settings.local.json" \
    --tmpfs "$HOME/.claude/skills" --tmpfs "$HOME/.claude/hooks" --tmpfs "$HOME/.claude/plugins" \
    --bind "$st/projects" "$HOME/.claude/projects" --bind "$st/sessions" "$HOME/.claude/sessions" \
    --bind "$w" "$W" --chdir "$W" "${DROP[@]}" "$HOME/.local/bin/claude" "$@")
}

CLAUDE_ISO=(--setting-sources project,local --strict-mcp-config --disable-slash-commands
            --allowedTools "Read,Write,Edit,Bash,Glob,Grep" --output-format stream-json --verbose)
PI_ISO=(-p --mode json --no-extensions --no-context-files --no-skills --no-prompt-templates --thinking off)

log() { echo "$*" | tee -a "$B/logs/runner.log"; }

timed() {      # timed <label> <timeout> <logfile> <cmd...>
  local label="$1" to="$2" out="$3"; shift 3
  local t0; t0=$(date +%s.%N)
  timeout --foreground "$to" "$@" </dev/null > "$out" 2> "${out%.jsonl}.stderr"
  local rc=$?
  log "$(printf '%s exit=%s wall=%.1fs' "$label" "$rc" "$(echo "$(date +%s.%N) - $t0" | bc)")"
}

snapshot() {   # snapshot <run> <stage>
  local src="$B/work/$1" dst="$B/runs/$1/$2"
  rm -rf "$dst"; mkdir -p "$dst"
  rsync -a --exclude __pycache__ --exclude .pytest_cache "$src/" "$dst/"
}

fresh() {      # fresh <run>  -> workdir with SPEC.md, fresh state dir
  local w="$B/work/$1" st="$B/state/$1"
  rm -rf "$w" "$st" "$B/runs/$1"; mkdir -p "$w" "$st/projects" "$st/sessions" "$st/pisessions"
  cp "$B/spec/SPEC.md" "$w/"
  cp "$B/config/pi-models.json" "$st/models.json"; echo '{}' > "$st/settings.json"
  cp "$B/skills/plan-first.md" "$st/plan-first.md"
}

server() {     # server nemotron|qwen : (re)start the matching llama-server unit with an empty KV cache
  local unit="llama-$1" other
  [ "$1" = nemotron ] && other=llama-qwen || other=llama-nemotron
  sudo -n systemctl stop "$other" llama-server 2>/dev/null
  if systemctl is-active -q "$unit"; then
    sudo -n systemctl restart "$unit"
  else
    sudo -n systemctl reset-failed "$unit" 2>/dev/null
    if [ "$1" = nemotron ]; then
      sudo -n systemd-run --unit="$unit" --uid="$(id -un)" --gid="$(id -gn)" --property=Restart=no "$HOME/llama.cpp/build/bin/llama-server" \
        --model "$HOME/llm-models/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-IQ4_XS.gguf" --alias nemotron-3.5-lightning \
        --host 127.0.0.1 --port 8001 -c 131072 -n 32768 --no-context-shift --temp 0.2 --top-p 0.95 \
        --fit on -fa on -ctk q8_0 -ctv q8_0 --jinja --reasoning off --ctx-checkpoints 32 --checkpoint-min-step 0 --metrics
    else
      sudo -n systemd-run --unit="$unit" --uid="$(id -un)" --gid="$(id -gn)" --property=Restart=no "$HOME/llama.cpp/build/bin/llama-server" \
        --model "$HOME/llm-models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf" --alias qwen3.6-35b-a3b \
        --host 127.0.0.1 --port 8001 -c 131072 -n 32768 --no-context-shift \
        --temp 0.6 --top-p 0.95 --top-k 20 --repeat-penalty 1.00 --presence-penalty 0.00 \
        --fit on -fa on -ctk q8_0 -ctv q8_0 --jinja --reasoning off --ctx-checkpoints 32 --checkpoint-min-step 0 --metrics
    fi
  fi
  for i in $(seq 1 90); do curl -sf http://127.0.0.1:8001/health >/dev/null && return 0; sleep 2; done
  log "server $1 failed to come up"; return 1
}

claude_run() {   # claude_run <model-id> <run> <budget1> <budget2>
  local model="$1" run="$2" st="$B/state/$2" w="$B/work/$2" sid
  fresh "$run"; sid=$(cat /proc/sys/kernel/random/uuid)
  sb_claude "$w" "$st" -p --model "$model" "${CLAUDE_ISO[@]}" \
    --max-budget-usd "$3" --session-id "$sid" "$SPEC_PROMPT"
  timed "$run:s1" "$T1" "$B/logs/$run-s1.jsonl" "${CMD[@]}"
  snapshot "$run" stage1
  cp "$B/spec/CHANGE_REQUEST.md" "$w/"
  sb_claude "$w" "$st" -p --model "$model" "${CLAUDE_ISO[@]}" \
    --max-budget-usd "$4" --resume "$sid" "$CR_PROMPT"
  timed "$run:s2" "$T2" "$B/logs/$run-s2.jsonl" "${CMD[@]}"
  snapshot "$run" final
}

local_run() {    # local_run nemotron|qwen <run> <plan rep or "">
  local m="$1" run="$2" n="$3" st="$B/state/$2" w="$B/work/$2" prov model
  if [ "$m" = nemotron ]; then prov=llama-nemotron model=nemotron-3.5-lightning; else prov=llama-qwen model=qwen3.6-35b-a3b; fi
  fresh "$run"
  local P=("${PI_ISO[@]}" --provider "$prov" --model "$model" --session-dir /home/user/.pi-bench/pisessions)
  server "$m" || return
  if [ -n "$n" ]; then
    [ -f "$B/opus-plans/opusplan-r$n/TODO.md" ] || { log "missing opus plan r$n; skipping $run"; return; }
    cp "$B/opus-plans/opusplan-r$n/TODO.md" "$w/"
    sb_pi "$w" "$st" "${P[@]}" --skill /home/user/.pi-bench/plan-first.md "$EXEC_FRESH_PROMPT"
    timed "$run:s1" "$T1" "$B/logs/$run-s1.jsonl" "${CMD[@]}"
  else
    sb_pi "$w" "$st" "${P[@]}" "$SPEC_PROMPT"
    timed "$run:s1" "$T1" "$B/logs/$run-s1.jsonl" "${CMD[@]}"
  fi
  snapshot "$run" stage1
  cp "$B/spec/CHANGE_REQUEST.md" "$w/"
  if [ -n "$n" ]; then
    sb_pi "$w" "$st" "${P[@]}" -c --skill /home/user/.pi-bench/plan-first.md "$CR_PROMPT"
    timed "$run:s2" "$T2" "$B/logs/$run-s2.jsonl" "${CMD[@]}"
  else
    sb_pi "$w" "$st" "${P[@]}" -c "$CR_PROMPT"
    timed "$run:s2" "$T2" "$B/logs/$run-s2.jsonl" "${CMD[@]}"
  fi
  snapshot "$run" final
}

[ -n "${V3_SOURCE_ONLY:-}" ] && return 0   # smoke tests source the functions only

for group in "$@"; do
  log "=== group $group start $(date -Is)"
  case "$group" in
    plans)
      for n in $(seq 1 "$REPS"); do
        run="opusplan-r$n"; fresh "$run"
        sb_claude "$B/work/$run" "$B/state/$run" -p --model claude-opus-5-5 \
          --setting-sources project,local --strict-mcp-config --disable-slash-commands --no-session-persistence \
          --allowedTools "Read,Write,Glob,Grep" --output-format stream-json --verbose --max-budget-usd 5 \
          --append-system-prompt "$(cat "$B/skills/plan-first.md")" "$PLAN_PROMPT"
        timed "$run" 900 "$B/logs/$run.jsonl" "${CMD[@]}"
        mkdir -p "$B/opus-plans/$run"; cp "$B/work/$run/TODO.md" "$B/opus-plans/$run/" 2>/dev/null || log "$run wrote no TODO.md"
      done ;;
    haiku) for n in $(seq 1 "$REPS"); do claude_run claude-haiku-5-5 "haiku-r$n" 10 5; done ;;
    opus)  for n in $(seq 1 "$REPS"); do claude_run claude-opus-5-5 "opus-r$n" 30 15; done ;;
    nemotron) for n in $(seq 1 "$REPS"); do local_run nemotron "nemotron-r$n" ""; local_run nemotron "opusplan-nemotron-r$n" "$n"; done ;;
    qwen)     for n in $(seq 1 "$REPS"); do local_run qwen "qwen-r$n" ""; local_run qwen "opusplan-qwen-r$n" "$n"; done ;;
    *) log "unknown group $group" ;;
  esac
  log "=== group $group done $(date -Is)"
done
sudo -n systemctl stop llama-nemotron llama-qwen 2>/dev/null
log "ALL_V3_GROUPS_DONE $*"
