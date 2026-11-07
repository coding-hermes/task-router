#!/usr/bin/env bash
# Install this repo's git hooks. Idempotent; safe to re-run.
#
# Why an installer instead of just committing .githooks/: git does not honour a
# hooks directory until core.hooksPath is set, and that setting is per-clone.
# A fresh clone therefore has the gate present but inactive until this runs —
# which is why every fleet agent and worktree should call it once at setup.
#
# IMPORTANT: setting core.hooksPath makes .git/hooks/ INERT. Everything git
# actually runs has to live in .githooks/. `git rev-parse --git-path
# hooks/pre-commit` must answer `.githooks/pre-commit`.
set -euo pipefail
cd "$(dirname "$0")/.."

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true
chmod +x scripts/ci_gate_check.py 2>/dev/null || true
chmod +x scripts/model_data_commit_gate.py 2>/dev/null || true

echo "hooks installed: core.hooksPath=$(git config core.hooksPath)"
echo "active hooks:    $(ls .githooks 2>/dev/null | tr '\n' ' ')"
echo
echo "What they do:"
echo "  pre-commit  refuses a commit that adds a live lane with no capability"
echo "              measurement and no stated reason, or that would raise the"
echo "              TR-369 usability defect count. Override loudly with"
echo "              ALLOW_UNUSABLE_DATA=1."
echo "  pre-push    refuses a push that UPDATES 'main' while main's newest"
echo "              completed CI run is not success. Override loudly with"
echo "              ALLOW_RED_PUSH=1."
echo
echo "Check the state by hand:"
echo "  python3 scripts/model_data_commit_gate.py --explain"
echo "  python3 scripts/ci_gate_check.py --require-green"

# Fail loudly if the resolution is wrong: a hook git will never execute is
# worse than no hook, because it reads as a gate in code review.
resolved="$(git rev-parse --git-path hooks/pre-commit)"
if [ "$resolved" != ".githooks/pre-commit" ]; then
	echo "ERROR: git resolves pre-commit to '$resolved', expected .githooks/pre-commit" >&2
	exit 1
fi
if [ ! -x "$resolved" ]; then
	echo "ERROR: $resolved is missing or not executable" >&2
	exit 1
fi
echo "pre-commit resolves correctly: $resolved"
