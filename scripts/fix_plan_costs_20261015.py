#!/usr/bin/env python3
"""Go -> Go Plus tier upgrade for both opencode-go accounts (Bane 2026-10-15).

  opencode-go   : $10/mo Go -> $40/mo Go Plus (billing cycle re-anchored 2026-10-15 09:03 UTC, verified via /zen/go/v1/usage)
  opencode-go-2 : $10/mo Go -> $40/mo Go Plus (billing cycle re-anchored 2026-10-15 11:03 UTC, same verification)

Provider has NO plan-name API endpoint; the monthly resetsAt re-anchor (primary re-issued
same-day 09:03 UTC; _2 got a NEW cycle 11:03 UTC minutes before the check) is the upgrade
fingerprint. Token prices are IDENTICAL Go vs Plus (official docs); Plus raises the
PER-MODEL monthly dollar caps (~4x: ds-v4.1-flash $80->$160, glm-5.3 $15->$120, kimi-k3
$15->$60 per qcode 2026-10-15 table).

Writes data/tables/{plan_terms,providers}.jsonl with a timestamped backup, in the
repo's canonical JSON spacing. Registry rebuild + suite = separate steps.
"""
from __future__ import annotations
import json
import pathlib
import shutil
import time

TR = pathlib.Path.home() / 'task-router'
TABLES = TR / 'data' / 'tables'

PLAN_FIX = {
    'opencode-go': (40.0, 'UPGRADED to Go Plus $40/mo (Bane 2026-10-15; billing cycle '
                         're-anchored 2026-10-15T09:03Z, verified /zen/go/v1/usage; no plan-name '
                         'API endpoint exists). Token prices unchanged Go vs Plus; per-model '
                         'monthly dollar caps ~4x Go (docs example: ds-v4.1-flash $160/mo, '
                         'glm-5.3 $120/mo, kimi-k3 $60/mo on Plus). 2026-09-11 requests_per_5h '
                         'table kept as the GO-plan baseline -> treat as ~4x conservative on Plus.'),
    'opencode-go-2': (40.0, 'UPGRADED to Go Plus $40/mo second account (Bane 2026-10-15; '
                            'billing cycle re-anchored 2026-10-15T11:03Z, minutes before the '
                            'verification probe — live call through the lane returned HTTP 200 '
                            'glm-5.3-flash completion). Mirror of opencode-go Plus terms.'),
}
PLAN_STRING_FIX = {
    'opencode-go': '$40/mo Go Plus (2 accounts = $80/mo)',
    'opencode-go-2': '$40/mo Go Plus (2nd account)',
}
QUOTA_UNIT_FIX = ('per-model dollar caps (Plus ~4x Go; 5h=20% wk=50% mo=100% of the '
                  'per-model monthly cap; e.g. ds-v4.1-flash $160/mo, glm-5.3 $120/mo, '
                  'kimi-k3 $60/mo)')

stamp = time.strftime('%Y%m%d-%H%M%S')
# TR-342: backup to data/state/, never /tmp
_data_dir = TABLES.parent / 'state'
_data_dir.mkdir(parents=True, exist_ok=True)
for name in ('plan_terms', 'providers'):
    src = TABLES / f'{name}.jsonl'
    bak = _data_dir / f'{name}.bak-{stamp}'
    shutil.copy(src, str(bak))
    print(f'backup {bak}')

# plan_terms
p = TABLES / 'plan_terms.jsonl'
rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
changed = []
for r in rows:
    prov = r.get('provider')
    if prov in PLAN_FIX:
        cost, why = PLAN_FIX[prov]
        old = r.get('plan_cost')
        r['plan_cost'] = cost
        r['note'] = f'GO PLUS UPGRADE {why} | ' + (r.get('note') or '')
        changed.append(f'plan_terms {prov}: plan_cost {old} -> {cost}')
p.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')

# providers
p = TABLES / 'providers.jsonl'
rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
for r in rows:
    pid = r.get('id')
    if pid in PLAN_STRING_FIX:
        old_plan = r.get('plan')
        r['plan'] = PLAN_STRING_FIX[pid]
        old_qu = r.get('quota_unit')
        r['quota_unit'] = QUOTA_UNIT_FIX
        changed.append(f'providers {pid}: plan "{old_plan}" -> "{r["plan"]}"; quota_unit updated')
p.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')

print('\n'.join('  ' + c for c in changed))
print(f'\n{len(changed)} field corrections applied')
