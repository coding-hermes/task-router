#!/usr/bin/env python3
"""plan_effective_backfill — price the fleet's own session rows from the router's registry.

Why: `import_hermes` copies the driver's `estimated_cost_usd` verbatim, and for a subscription lane
that number is 0.0 by config. Measured consequence: 95 of 110 `hermes` cost buckets in the rolling
averages read exactly 0.0 while the registry prices those very lanes, so the cost-per-task the chain
sort feeds on was zero across the fleet. TR-070 puts lane pricing here.

Scope, and why it is safe to re-run:
  * only rows with source_system == 'hermes'
  * only where the row's own cost is falsy (0 / null) AND the row carries usage
  * only where the lane declares a price; otherwise the row is stamped with the reason for the NULL
  * a row that already carries a cost is NEVER touched
  * every changed row gets a price_basis naming the derivation

Default is a DRY RUN. `--apply` writes the file atomically after taking a timestamped backup, and
refuses to run while the store's mtime changes under it (a live proxy append would be clobbered).
"""
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import router_outcomes as ro  # noqa: E402

STORE = os.path.expanduser('~/.hermes/model-router/../task-router/data/state/outcomes.jsonl')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--store', default=os.environ.get(
        'ROUTING_OUTCOMES_FILE', '/home/kara/task-router/data/state/outcomes.jsonl'))
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    before_mtime = os.path.getmtime(a.store)
    rows, changed, untouched, unpriced, no_usage = [], 0, 0, 0, 0
    sample = []
    with open(a.store, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                rows.append(None)
                continue
            if d.get('source_system') == 'hermes' and not d.get('cost_usd'):
                tin, tout = d.get('tokens_in') or 0, d.get('tokens_out') or 0
                cost, basis = ro.plan_effective_cost(d.get('provider'), d.get('model'), tin, tout)
                if cost is not None and cost > 0:
                    d['cost_usd'] = cost
                    d['price_basis'] = basis
                    changed += 1
                    if len(sample) < 5:
                        sample.append((d.get('provider'), d.get('model'), tin, tout, cost, basis))
                else:
                    d['price_basis'] = basis
                    if 'no usage' in (basis or ''):
                        no_usage += 1
                    else:
                        unpriced += 1
            else:
                untouched += 1
            rows.append(d)

    print(json.dumps({'store': a.store, 'rows': len(rows), 'priced_now': changed,
                      'left_unpriced_with_reason': unpriced, 'no_usage': no_usage,
                      'already_had_a_cost_or_not_hermes': untouched, 'applied': bool(a.apply)},
                     indent=2))
    for s in sample:
        print('   sample: %s/%s in=%s out=%s -> %.8f  (%s)' % (s[0], s[1], s[2], s[3], s[4], s[5][:60]))
    if not a.apply:
        print('   DRY RUN - nothing written (pass --apply to write)')
        return 0
    if os.path.getmtime(a.store) != before_mtime:
        print('   REFUSED: the store changed while reading (a live writer is appending). Re-run when idle.')
        return 2
    bak = a.store + '.backup-' + time.strftime('%Y%m%dT%H%M%S')
    shutil.copy2(a.store, bak)
    tmp = a.store + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        for d in rows:
            fh.write((json.dumps(d) if d is not None else '') + '\n')
    os.replace(tmp, a.store)
    print('   written; backup at %s' % bak)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
