#!/usr/bin/env python3
"""router_pricing.py — normalized pricing engine (Bane 2026-08-27).

The normalized price is NOT the sticker: it is the effective $/M given how the
subscription actually bills (per-token PAYG vs per-minute agent lanes vs
request-bucket plans vs official credit-point formulas). The chain sorts by
normalized_price × token_factor — garbage in = wrong routing.

Sources of truth, in order:
  1. Existing evidence rows are PRESERVED ('official formula', 'or-spot-*',
     'official+estimate', 'measured+estimate', 'estimate') — never overwritten.
  2. plan_terms.jsonl declares each provider's billing model; the math lives
     in scripts/pricing/<billing_model>.py (TR-034), dispatched via
     scripts/pricing/__init__.py BY_BILLING_MODEL:
       per_token       -> normalized = sticker cost_in (models.dev catalog),
                          evidence 'normalized:payg-sticker'
       per_request     -> normalized = plan_cost / requests / tokens_per_request * 1e6,
                          evidence 'normalized:sub-bucket' (blended-estimate
                          fallback when the bucket budget is unknown)
       per_minute      -> normalized = rate_per_minute / tokens_per_minute * 1e6,
                          evidence 'normalized:sub-minute'
  2. flat_subscription — a fixed monthly/period fee buys INCLUDED models at a
     usage multiplier vs the standard API rate (Cline Pass $9.99/mo, 2-5x usage
     per docs.cline.bot). effective $/M = models.dev blended sticker / multiplier.
     Models outside the included list are PAYG (unknown prices -> gap).
  3. temporary_discounts.jsonl — active discounts applied on top of the base
     price (math in scripts/pricing/helpers.py): {provider, model ('*' =
     provider-wide), discount_type ('percent'|'free'), value, valid_from,
     valid_to (null = open), source, note}. Expired rows (valid_to < today)
     are ignored; expiring rows are reported. Evidence tag gains '+discount'
     and the discount window is stamped on the row.
  4. Providers with UNKNOWN terms keep NULL prices and are reported as
     pricing-gaps — the research agent fills plan_terms.jsonl, the next run
     prices them (the self-improving loop).

This module is the thin entrypoint (CLI + orchestration); every pricing
formula lives in scripts/pricing/ (TR-034).

CLI: router_pricing.py [--dry-run] [--json]
"""
import argparse
import datetime
import json
import os
import sys

# realpath (not abspath): the live install at ~/.hermes/scripts/router_pricing.py
# is a SYMLINK into this repo — the pricing package import must anchor to the
# real script location regardless of the caller's cwd.
_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from pricing import BY_BILLING_MODEL, MANUAL_FORMULA_MODELS  # noqa: E402
from pricing import helpers as pricing_helpers  # noqa: E402

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


def normalize(dry_run, quiet=False):
    models = _rows('models')
    terms = {t['provider']: t for t in _rows('plan_terms')}
    catalog = {(c['provider'], c['model']): c for c in _rows('model_catalog')}
    discounts = _rows('temporary_discounts')
    today = datetime.date.today().isoformat()

    # --- 0. PUBLIC PRICE FILL (Bane 2026-08-27) ------------------------------
    # Stamp every row that has a models.dev catalog sticker with its PUBLIC
    # list price, whether or not it is already normalized-priced. The
    # scheduler's cost reporting consumes these (provider-aware, sticker-true)
    # instead of the hardcoded map; normalized_price keeps driving ordering.
    filled_public = 0
    for m in models:
        if m.get('archive') or m.get('valid_to') or m.get('disabled'):
            continue
        cat = catalog.get((m['provider'], m['model']))
        if cat and pricing_helpers.fill_public_price(
                m, cat.get('cost_input'), cat.get('cost_output')):
            filled_public += 1
    if filled_public and not quiet:
        print(f'public-price fill: {filled_public} rows stamped from models.dev sticker')

    priced, gaps = [], []
    for m in models:
        if m.get('archive') or m.get('valid_to') or m.get('disabled'):
            continue  # disabled = intentional exclusion (plan sweep / quality)
        t = terms.get(m['provider'])
        if not t:
            if m.get('normalized_price') is not None:
                continue  # already priced, no terms needed
            gaps.append((m['provider'], m['model'], 'no plan_terms row'))
            continue
        # STALE FLAT REPRICE (Bane 2026-08-27): in-plan lanes whose price
        # comes from the sticker era (estimate/clinepass-api — before the flat
        # plan was known) must carry the subscription economics, not the old
        # box price. Protected evidence (normalized:*/official formula/or-spot)
        # is never touched.
        # NOTE (2026-08-30): only applies to plans WITH an included_models
        # list. Usage-bucket flat plans (ollama-cloud: no per-model included
        # list — the flat fee buys a bucket) keep their researched estimate
        # prices; there is no in-plan/PAYG distinction to reprice against.
        stale_flat = (t.get('billing_model') == 'flat_subscription'
                      and t.get('included_models')
                      and m.get('normalized_price') not in (None, 0)
                      and not (m.get('price_evidence') or '').startswith('normalized:'))
        if m.get('normalized_price') not in (None, 0) and not stale_flat:
            continue  # already priced (evidence preserved)
        model = t.get('billing_model')
        mod = BY_BILLING_MODEL.get(model)
        if mod is None:
            gaps.append((m['provider'], m['model'], f'unknown billing_model {model!r}'))
            continue
        if model in MANUAL_FORMULA_MODELS:
            # manual-formula models: lanes carry researched prices already
            # (official formula / official+estimate); unpriced lanes stay
            # documented gaps (kimi aliases are plan aliases, not models).
            gaps.append((m['provider'], m['model'], mod.price(m, t, catalog, pricing_helpers,
                                                              {'discounts': discounts, 'today': today})[2]))
            continue
        price, evidence, reason = mod.price(
            m, t, catalog, pricing_helpers, {'discounts': discounts, 'today': today})
        if price is None:
            if reason:
                gaps.append((m['provider'], m['model'], reason))
            continue

        # temporary discounts on top of the base price
        eff, dnotes = pricing_helpers.apply_discount(price, m['provider'], m['model'],
                                                     discounts, today)
        if dnotes:
            evidence = evidence + '+discount(' + ','.join(dnotes) + ')'
            vt = [d.get('valid_to') for d in
                  pricing_helpers.active_discounts(m['provider'], m['model'],
                                                   discounts, today)
                  if d.get('valid_to')]
            if vt:
                m['discount_valid_to'] = min(vt)
        priced.append((m['provider'], m['model'], eff, evidence))
        m['normalized_price'] = eff
        m['price_evidence'] = evidence

    if dry_run:
        if not quiet:
            for p, name, price, ev in priced:
                print(f'  ~ {p}/{name} -> ${price:.4f} ({ev})')
            for p, name, why in gaps:
                print(f'  ! {p}/{name}: {why}')
            print(f'DRY-RUN: {len(priced)} would price, {len(gaps)} remain gaps')
        return {'dry_run': True, 'priced': priced, 'gaps': gaps,
                'filled_public': filled_public}

    if priced or filled_public:
        _write('models', models)
    if not quiet:
        for p, name, price, ev in priced:
            print(f'  ~ {p}/{name} -> ${price:.4f} ({ev})')
        for p, name, why in gaps:
            print(f'  ! {p}/{name}: {why}')
        print(f'normalized pricing: {len(priced)} priced, {len(gaps)} gaps remain')
    return {'dry_run': False, 'priced': priced, 'gaps': gaps,
            'filled_public': filled_public}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    res = normalize(args.dry_run, quiet=args.json)
    if args.json:
        print(json.dumps({
            'dry_run': res['dry_run'],
            'filled_public': res['filled_public'],
            'priced': [{'provider': p, 'model': m, 'price': pr, 'evidence': ev}
                       for p, m, pr, ev in res['priced']],
            'gaps': [{'provider': p, 'model': m, 'reason': why}
                     for p, m, why in res['gaps']],
        }, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
