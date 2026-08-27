#!/usr/bin/env bash
# GitReins guard test command for task-router.
# - No tests dir / pytest not importable -> SKIP (exit 0), do not block commits.
# - Tests present + pytest importable -> run smoke suite; failures exit 1.
# Bare `pytest` is NOT used (console-script entry point does not add cwd to
# sys.path — see gitreins guard-bare-pytest-syspath pitfall).
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if [ ! -d tests ]; then
  echo "SKIP: no tests/ dir — guard tests deferred."
  exit 0
fi
if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  echo "SKIP: pytest not importable in $PY — guard tests deferred."
  exit 0
fi
if ! "$PY" -c "import duckdb" >/dev/null 2>&1; then
  echo "SKIP: duckdb not importable in $PY — smoke suite self-skips; guard tests deferred."
fi

"$PY" -m pytest -q tests/ -x
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "ERROR: pytest failed (exit $RC) — see output above."
  exit 1
fi
