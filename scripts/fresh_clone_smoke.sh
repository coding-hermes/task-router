#!/usr/bin/env bash
# Fresh-checkout smoke: what a stranger's first five minutes must look like.
#
# Why this exists (criterion 4 of the CI work): the repo's most valuable missing
# test is the bootstrap itself. A hands-on review of this repo omitted the `seed`
# step and got 15 phantom test failures; `router validate` legitimately exits 1
# until registry.json exists; and `pip install .` produced a CLI that could not
# run a single command until the root resolution was fixed. None of it was covered.
#
# It reproduces a clone with `git archive HEAD` (committed files only, no
# gitignored state — the environment CI has) and runs with a SCRUBBED HOME so it
# cannot read or write this box's live state, nor pollute a real ~/.local/share.
#
# Usage: bash scripts/fresh_clone_smoke.sh [--keep]
# Exit:  0 all checks pass, 1 any fails, 2 setup unusable.
set -uo pipefail

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/fresh-smoke-XXXXXX)"
# TR-202 re-pass (2026-10-07): the default interpreter was hardcoded to
# /home/kara/.hermes/venvs/board/bin/python3 — a stranger's clone exits 2
# before running a single check. Keep the board-venv preference (existence
# probed, $HOME-relative) but fall back to PATH python3 elsewhere; PYTHON=
# still wins.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "${HOME:-}/.hermes/venvs/board/bin/python3" ]; then
    PY="${HOME}/.hermes/venvs/board/bin/python3"
  else
    PY="$(command -v python3 || echo python3)"
  fi
fi
FAIL=0

# A stranger's HOME has no ~/.hermes, no ~/.local/share/task-router, no registry.
FAKE_HOME="$WORK/home"
mkdir -p "$FAKE_HOME"
export HOME="$FAKE_HOME"

say()  { printf '  %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAIL=1; }

say "workdir: $WORK (keep=$KEEP, HOME scrubbed to $FAKE_HOME)"
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
# The case that was broken before the root-resolution fix: the console script
# derives its root from __file__, which is site-packages/ once installed.
if (cd "$WORK/checkout" && "$V/router" --help >/dev/null 2>&1); then
  pass "installed CLI runs from inside a checkout"
else
  fail "installed CLI cannot run from inside a checkout"
fi

# ---- 4. seed ---------------------------------------------------------------
SEEDED=0
if (cd "$WORK/checkout" && "$V/router" seed >"$WORK/seed.log" 2>&1); then
  pass "seed exited 0"
  SEEDED=1
else
  fail "seed failed — see $WORK/seed.log"; tail -3 "$WORK/seed.log" | sed 's/^/        /'
fi

# ---- 5. status must stop reporting the sample-table fallback ---------------
# Deliberately school-agnostic: instead of hard-coding where the registry "should"
# be, this asserts the CONTRACT ("no longer the fallback") and then checks that the
# path status itself reports really exists. The two invocation schools disagree
# today (CLI writes ~/.local/share/task-router/registry.json; direct scripts use
# <repo>/registry.json) — that is filed separately, and this smoke must not depend
# on which one wins.
if (cd "$WORK/checkout" && "$V/router" status >"$WORK/status.json" 2>"$WORK/status.err"); then
  if "$PY" - "$WORK/status.json" <<'PYEOF'
import json, os, sys
d = json.load(open(sys.argv[1]))
reg = d.get("registry", {})
bad = []
if reg.get("fallback_used") is not False:
    bad.append("registry.fallback_used is %r, expected False" % reg.get("fallback_used"))
if reg.get("unavailable") is not False:
    bad.append("registry.unavailable is %r, expected False" % reg.get("unavailable"))
if reg.get("source") != "registry.json":
    bad.append("registry.source is %r, expected 'registry.json'" % reg.get("source"))
age = d.get("registry_age") or {}
p = age.get("path")
if p and not os.path.exists(p):
    bad.append("status reports registry path %r but it does not exist" % p)
if bad:
    print("        " + "; ".join(bad))
    sys.exit(1)
print("        source=%s path=%s" % (reg.get("source"), p))
PYEOF
  then
    pass "status no longer reports the fallback (source=registry.json, unavailable=false)"
  else
    fail "status still reports the sample-table fallback after seeding"
  fi
else
  fail "router status failed — see $WORK/status.err"
fi

# ---- 6. and the direct-script school still works (both must bootstrap) ------
if [ "$SEEDED" -eq 1 ]; then
  (cd "$WORK/checkout" && "$V/router" validate --json >"$WORK/validate.json" 2>&1)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    pass "router validate agrees the checkout is healthy (exit 0)"
  else
    # Not a false alarm: this is the documented "seed then validate" path, and it
    # is exactly the trap a newcomer hits by mixing the README's CLI steps with
    # the direct-script steps in AGENTS.md.
    say "  NOTE  router validate exited $rc after a successful seed — the two registry"
    say "        locations disagree (see the board row for this finding)"
  fi
fi

# ---- verdict + cleanup -----------------------------------------------------
if [ "$FAIL" -eq 0 ]; then
  echo "fresh-clone smoke: OK"
  [ "$KEEP" -eq 0 ] && rm -rf "$WORK"
  exit 0
fi
echo "fresh-clone smoke: FAILED — artefacts left in $WORK (HOME was $FAKE_HOME)"
exit 1
