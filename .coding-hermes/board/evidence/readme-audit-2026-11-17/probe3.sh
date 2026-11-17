#!/usr/bin/env bash
# ch:trace producer (part 3) — decisive arms for the documented install contract.
set -u
STR=/home/kara/.hermes/stand-in/readme/task-router/stranger-1717/repo
S2=/home/kara/.hermes/stand-in/readme/task-router/stranger-1717
arm() { echo; echo "########## ARM: $1"; }

arm C1-venv3-documented-install "fresh clone, EXACT README quickstart install (pip install -e . ; pip install duckdb)"
cd "$S2" || exit 1
if [ ! -x "$S2/venv3/bin/python" ]; then timeout 240 python3 -m venv "$S2/venv3" >/dev/null 2>&1; fi
"$S2/venv3/bin/pip" install -q -e "$STR" 2>&1 | tail -3; echo "--install-rc=$?"
"$S2/venv3/bin/pip" install -q duckdb 2>&1 | tail -2; echo "--duckdb-rc=$?"
echo "## venv3 pkgs:"; "$S2/venv3/bin/pip" list 2>/dev/null | grep -iE '^(pytest|duckdb|task-router|PyYAML) ' || true
cd "$STR" || exit 1
echo "## [README:45 claim] router validate BEFORE seed:"
out=$(timeout 120 "$S2/venv3/bin/router" validate 2>&1); rc=$?; echo "$out" | tail -3; echo "--validate-pre-seed-rc=$rc"
echo "## [README:46] router seed:"
out=$(timeout 180 "$S2/venv3/bin/router" seed 2>&1); rc=$?; echo "$out" | tail -4; echo "--seed-rc=$rc"
echo "## registry.json after seed: $(test -f registry.json && echo YES || echo NO)"
echo "## [README:70] the exact documented test command:"
out=$(timeout 180 python3 -m pytest -q tests/ 2>&1); rc=$?
echo "$(echo "$out" | tail -3)"; echo "--pytest-documented-rc=$rc"

arm C2-venv2-no-duckdb "README:73-78 claims — seed 'fails loudly', every other subcommand still works"
cd "$STR" || exit 1
for c in "seed" "status" "validate" "spawn my-project --format json" "estimate --project my-project --tokens-in 1000 --tokens-out 1000"; do
  out=$(timeout 120 "$S2/venv2/bin/router" $c 2>&1); rc=$?
  echo "cmd: router $c | rc=$rc | bytes=$(printf '%s' "$out" | wc -c) | head: $(printf '%s' "$out" | head -2 | tr '\n' ' ' | cut -c1-160)"
done

arm C3-readme-lines "exact README lines that make the install/test claims"
cd /home/kara/.hermes/stand-in/readme/task-router/pub-2026-11-17 || exit 1
grep -n 'pip install\|pytest' README.md | head -12

echo; echo "=== PART3 COMPLETE ==="
