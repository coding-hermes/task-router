#!/usr/bin/env bash
# ch:trace producer (part 2) — disambiguate arms A3/A5/A9 from part 1.
set -u
PUB=/home/kara/.hermes/stand-in/readme/task-router/pub-2026-11-17
STR=/home/kara/.hermes/stand-in/readme/task-router/stranger-1717/repo
S2=/home/kara/.hermes/stand-in/readme/task-router/stranger-1717
SHOME=/home/kara/.hermes/stand-in/readme/task-router/home-1717

arm() { echo; echo "########## ARM: $1"; }

arm B1-pyproject-deps "dependency + optional-dependency blocks"
cd "$PUB" || exit 1
sed -n '/^\[project\]/,/^\[project.scripts\]/p' pyproject.toml
echo "-- extras:"; sed -n '/optional-dependencies/,/^\[/p' pyproject.toml

arm B2-pytest-origin "is pytest a CORE dep or did the test extra get installed?" "$STR"
cd "$STR" || exit 1
.venv/bin/pip list 2>/dev/null | grep -iE '^(pytest|duckdb|PyYAML|task-router) '
echo "-- egg-link / direct_url:"; cat .venv/lib/python3*/site-packages/task_router*.dist-info/direct_url.json 2>/dev/null | head -3
echo "-- what a bare 'pip install -e .' would install (dry, from pyproject):"
.venv/bin/python - <<'PY'
import tomllib
d=tomllib.load(open('pyproject.toml','rb'))
p=d['project']
print('core deps:', p.get('dependencies'))
print('optional:', p.get('optional-dependencies'))
PY

arm B3-router-seed-no-duckdb "venv2 (no duckdb, no pytest): router seed" "$S2"
cd "$STR" || exit 1
"$S2/venv2/bin/pip" list 2>/dev/null | grep -iE '^(task-router|duckdb|pytest|PyYAML) ' || true
rm -f registry.json
out=$(timeout 180 "$S2/venv2/bin/router" seed 2>&1); rc=$?
echo "$out" | tail -8; echo "--seed-rc=$rc"
echo "## registry.json created: $(test -f registry.json && echo YES || echo NO)"

arm B4-cli-surface "real help output + context-call discoverability"
cd "$STR" || exit 1
source .venv/bin/activate
timeout 60 router --help 2>&1 | sed -n '1,30p'
echo "-- help rc=$?"
echo "-- context-call in help: $(timeout 60 router --help 2>&1 | grep -c 'context-call')"
echo "-- context-call --help rc:"; timeout 60 router context-call --help >/dev/null 2>&1; echo "rc=$?"
echo "-- README mentions context-call: $(grep -c 'context-call' README.md)"
echo "-- README CLI-table rows for context-call: $(grep -c '^| `router context-call' README.md)"

arm B5-classifier-model "documented classifier model id vs source default"
cd "$PUB" || exit 1
echo "## README recipe line:"; grep -n 'ROUTER_CLASSIFIER_MODEL' README.md
echo "## source default:"; grep -n 'ROUTER_CLASSIFIER_MODEL' scripts/router_classify.py | head -5
echo "## docs/configuration.md row:"; grep -n 'ROUTER_CLASSIFIER_MODEL' docs/configuration.md | head -5

arm B6-freshness-in-readme "version / changelog / release pointers in README"
cd "$PUB" || exit 1
grep -niE 'changelog|release|tagged|version 0\.' README.md | head -10; echo "--rc=$?"
echo "## README 'Badge' count: $(grep -c 'badge' README.md)"
echo "## README line1-8:"; sed -n '1,8p' README.md

arm B7-host-port-state "what is actually listening on documented ports"
ss -ltnp 2>/dev/null | grep -E ':(9092|9093|9391|8642)\b' | sed 's/  */ /g' | head -8 || echo "(none / ss unavailable)"

echo; echo "=== PART2 COMPLETE ==="
