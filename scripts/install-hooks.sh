#!/usr/bin/env bash
# Install this repo's git hooks. Idempotent; safe to re-run.
#
# Why an installer instead of just committing .githooks/: git does not honour a
# hooks directory until core.hooksPath is set, and that setting is per-clone.
# A fresh clone therefore has the gate present but inactive until this runs —
# which is why every fleet agent and worktree should call it once at setup.
set -euo pipefail
cd "$(dirname "$0")/.."

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true
chmod +x scripts/ci_gate_check.py 2>/dev/null || true

echo "hooks installed: core.hooksPath=$(git config core.hooksPath)"
echo "active hooks:    $(ls .githooks 2>/dev/null | tr '\n' ' ')"
echo
echo "What it does: a push that UPDATES 'main' is refused while main's newest"
echo "completed CI run is not success. Override loudly with ALLOW_RED_PUSH=1."
echo "Check the state by hand: python3 scripts/ci_gate_check.py --require-green"
