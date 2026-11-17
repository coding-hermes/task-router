#!/usr/bin/env bash
# Re-fire verification producer for the task-router README lane, tick 2026-11-17-02-40-53.
# Read-only. Measures only the two rows this tick filed plus the README identity of the
# published tip. No board readback, no marker census, no writes outside the scratch home.
set -uo pipefail
WT="$(cd "$(dirname "$0")/../../../.." && pwd)"
PY=/home/kara/task-router/.venv/bin/python
export TASK_ROUTER_HOME=/home/kara/.hermes/stand-in/readme/task-router/home-1117-0240/refire
export ROUTER_STATE_DIR="$TASK_ROUTER_HOME/state"

arm() { printf '\n########## ARM: %s\n' "$1"; }
run() { printf '## cmd: %s\n' "$*"; "$@" 2>&1; printf '## rc=%s\n' "$?"; }

arm V1-identity
echo "## worktree: $WT"
git -C "$WT" rev-parse HEAD
git -C "$WT" rev-parse HEAD:README.md
sha256sum "$WT/README.md"
git -C "$WT" rev-list --count HEAD..origin/main
wc -l < "$WT/README.md"
grep -c '^## ' "$WT/README.md"
grep -c '^#\{1,6\} ' "$WT/README.md"

arm V2-maintain-row
sed -n '660p' "$WT/README.md"
run "$PY" "$WT/scripts/router_maintain.py" --dry-run
run "$PY" "$WT/scripts/router_maintain.py" reprice --dry-run

arm V3-toc-vs-h2
grep -n '^## ' "$WT/README.md"
echo "## TOC bullet count (lines 9-27):"
sed -n '9,27p' "$WT/README.md" | grep -c '^- \['
echo "## License line:"
grep -n '^## License' "$WT/README.md"
echo "## License present in TOC bullets:"
sed -n '9,27p' "$WT/README.md" | grep -c '^- \[License\]'

echo
echo "=== REFIRE VERIFICATION COMPLETE ==="
