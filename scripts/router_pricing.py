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
  2. flat_subscription — a fixed monthly/period fee buys INCLUDED models at a
     usage multiplier vs the standard API rate (Cline Pass $9.99/mo, 2-5x usage
     per docs.cline.bot). effective $/M = models.dev blended sticker / multiplier.
     Models outside the included list are PAYG (unknown prices -> gap).
  3. temporary_discounts.jsonl — active discounts applied on top of the base
     price: {provider, model ('*' = provider-wide), discount_type
     ('percent'|'free'), value, valid_from, valid_to (null = open), source, note}.
     Expired rows (valid_to < today) are ignored; expiring rows are reported.
     Evidence tag gains '+discount' and the discount window is stamped on the row.
  4. Providers with UNKNOWN terms keep NULL prices and are reported as
     pricing-gaps — the research agent fills plan_terms.jsonl, the next run
     prices them (the self-improving loop).

CLI: router_pricing.py [--dry-run] [--json]
"""
import argparse
import datetime
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
    discounts = _rows('temporary_discounts')
    today = datetime.date.today().isoformat()

    def active_discounts(provider, model):
        """Yield discount rows currently in effect for (provider, model)."""
        for d in discounts:
            if d.get('provider') != provider:
                continue
            if d.get('model') not in ('*', model):
                continue
            vf = d.get('valid_from')
            vt = d.get('valid_to')
            if vf and vf > today:
                continue
            if vt and vt < today:
                continue
            yield d

    def apply_discount(price, provider, model):
        """Apply active discounts to a base price. Returns (effective, notes)."""
        eff, notes = price, []
        for d in active_discounts(provider, model):
            typ, val = d.get('discount_type'), d.get('value')
            if typ == 'free' or (typ == 'percent' and float(val) >= 1.0):
                eff, notes = 0.0, ['free-lane']
            elif typ == 'percent':
                eff = eff * (1.0 - float(val))
                notes.append(f"{float(val)*100:.0f}% off")
            elif typ == 'absolute':
                eff = max(0.0, eff - float(val))
                notes.append(f"-${val}")
        return round(eff, 4), notes

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
        elif model == 'flat_subscription':
            base_name = m['model'].replace(':free', '')
            included = t.get('included_models') or []
            if base_name not in included:
                gaps.append((m['provider'], m['model'], 'outside flat-plan included list (PAYG)'))
                continue
            cost_in = (cat or {}).get('cost_input')
            cost_out = (cat or {}).get('cost_output')
            sticker_src = m['provider']
            if cost_in is None or cost_out is None:
                # same weights, other provider's models.dev sticker = the standard
                # API rate the flat plan multiplies (docs.cline.bot "2-5x usage
                # vs standard API rate")
                for (cp, cm), cr in catalog.items():
                    if cm == base_name and cr.get('cost_input') is not None and cr.get('cost_output') is not None:
                        cost_in, cost_out = cr['cost_input'], cr['cost_output']
                        sticker_src = cp
                        break
                else:
                    gaps.append((m['provider'], m['model'], 'included but no sticker for lane math'))
                    continue
            mult = float(t.get('usage_multiplier') or 1.0)
            price = round((float(cost_in) + float(cost_out)) / 2.0 / mult, 4)
            evidence = f'normalized:flat-sub({mult:.1f}x lane)'
            if sticker_src != m['provider']:
                evidence += f' sticker@{sticker_src}'
        else:
            gaps.append((m['provider'], m['model'], f'unknown billing_model {model!r}'))
            continue

        # temporary discounts on top of the base price
        eff, dnotes = apply_discount(price, m['provider'], m['model'])
        if dnotes:
            evidence = evidence + '+discount(' + ','.join(dnotes) + ')'
            vt = [d.get('valid_to') for d in active_discounts(m['provider'], m['model']) if d.get('valid_to')]
            if vt:
                m['discount_valid_to'] = min(vt)
        priced.append((m['provider'], m['model'], eff, evidence))
        m['normalized_price'] = eff
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
