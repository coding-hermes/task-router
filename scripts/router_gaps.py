#!/usr/bin/env python3
"""router_gaps.py — registry data-quality report (Bane 2026-08-27).

Answers "which models are lacking data or incomplete" so agents can quality-
identify what to fill, and the data-quality cron can iterate.

Dimensions per (provider, model):
  price     — normalized_price is null/0 (no usable price -> inert in chains)
  catalog   — no model_catalog.jsonl row (no models.dev metadata)
  benchmark — no benchmarks.jsonl rows at all for this model
  perf      — all perf_* estimate columns null (no capability estimates)
  tiers     — model_tier coverage < 24 categories
  sentiment — no benchmarks rows with category='sentiment' (no user-sentiment evidence)
  notes     — no model_notes.jsonl row (no qualitative research notes)

CLI:
  router_gaps.py [--json] [--lacking N] [--models <substr>] [--providers]
  --lacking N  only models with >= N missing dimensions
  --json       machine-readable (the cron/agent consumption path)
  --top N      show only the N most incomplete models
Exit 0 always (reporting tool).
"""
import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
CATS = 24


def _rows(name):
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    out = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load():
    tables = {}
    for name in ('models', 'benchmarks', 'model_tier', 'model_catalog', 'model_notes'):
        tables[name] = _rows(name)
    return tables


def assess(tables):
    models = tables['models']
    bench = tables['benchmarks']
    tier = tables['model_tier']
    cat = tables['model_catalog']
    notes = tables['model_notes']

    bench_by = {}
    for b in bench:
        bench_by.setdefault(b['model'], []).append(b)
    tier_by = {}
    for t in tier:
        tier_by.setdefault(t['model'], []).append(t)
    cat_by = {(c['provider'], c['model']) for c in cat}
    notes_by = {(n['provider'], n['model']) for n in notes}

    per = []
    for m in models:
        if m.get('archive') or m.get('valid_to') or m.get('disabled'):
            continue  # disabled = intentional exclusion (plan sweep / quality)
        p, name = m['provider'], m['model']
        missing = []
        if m.get('normalized_price') in (None, 0):
            missing.append('price')
        if (p, name) not in cat_by:
            missing.append('catalog')
        if not bench_by.get(name):
            missing.append('benchmark')
        perf_cols = [m.get(k) for k in ('perf_agent_tick', 'perf_long_doc', 'perf_debug',
                                        'perf_schema', 'perf_e2e_vision', 'perf_review',
                                        'perf_delegation', 'perf_guard', 'perf_mock',
                                        'perf_reasoning')]
        if not any(v is not None for v in perf_cols):
            missing.append('perf')
        n_tier = len(tier_by.get(name, []))
        # tiers gap: < HALF the taxonomy evidenced (BLANK default -1 makes
        # <24 normal; <12 evidenced cats = genuinely thin — 2026-08-27)
        if n_tier < CATS // 2:
            missing.append(f'tiers({n_tier}/{CATS})')
        if not any(b.get('category') == 'sentiment' for b in bench_by.get(name, [])):
            missing.append('sentiment')
        if (p, name) not in notes_by:
            missing.append('notes')
        price = m.get('normalized_price')
        per.append({'provider': p, 'model': name, 'price': price,
                    'missing': missing, 'n_missing': len(missing)})
    return per


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--lacking', type=int, default=0)
    ap.add_argument('--models', default=None)
    ap.add_argument('--top', type=int, default=0)
    args = ap.parse_args(argv)

    tables = load()
    per = assess(tables)
    if args.models:
        per = [x for x in per if args.models.lower() in x['model'].lower()]
    if args.lacking:
        per = [x for x in per if x['n_missing'] >= args.lacking]
    per.sort(key=lambda x: (-x['n_missing'], x['provider'], x['model']))
    if args.top:
        per = per[: args.top]

    if args.json:
        print(json.dumps({'total_models': len(assess(tables)),
                          'gapped': per}, indent=1))
        return 0

    print(f'== registry data-quality gaps ({len(per)} of '
          f'{len(assess(tables))} active models) ==')
    if not per:
        print('no gaps — every active model has price, catalog, benchmark, perf, '
              'full tiers, sentiment evidence and notes')
        return 0
    for x in per:
        price = f"${x['price']:.4f}" if x['price'] not in (None, 0) else 'NULL'
        print(f"  {x['provider'] + '/' + x['model']:<46} price={price:<10} "
              f"missing({x['n_missing']}): {', '.join(x['missing'])}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
