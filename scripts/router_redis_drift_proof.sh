#!/usr/bin/env bash
# TR-388 hardening proof: drift detection end-to-end in an ISOLATED scratch scope.
# Proves R8 (scope isolation), R9 (hash moves on a one-field edit), R11 (STALE names
# the table), R14-R17 (diff counts), and that production scope is untouched.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=$HOME/.hermes/venvs/board/bin/python3
SCRATCH="${TR388_PROOF_DIR:-$HOME/.hermes/cache/scratch/tr388-drift}"
rm -rf "$SCRATCH"; mkdir -p "$SCRATCH/tables" "$SCRATCH/state"

for t in archetypes benchmarks category_levels fallback_lanes level_defs model_perf \
         model_tier models projects providers task_profile_requirements task_profiles; do
  cp "$REPO/data/tables/$t.jsonl" "$SCRATCH/tables/$t.jsonl"
done
cp "$REPO/registry.json" "$SCRATCH/registry.json" 2>/dev/null || true

export ROUTING_DATA_DIR="$SCRATCH/tables"
export ROUTING_REGISTRY="$SCRATCH/registry.json"
export ROUTING_STATE_DIR="$SCRATCH/state"

echo "### 1. scratch publish REFUSED without the opt-in (R8 guard)"
( unset ROUTING_REDIS_ALLOW_SCRATCH; $PY "$REPO/scripts/router_redis.py" load --force; echo "rc=$?" )

echo
echo "### 2. with opt-in: publish the scratch plane"
ROUTING_REDIS_ALLOW_SCRATCH=1 $PY "$REPO/scripts/router_redis.py" load --force
echo "rc=$?"

echo
echo "### 3. status in the scratch scope (expect UNSTAMPED / MATCH)"
$PY "$REPO/scripts/router_redis.py" status; echo "rc=$?"

echo
echo "### 4. mutate ONE field in ONE row, then status must be STALE naming models"
python3 - "$SCRATCH" <<'PY'
import json, sys
p = sys.argv[1] + '/tables/models.jsonl'
lines = [json.loads(l) for l in open(p) if l.strip()]
for row in lines:
    if row.get('provider') == 'zai-glm' and row.get('model') == 'glm-5.3':
        row['normalized_price'] = 999.0
        break
with open(p, 'w') as fh:
    for row in lines:
        fh.write(json.dumps(row, ensure_ascii=False) + '\n')
print('mutated normalized_price of zai-glm/glm-5.3 -> 999.0')
PY

$PY "$REPO/scripts/router_redis.py" status; echo "rc=$?"

echo
echo "### 5. diff must show model_perf/models drift for that row"
$PY "$REPO/scripts/router_redis.py" status --diff --table models 2>&1 | tail -3

echo
echo "### 6. reload, status back to clean"
ROUTING_REDIS_ALLOW_SCRATCH=1 $PY "$REPO/scripts/router_redis.py" load --force >/dev/null
$PY "$REPO/scripts/router_redis.py" status; echo "rc=$?"

echo
echo "### 7. production scope untouched (different scope string, own pointer)"
unset ROUTING_DATA_DIR ROUTING_REGISTRY ROUTING_STATE_DIR ROUTING_REDIS_ALLOW_SCRATCH
$PY "$REPO/scripts/router_redis.py" status; echo "rc=$?"
