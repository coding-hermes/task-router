#!/usr/bin/env bash
# GitReins guard test command for task-router.
# - No tests dir / pytest not importable -> SKIP (exit 0), do not block commits.
# - Tests present + pytest importable -> run smoke suite; failures exit 1.
# Bare `pytest` is NOT used (console-script entry point does not add cwd to
# sys.path — see gitreins guard-bare-pytest-syspath pitfall).
#
# TR-175 (2026-10): the suite outgrew this script and its budgets.
#   * The suite grew to 1443 collected tests. Measured full runs: 917s in CI
#     (ubuntu-latest, 2026-10-01, run 36953390462) and 2045-2244s on this box
#     (loadavg 26-44). The old budgets (test_timeout 900 / hook_timeout 700)
#     were sized for the ~220s suite of 2026-09-17, so the gitreins hook clock
#     expired mid-suite and the guard FAILED OPEN ("commit allowed to
#     proceed") — commits landed on trees nobody graded.
#   * The script now enforces its OWN clock (coreutils `timeout`,
#     GUARD_SUITE_TIMEOUT, default 2400s) so a run that would outlive any
#     outer budget exits 1 LOUDLY with a readable message instead of being
#     killed into a fail-open. Budget ladder (see .gitreins/config.yaml):
#     script 2400s < gitreins test_timeout 3000s < gitreins hook_timeout
#     3600s — the innermost clock must always fire first.
#   * `-x` is gone: stop-at-first-failure hid how many tests a change broke;
#     the budget lives in `timeout`, not in the failure count.
#   * The suite shape is deliberately CI's shape — the full tests/ tree,
#     exactly what .github/workflows/ci.yml runs (`pytest -q tests/`), so a
#     green guard means the same thing a green CI run means. There is no
#     subset mode: this repo's law (f792a7b) is that the guard grades the
#     tree conclusively, and diff mode cannot narrow a custom command
#     anyway (gitreins runs non-pytest test_commands verbatim).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

#: Interpreter: prefer the fleet board venv, fall back to PATH python3 so the
#: script also works in CI (no /home/kara there) and on other hosts. An
#: explicit PYTHON= always wins.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x /home/kara/.hermes/venvs/board/bin/python3 ] \
     && /home/kara/.hermes/venvs/board/bin/python3 -c "import pytest" >/dev/null 2>&1; then
    PY=/home/kara/.hermes/venvs/board/bin/python3
  else
    PY="$(command -v python3 || echo python3)"
  fi
fi
#: Own-clock suite budget (seconds). Sized for the worst load this box
#: actually sees (conftest.py doctrine), not the idle case: 2045-2244s
#: measured under fleet load + margin. Override with GUARD_SUITE_TIMEOUT=<seconds>;
#: if you raise it past gitreins' test_timeout, raise the config too.
GUARD_SUITE_TIMEOUT="${GUARD_SUITE_TIMEOUT:-2400}"
if ! [[ "$GUARD_SUITE_TIMEOUT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: GUARD_SUITE_TIMEOUT='${GUARD_SUITE_TIMEOUT}' is not a positive integer."
  exit 2
fi
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

timeout --kill-after=30s "$GUARD_SUITE_TIMEOUT" "$PY" -m pytest -q tests/
RC=$?
if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
  echo "ERROR: guard suite exceeded GUARD_SUITE_TIMEOUT=${GUARD_SUITE_TIMEOUT}s (exit $RC)."
  echo "       The run hit its OWN budget instead of an outer kill — treat this"
  echo "       as a failure, not a timeout to retry through. If the box is"
  echo "       legitimately slower now, raise GUARD_SUITE_TIMEOUT here AND"
  echo "       test_timeout + hook_timeout in .gitreins/config.yaml together."
  exit 1
fi
if [ "$RC" -ne 0 ]; then
  echo "ERROR: pytest failed (exit $RC) — see output above."
  exit 1
fi

# ---------------------------------------------------------------------------
# CI gate (see docs/ci-gate.md). This suite runs WITH this box's live state
# present; CI runs it with none, so a green run here is not evidence about CI.
# Three consecutive red CI runs on main landed anyway because nothing looked.
# Skips (announced) when gh is missing/unauthenticated/offline — a network blip
# must never wedge the fleet. Override loudly with CI_GATE_BYPASS=1.
# ---------------------------------------------------------------------------
if [ -f scripts/ci_gate_check.py ]; then
  "$PY" scripts/ci_gate_check.py || {
    echo "ERROR: CI is not green for the branch tip — do not land more work."
    echo "       Fix it, or proceed deliberately with CI_GATE_BYPASS=1."
    exit 1
  }
fi
