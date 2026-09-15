#!/usr/bin/env bash
# GitReins guard test command for task-router.
# - No tests dir / pytest not importable -> SKIP (exit 0), do not block commits.
# - Tests present + pytest importable -> run smoke suite; failures exit 1.
# Bare `pytest` is NOT used (console-script entry point does not add cwd to
# sys.path — see gitreins guard-bare-pytest-syspath pitfall).
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-/home/kara/.hermes/venvs/board/bin/python3}"
if [ ! -d tests ]; then
  echo "SKIP: no tests/ dir — guard tests deferred."
  exit 0
fi
if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  echo "SKIP: pytest not importable in $PY — guard tests deferred."
  exit 0
fi
# NOTE (TR-009/010 + TR-047): the suite is mostly pure-JSON, but CI installs
# duckdb: the seed subprocess and the duckdb-gated tests
# (tests/test_seed_ns_guard.py) need it. Tests guard duckdb via
# importorskip/skipif, so a bare pytest run without duckdb skips cleanly
# instead of erroring.

"$PY" -m pytest -q tests/ -x
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "ERROR: pytest failed (exit $RC) — see output above."
  exit 1
fi
