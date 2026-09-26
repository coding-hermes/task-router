#!/usr/bin/env python3
"""Correct plan/pricing rows per Bane 2026-09-26.

  commandcode   : 2 seats x $10/mo = $20/mo provider fee (registry said $15/mo)
  opencode-go   : $10/mo per seat, 2 seats = $20 (registry/plan_terms said $12)
  grok-build    : $300/year = $25/mo (registry said $60, plan_terms said $30)

Writes data/tables/{plan_terms,providers}.jsonl with a timestamped backup, in the
repo's canonical JSON spacing. Registry rebuild is a separate step (router_maintain).
"""
import json
import pathlib
import shutil
import time

TR = pathlib.Path.home() / 'task-router'
TABLES = TR / 'data' / 'tables'

PLAN_FIX = {
    'commandcode': (20.0, '2 seats x $10/mo = $20/mo provider fee (Bane 2026-09-26; '
                           'recorded as "$15/mo" before). Model usage stays 1:1 PAYG '
                           'pass-through with no markup, so billing_model stays per_token.'),
    'opencode-go': (10.0, '$10/mo per seat (Bane 2026-09-26; recorded $12). Two seats '
                          '(opencode-go + opencode-go-2) = $20/mo total.'),
    'opencode-go-2': (10.0, '$10/mo second seat (Bane 2026-09-26; recorded $12); mirror of '
                            'opencode-go terms, same 5h request table.'),
    'grok-build': (25.0, 'SuperGrok Build is BILLED $300/year = $25/mo (Bane 2026-09-26; '
                         'recorded $30/mo). Bane is considering moving to the $100/mo tier '
                         'so the lane can carry real work.'),
}
PLAN_STRING_FIX = {
    'commandcode': '2 seats x $10/mo = $20/mo provider fee + PAYG pass-through (no markup)',
    'opencode-go': '$10/mo (2 accounts = $20/mo)',
    'opencode-go-2': '$10/mo (2nd account)',
    'grok-build': '$300/yr (~$25/mo) SuperGrok Build — candidate to move to $100/mo tier',
}

stamp = time.strftime('%Y%m%d-%H%M%S')
for name in ('plan_terms', 'providers'):
    src = TABLES / f'{name}.jsonl'
    shutil.copy(src, f'/tmp/{name}.bak-{stamp}')
    print(f'backup /tmp/{name}.bak-{stamp}')

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
        r['note'] = f'PLAN COST CORRECTED {why} | ' + (r.get('note') or '')
        changed.append(f'plan_terms {prov}: plan_cost {old} -> {cost}')
p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

# providers
p = TABLES / 'providers.jsonl'
rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
for r in rows:
    pid = r.get('id')
    if pid in PLAN_STRING_FIX:
        old = r.get('plan')
        r['plan'] = PLAN_STRING_FIX[pid]
        changed.append(f'providers {pid}: plan "{old}" -> "{r["plan"]}"')
p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

print('\n'.join('  ' + c for c in changed))
print(f'\n{len(changed)} field corrections applied')
