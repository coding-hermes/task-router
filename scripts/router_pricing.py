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


def normalize(dry_run, quiet=False):
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

    def public_from_sticker(cost_in, cost_out):
        """Public $/M prices from a models.dev-style sticker (list price).

        Returns (in_per_m, out_per_m, blended) or None when no input sticker.
        Blended = 0.96*in + 0.04*out — the same input-dominant mix the
        sub-bucket estimator uses (agent ticks are ~96%+ input tokens).
        """
        if cost_in is None:
            return None
        ci = float(cost_in)
        co = float(cost_out) if cost_out is not None else ci
        return round(ci, 4), round(co, 4), round(0.96 * ci + 0.04 * co, 4)

    def fill_public_price(m, cost_in, cost_out):
        """Stamp PUBLIC (sticker) prices on a model row unless already present.

        Bane 2026-08-27: cost reporting ("what did it cost to build feature X")
        quotes the provider's PUBLIC list price. normalized_price stays the
        internal effective $/M used for chain ordering; these columns are what
        router_spawn.py exposes as usd_1m / in_per_m / out_per_m. Never
        overwrite an existing fill (idempotent across runs).
        """
        got = public_from_sticker(cost_in, cost_out)
        if got is None:
            return False
        pub_in, pub_out, pub_blend = got
        if m.get('public_in_per_m') is None:
            m['public_in_per_m'] = pub_in
        if m.get('public_out_per_m') is None:
            m['public_out_per_m'] = pub_out
        if m.get('public_price') is None:
            m['public_price'] = pub_blend
        return True

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
        if cat and fill_public_price(m, cat.get('cost_input'), cat.get('cost_output')):
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
        stale_flat = (t.get('billing_model') == 'flat_subscription'
                      and m.get('normalized_price') not in (None, 0)
                      and not (m.get('price_evidence') or '').startswith('normalized:'))
        if m.get('normalized_price') not in (None, 0) and not stale_flat:
            continue  # already priced (evidence preserved)
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
            fill_public_price(m, cost_in, (cat or {}).get('cost_output'))
        elif model == 'per_request':
            cost = t.get('plan_cost'); reqs = t.get('requests'); tpr = t.get('tokens_per_request')
            if not (cost and reqs and tpr):
                # budget-unknown fallback (registry-maintenance.md design):
                # blended estimate 0.96×sticker-in + 0.04×sticker-out — the
                # sub-bucket proxy until per-model req rates are researched
                ci, co = (cat or {}).get('cost_input'), (cat or {}).get('cost_output')
                if ci is None or co is None:
                    for (cp, cm), cr in catalog.items():
                        if cm == m['model'] and cr.get('cost_input') is not None and cr.get('cost_output') is not None:
                            ci, co = cr['cost_input'], cr['cost_output']
                            break
                    else:
                        gaps.append((m['provider'], m['model'], 'no req-rate AND no sticker for blended est'))
                        continue
                price = round(0.96 * float(ci) + 0.04 * float(co), 4)
                evidence = 'normalized:sub-bucket(blended est)'
                fill_public_price(m, ci, co)
            else:
                price = round(float(cost) / float(reqs) / float(tpr) * 1e6, 4)
                evidence = 'normalized:sub-bucket'
                fill_public_price(m, (cat or {}).get('cost_input'), (cat or {}).get('cost_output'))
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
                # non-included lane: priced ONLY if a temporary discount makes
                # it worth routing (free promo lanes) — otherwise PAYG gap
                if any(True for d in active_discounts(m['provider'], m['model'])
                       if d.get('discount_type') == 'free'):
                    price, evidence = 0.0, 'temporary free lane'
                else:
                    gaps.append((m['provider'], m['model'], 'outside flat-plan included list (PAYG)'))
                    continue
                cat = None
            else:
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
                fill_public_price(m, cost_in, cost_out)
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
