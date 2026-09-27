#!/usr/bin/env bash
# Fresh-checkout smoke: what a stranger's first five minutes must look like.
#
# Why this exists (criterion 4 of the CI work): the repo's most valuable missing
# test is the bootstrap itself. A review of this repo omitted the `seed` step and
# got 15 phantom test failures; `router validate` legitimately exits 1 until
# registry.json exists; and `pip install .` produced a CLI that could not run a
# single command until the root resolution was fixed. None of that was covered.
#
# It deliberately does NOT use the working tree as a checkout: `git archive HEAD`
# reproduces exactly what a clone contains (committed files only, no gitignored
# state), which is the environment CI actually has.
#
# Usage: bash scripts/fresh_clone_smoke.sh [--keep]
# Exit:  0 all five assertions pass, 1 any fails, 2 setup unusable.
set -uo pipefail

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/fresh-smoke-XXXXXX)"
PY="${PYTHON:-/home/kara/.hermes/venvs/board/bin/python3}"
FAIL=0

say()  { printf '  %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAIL=1; }

say "workdir: $WORK (keep=$KEEP)"
say "source : $HERE @ $(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo '?')"

# ---- 1. a clone, byte-for-byte (committed files only) ----------------------
mkdir -p "$WORK/checkout"
if ! git -C "$HERE" archive HEAD | tar -x -C "$WORK/checkout"; then
  say "ERROR: git archive failed — cannot simulate a clone"; exit 2
fi
if [ -e "$WORK/checkout/registry.json" ]; then
  fail "registry.json is present in a fresh checkout (it must be gitignored)"
else
  pass "checkout has no registry.json (as a stranger would find it)"
fi

# ---- 2. install into a fresh venv ------------------------------------------
if ! "$PY" -m venv "$WORK/venv" >/dev/null 2>&1; then
  say "ERROR: venv creation failed with $PY"; exit 2
fi
V="$WORK/venv/bin"
if ! "$V/pip" install -q --disable-pip-version-check "$WORK/checkout" duckdb >"$WORK/pip.log" 2>&1; then
  fail "pip install failed — see $WORK/pip.log"; tail -3 "$WORK/pip.log" | sed 's/^/        /'
else
  pass "installed into a fresh venv ($(du -sh "$WORK/venv" | cut -f1))"
fi

# ---- 3. the installed CLI works FROM INSIDE a checkout ----------------------
# This is the case that was broken before the root-resolution fix: the console
# script derives its root from __file__, which is site-packages/ once installed.
if (cd "$WORK/checkout" && "$V/router" --help >/dev/null 2>&1); then
  pass "installed CLI runs from inside a checkout"
else
  fail "installed CLI cannot run from inside a checkout"
fi

# ---- 4. seed ---------------------------------------------------------------
if (cd "$WORK/checkout" && "$V/router" seed >"$WORK/seed.log" 2>&1); then
  if [ -s "$WORK/checkout/registry.json" ]; then
    pass "seed produced registry.json ($(du -h "$WORK/checkout/registry.json" | cut -f1))"
  else
    fail "seed exited 0 but wrote no registry.json"
  fi
else
  fail "seed failed — see $WORK/seed.log"; tail -3 "$WORK/seed.log" | sed 's/^/        /'
fi

# ---- 5. status must stop reporting the fallback ----------------------------
if (cd "$WORK/checkout" && "$V/router" status >"$WORK/status.json" 2>"$WORK/status.err"); then
  if "$PY" - "$WORK/status.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
reg = d.get("registry", {})
bad = []
if reg.get("fallback_used") is not False:
    bad.append("registry.fallback_used is %r, expected False" % reg.get("fallback_used"))
if reg.get("unavailable") is not False:
    bad.append("registry.unavailable is %r, expected False" % reg.get("unavailable"))
if reg.get("source") != "registry.json":
    bad.append("registry.source is %r, expected 'registry.json'" % reg.get("source"))
if bad:
    print("        " + "; ".join(bad))
    sys.exit(1)
PYEOF
  then
    pass "status reports source=registry.json, fallback_used=false, unavailable=false"
  else
    fail "status still reports the sample-table fallback after seeding"
  fi
else
  fail "router status failed — see $WORK/status.err"
fi

# ---- verdict + cleanup -----------------------------------------------------
if [ "$FAIL" -eq 0 ]; then
  echo "fresh-clone smoke: OK (5 checks)"
  [ "$KEEP" -eq 0 ] && rm -rf "$WORK"
  exit 0
fi
echo "fresh-clone smoke: FAILED — artefacts left in $WORK"
exit 1
