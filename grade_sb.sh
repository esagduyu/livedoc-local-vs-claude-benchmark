#!/usr/bin/env bash
# Run the grader in a sandbox: own PID namespace (agent tests that pgrep/kill only see the grader own processes),
# private /tmp, read-only everything except logs/. Usage: grade_sb.sh <grade.py args...>
B="$HOME/llama-bench-v3"
exec sudo -n bwrap --ro-bind / / --dev /dev --proc /proc --perms 1777 --tmpfs /tmp --bind "$B/logs" "$B/logs" \
  --unshare-pid --die-with-parent --chdir "$B" \
  setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups --no-new-privs \
  "$HOME/.venvs/livedoc/bin/python" "$B/grader/grade.py" "$@"
