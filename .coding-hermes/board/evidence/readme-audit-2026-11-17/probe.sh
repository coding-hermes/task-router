#!/usr/bin/env bash
# ch:trace producer — README health sweep, task-router, published tip.
# Re-runnable: bash probe.sh  (records command, cwd, rc, output per arm)
# Arms are independent; a failing arm does not abort the run.
set -u
PUB=/home/kara/.hermes/stand-in/readme/task-router/pub-2026-11-17
STR=/home/kara/.hermes/stand-in/readme/task-router/stranger-1717/repo
SHOME=/home/kara/.hermes/stand-in/readme/task-router/home-1717
export HOME="$SHOME"

arm() { echo; echo "########## ARM: $1"; echo "## cmd: $2"; echo "## cwd: ${3:-$STR}"; }

echo "=== SWEEP task-router README 2026-11-17 ==="
echo "## date: $(date -Is)"

arm A1-identity "git rev-parse HEAD; git rev-parse origin/main; git rev-parse HEAD:README.md" "$PUB"
cd "$PUB" || exit 1
git rev-parse HEAD; git rev-parse origin/main; git rev-parse origin/main:README.md
sha256sum README.md; git cat-file -p origin/main:README.md | sha256sum
echo "-- rc=$?"

arm A2-release-boundary "gh release view; git ls-remote --tags; CHANGELOG head; pyproject version" "$PUB"
timeout 60 gh release list --repo coding-hermes/task-router 2>&1 | head -5; echo "--gh-rc=$?"
git ls-remote --tags origin 2>&1 | head -5; echo "--tags-rc=$?"
grep -n '^## ' CHANGELOG.md | head -4
grep -nE '^version|^name|requires-python' pyproject.toml | head -4
git log -1 --pretty='%h %ad %s' --date=short -- README.md

arm A3-pytest-verbatim "python3 -m pytest -q tests/   # README line 70, after the documented install only" "$STR"
cd "$STR" || exit 1
source .venv/bin/activate
python3 -c "import sys;print('python',sys.version.split()[0])"
pip list 2>/dev/null | grep -iE '^(pytest|duckdb|pyyaml) ' || echo "(-- no pytest/duckdb/pyyaml in venv)"
timeout 180 python3 -m pytest -q tests/ 2>&1 | tail -6; echo "--pytest-rc=${PIPESTATUS[0]}"

arm A4-make-schemas "make schemas   # Makefile target" "$STR"
timeout 180 make schemas 2>&1 | tail -8; echo "--make-schemas-rc=${PIPESTATUS[0]}"
ls -la scripts/schema_check.py 2>&1 | head -2

arm A5-seed-fresh-clone "router seed  (venv WITHOUT duckdb, fresh clone)" "/home/kara/.hermes/stand-in/readme/task-router/stranger-1717"
S2=/home/kara/.hermes/stand-in/readme/task-router/stranger-1717
if [ ! -x "$S2/venv2/bin/python" ]; then
  timeout 200 python3 -m venv "$S2/venv2" >/dev/null 2>&1
  ( cd "$STR" && "$S2/venv2/bin/pip" install -q -e . >/dev/null 2>&1 )
fi
echo "## venv2 pkgs:"; "$S2/venv2/bin/pip" list 2>/dev/null | grep -iE '^(duckdb|pytest) ' || echo "(-- neither duckdb nor pytest installed)"
rm -f "$STR/registry.json"
echo "## registry.json present before: $(test -f "$STR/registry.json" && echo yes || echo no)"
( cd "$STR" && timeout 120 "$S2/venv2/bin/python" -m task_router seed 2>&1 | tail -6 ); echo "--seed-rc=$?"
echo "## registry.json after: $(test -f "$STR/registry.json" && echo yes || echo no)"

arm A6-chainrun-outside-checkout "router chain-run   (cwd is NOT a git checkout)" "$SHOME"
cd "$SHOME" || exit 1
source "$STR/.venv/bin/activate"
timeout 60 router chain-run 2>&1 | tail -8; echo "--chain-run-rc=${PIPESTATUS[0]}"
timeout 60 router chain-run my-project 2>&1 | tail -6; echo "--chain-run-proj-rc=${PIPESTATUS[0]}"

arm A7-env-var-count "grep ROUTER_* in code vs docs/configuration.md; README claim" "$PUB"
cd "$PUB" || exit 1
echo "## README claim:"; grep -n 'ROUTER_' README.md | grep -iE '44|environment variables' | head -3
echo "## unique in task_router/ + scripts/:"; grep -rhoE 'ROUTER_[A-Z0-9_]+' task_router scripts 2>/dev/null | sort -u | wc -l
echo "## unique documented in docs/configuration.md:"; grep -oE 'ROUTER_[A-Z0-9_]+' docs/configuration.md 2>/dev/null | sort -u | wc -l

arm A8-toc-vs-h2 "H2 headings vs Contents entries" "$PUB"
cd "$PUB" || exit 1
grep -n '^## ' README.md
echo "## TOC bullet count (lines 9-27):"; sed -n '9,27p' README.md | grep -c '^- \['
echo "## TOC entries missing an H2, and H2s missing from TOC:"
python3 - <<'PY'
import re
t=open('README.md',encoding='utf-8').read().splitlines()
h2=[l[3:].strip() for l in t if l.startswith('## ')]
toc=[l for l in t[8:28] if l.startswith('- [')]
slugs=[re.sub(r'[^a-z0-9 -]','',h.lower()).replace(' ','-') for h in h2]
intoc=[any(s in e.lower().replace(' ','-').replace('_','-') for s in slugs) for e in toc]
print('H2 count',len(h2),'TOC count',len(toc))
for h,s in zip(h2,slugs):
    hit=any(s.replace('-','') in e.lower().replace('/','').replace('-','').replace(' ','') for e in toc)
    if not hit: print('H2 NOT IN TOC:',h)
PY

arm A9-cli-table-drift "router --help command list vs README CLI reference" "$STR"
cd "$STR" || exit 1
source .venv/bin/activate
timeout 60 router --help > /tmp/cli-help.txt 2>&1; echo "--help-rc=$?"
python3 - <<'PY'
import re
h=open('/tmp/cli-help.txt',encoding='utf-8').read()
m=re.search(r'\{([a-z0-9,\-]+)\}',h)
cmds=set(m.group(1).split(',')) if m else set()
r=open('README.md',encoding='utf-8').read().splitlines()
rows=[l for l in r if l.startswith('| `router ')]
listed=[]
for l in rows:
    t=re.match(r'\|\s*`router ([a-z0-9\-]+)',l)
    if t: listed.append(t.group(1))
dup=sorted({c for c in listed if listed.count(c)>1})
print('CLI commands:',len(cmds))
print('README table rows:',len(rows),'unique:',len(set(listed)))
print('IN CLI, MISSING FROM README:',sorted(cmds-set(listed)))
print('IN README, NOT IN CLI:',sorted(set(listed)-cmds))
print('DUPLICATED IN README:',dup)
PY

arm A10-quickstart-rerun "documented quickstart commands, rc only" "$STR"
cd "$STR" || exit 1
source .venv/bin/activate
for c in "router seed" "router validate" "router status" "router spawn my-project --format json" "router spawn P1_CODING --format json" "router spawn --list-profiles" "router circuit status --json" "router estimate --project my-project --tokens-in 100000 --tokens-out 100000"; do
  out=$(timeout 120 $c 2>&1); rc=$?
  echo "cmd: $c | rc=$rc | first-line: $(echo "$out" | head -1 | cut -c1-140)"
done

echo; echo "=== SWEEP COMPLETE ==="
