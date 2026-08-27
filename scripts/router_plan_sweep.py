#!/usr/bin/env python3
"""router_plan_sweep.py — disable lanes that don't pay for themselves (Bane 2026-08-27).

Doctrine: the registry should carry what we can actually use AFFORDABLY.
For flat_subscription providers (Cline Pass $9.99/mo), every lane that is:
  - NOT in the plan's included_models, AND
  - NOT covered by an active temporary discount (:free lanes etc.)
is PAYG at published per-token prices — expensive vs the flat lane — so it is
DISABLED (models.disabled = true + disabled_reason), exactly like the
openai-codex archive for API-catalog junk. Disabled lanes:
  - are skipped by router_spawn (never in chains)
  - stay in the registry (auditable, re-enableable if pricing changes)
  - are skipped by pricing/gaps (no busy work on intentional exclusions)

Quality disables (persistent complaints / failed evals) are written directly
by the research agent with disabled_reason = the finding; this tool handles
the mechanical plan-outside sweep. Idempotent: already-disabled lanes are
skipped. --apply writes; default is a dry-run report.

Usage:
  python3 router_plan_sweep.py [--apply] [--json]
"""
import argparse
import datetime
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_REPO, 'data', 'tables')


def _rows(name):
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    out = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _write(name, rows):
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    with open(path + '.tmp', 'w') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(path + '.tmp', path)


def sweep(apply=False):
    today = datetime.date.today().isoformat()
    models = _rows('models')
    terms = _rows('plan_terms')
    discounts = _rows('temporary_discounts')

    flat = {t['provider']: t for t in terms if t.get('billing_model') == 'flat_subscription'}
    if not flat:
        print('no flat_subscription providers — nothing to sweep')
        return [], 0

    # active discount coverage: model-level rows valid today (valid_to null = open)
    def _active(d):
        if d.get('valid_from') and d['valid_from'] > today:
            return False
        if d.get('valid_to') and d['valid_to'] < today:
            return False
        return True

    disc_models = {(d['provider'], d['model']) for d in discounts if _active(d)}
    disc_wide = {d['provider'] for d in discounts if _active(d) and d.get('model') == '*'}

    to_disable = []
    for m in models:
        if m.get('archive') or m.get('valid_to') is not None or m.get('disabled'):
            continue
        p, name = m['provider'], m['model']
        if p not in flat:
            continue
        if p in disc_wide or (p, name) in disc_models:
            continue  # provider-wide or lane discount = covered
        bare = name.replace(':free', '')
        included = flat[p].get('included_models') or []
        if bare in included:
            continue  # flat-plan member — keep
        reason = (f'PAYG outside {p} flat plan — expensive vs flat lane; '
                  f'not in included_models ({len(included)} in-plan, '
                  f'{sum(1 for x in models if x["provider"] == p and not x.get("disabled"))} lanes total)')
        to_disable.append({'provider': p, 'model': name, 'reason': reason})

    if not to_disable:
        print(f'sweep: nothing to disable for {sorted(flat)}')
        return [], 0

    if apply:
        by = {(m['provider'], m['model']): m for m in models}
        for d in to_disable:
            row = by.get((d['provider'], d['model']))
            if row is not None:
                row['disabled'] = True
                row['disabled_reason'] = d['reason']
        _write('models', models)
        print(f'sweep --apply: disabled {len(to_disable)} lanes')
    else:
        provs = {}
        for d in to_disable:
            provs[d['provider']] = provs.get(d['provider'], 0) + 1
        print(f'sweep DRY-RUN: would disable {len(to_disable)} lanes: ' +
              ', '.join(f'{p}={n}' for p, n in sorted(provs.items())))
        for d in to_disable[:8]:
            print(f'  - {d["provider"]}/{d["model"]}')
        if len(to_disable) > 8:
            print(f'  ... and {len(to_disable) - 8} more')
    return to_disable, len(to_disable)


def main(argv=None):
    ap = argparse.ArgumentParser(description='disable lanes outside flat plans (dry-run default)')
    ap.add_argument('--apply', action='store_true', help='write the disables (default: report only)')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    disables, n = sweep(apply=args.apply)
    if args.json:
        print(json.dumps({'disabled': n,
                          'lanes': [{'provider': d['provider'], 'model': d['model']}
                                    for d in disables]}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
