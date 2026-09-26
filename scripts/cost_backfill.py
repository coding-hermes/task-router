#!/usr/bin/env python3
"""cost_backfill — stamp a cost on proxied rows that were served before their lane had a price.

Why: rows served on a plan lane with no price recorded `cost_usd: None` with
"no price on this hop" — honest, but it left cost coverage at 2/23 while the tokens were sitting
right there. The price now exists in the registry (declared sticker / usage_multiplier), so the
cost is computable from data already on the row.

Rules (no fabrication):
- Only rows whose cost_usd is None. A measured cost is never overwritten.
- Only when the serving lane HAS a price in the registry now.
- Only when the row records tokens. No tokens -> nothing to compute -> left alone.
- The stamp names its basis, so a backfilled number is never mistaken for a metered one.
- A lane still unpriced (or a genuine $0 promo lane) stays None: unknown is unknown.

Default is a DRY RUN. --apply writes, after backing the ledger up.
"""
import argparse, collections, json, shutil, sys, time
from datetime import datetime, timezone

LEDGER = '/home/kara/task-router/data/state/outcomes.jsonl'
REGISTRY = '/home/kara/task-router/registry.json'
STAMP = 'backfill-20260926: public split (in_per_m/out_per_m) from registry'


def load_prices():
    reg = json.load(open(REGISTRY))
    px = {}
    for m in reg['tables']['models']:
        if m.get('disabled') or m.get('archive'):
            continue
        key = (m.get('provider'), m.get('model'))
        pin, pout = m.get('public_in_per_m'), m.get('public_out_per_m')
        cache = m.get('public_cache_read_per_m')
        if pin is None and pout is None:
            continue
        px[key] = (float(pin or 0), float(pout or 0), float(cache) if cache is not None else None)
    return px


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int, default=0, help='cap rows written (0 = all)')
    a = ap.parse_args()

    px = load_prices()
    rows, enriched, skipped = [], 0, collections.Counter()
    examples = []
    for line in open(LEDGER, errors='replace'):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        rows.append(d)
        if 'router-proxy' not in str(d.get('session_id') or d.get('source_system')):
            continue
        if d.get('cost_usd') is not None:
            skipped['already has a cost'] += 1
            continue
        p = px.get((d.get('provider'), d.get('model')))
        if not p:
            skipped['lane has no price'] += 1
            continue
        tin, tout = d.get('tokens_in'), d.get('tokens_out')
        if not tin and not tout:
            skipped['no tokens recorded'] += 1
            continue
        cache = d.get('cache_read_tokens') or 0
        cost = (float(tin or 0) / 1e6) * p[0] + (float(tout or 0) / 1e6) * p[1]
        if p[2] is not None and cache:
            cost += (float(cache) / 1e6) * p[2]
        cost = round(cost, 8)
        if cost <= 0:
            skipped['computed zero'] += 1
            continue
        d['cost_usd'] = cost
        d['price_basis'] = STAMP
        d['cost_source'] = 'backfill-20260926'
        d['cost_backfilled_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        enriched += 1
        if len(examples) < 5:
            examples.append((d.get('provider'), d.get('model'), tin, tout, cache, cost))
        if a.limit and enriched >= a.limit:
            break

    print(f'  rows scanned      : {len(rows)}')
    print(f'  would enrich      : {enriched}')
    print(f'  skipped           : {dict(skipped)}')
    for e in examples:
        print(f'    {e[0]}/{str(e[1])[:26]:28} in={e[2]} out={e[3]} cache={e[4]} -> ${e[5]}')

    if not a.apply:
        print('  DRY RUN — nothing written (pass --apply)')
        return 0
    if not enriched:
        print('  nothing to apply')
        return 0
    bak = LEDGER + f'.bak-costbackfill-{int(time.time())}'
    shutil.copy2(LEDGER, bak)
    with open(LEDGER, 'w') as f:
        for d in rows:
            f.write(json.dumps(d) + '\n')
    print(f'  APPLIED — {enriched} rows stamped; backup at {bak}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
