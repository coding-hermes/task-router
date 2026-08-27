#!/usr/bin/env python3
"""router_pricing.py — normalized pricing engine (Bane 2026-08-27).

The normalized price is NOT the sticker: it is the effective $/M given how the
subscription actually bills (per-token PAYG vs per-minute agent lanes vs
request-bucket plans vs official credit-point formulas). The chain sorts by
normalized_price × token_factor — garbage in = wrong routing.

Sources of truth, in order:
  1. Existing evidence rows are PRESERVED ('official formula', 'or-spot-*',
     'official+estimate', 'measured+estimate', 'estimate') — never overwritten.
  2. plan_terms.jsonl declares each provider's billing model:
       per_token       -> normalized = sticker cost_in (models.dev catalog),
                          evidence 'normalized:payg-sticker'
       per_request     -> normalized = plan_cost / requests / tokens_per_request * 1e6,
                          evidence 'normalized:sub-bucket'
       per_minute      -> normalized = rate_per_minute / tokens_per_minute * 1e6,
                          evidence 'normalized:sub-minute'
       official-points -> untouched (formula rows already carry the price)
  3. Providers with UNKNOWN terms keep NULL prices and are reported as
     pricing-gaps — the research agent fills plan_terms.jsonl, the next run
     prices them (the self-improving loop).

CLI: router_pricing.py [--dry-run] [--json]
"""
import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))


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
            f.write(json.dumps(r) + '\n')
    os.replace(path + '.tmp', path)


def normalize(dry_run):
    models = _rows('models')
    terms = {t['provider']: t for t in _rows('plan_terms')}
    catalog = {(c['provider'], c['model']): c for c in _rows('model_catalog')}

    priced, gaps = [], []
    for m in models:
        if m.get('archive') or m.get('valid_to'):
            continue
        if m.get('normalized_price') not in (None, 0):
            continue  # already priced (evidence preserved)
        t = terms.get(m['provider'])
        if not t:
            gaps.append((m['provider'], m['model'], 'no plan_terms row'))
            continue
        model = t.get('billing_model')
        if model == 'official-points':
            gaps.append((m['provider'], m['model'], 'official-points (manual formula row)'))
            continue
        cat = catalog.get((m['provider'], m['model']))
        if model == 'per_token':
            cost_in = (cat or {}).get('cost_input')
            if cost_in is None:
                gaps.append((m['provider'], m['model'], 'no models.dev sticker'))
                continue
            price = round(float(cost_in), 4)
            evidence = 'normalized:payg-sticker'
        elif model == 'per_request':
            cost = t.get('plan_cost'); reqs = t.get('requests'); tpr = t.get('tokens_per_request')
            if not (cost and reqs and tpr):
                gaps.append((m['provider'], m['model'], 'incomplete per_request terms'))
                continue
            price = round(float(cost) / float(reqs) / float(tpr) * 1e6, 4)
            evidence = 'normalized:sub-bucket'
        elif model == 'per_minute':
            rate = t.get('rate_per_minute'); tpm = t.get('tokens_per_minute')
            if not (rate and tpm):
                gaps.append((m['provider'], m['model'], 'incomplete per_minute terms'))
                continue
            price = round(float(rate) / float(tpm) * 1e6, 4)
            evidence = 'normalized:sub-minute'
        else:
            gaps.append((m['provider'], m['model'], f'unknown billing_model {model!r}'))
            continue
        priced.append((m['provider'], m['model'], price, evidence))
        m['normalized_price'] = price
        m['price_evidence'] = evidence

    if dry_run:
        for p, name, price, ev in priced:
            print(f'  ~ {p}/{name} -> ${price:.4f} ({ev})')
        for p, name, why in gaps:
            print(f'  ! {p}/{name}: {why}')
        print(f'DRY-RUN: {len(priced)} would price, {len(gaps)} remain gaps')
        return 0

    if priced:
        _write('models', models)
    for p, name, price, ev in priced:
        print(f'  ~ {p}/{name} -> ${price:.4f} ({ev})')
    for p, name, why in gaps:
        print(f'  ! {p}/{name}: {why}')
    print(f'normalized pricing: {len(priced)} priced, {len(gaps)} gaps remain')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    return normalize(args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
