#!/usr/bin/env python3
"""TR-070 wave 2 — clear the pricing audit's real burn traps.

Recalibrated audit (2026-09-20) found 50 genuine traps after excluding 661 lanes
whose state was already documented. This fixes the two GROUPS whose remedy is
deterministic evidence:

  * 21 opencode-go lanes stamped "opencode-go: sub-bucket 12/req/31250
    (rate=opencode.ai/go)" and no date. The cited rate page IS the source; its
    catalog observation date is 2026-09-16, so date the stamp. (A named-but-
    undated source is a real trap because it is a staleness risk — exactly what
    "thinks cheap but actually not" means.)

  * 15 zero-priced lanes on plan providers with no story. Apply the xKiro rule
    (already the convention on 15 sibling lanes): a free SKU draws the metered
    window at its paid-sibling's list-equivalent, so normalized carries that
    blend while public_price stays 0 (the monthly-pool truth). Where no PAID
    sibling is priced anywhere in the catalog, stamp window-cost-pending — the
    explicit end state F3 names.

The 14 stale offsets are NOT touched here: re-deriving an offset needs metered
usage (the kimi method) and none of those providers has any (0 metered bases),
so they are an open decision, not a mechanical fix.

Usage:  router_trapfix.py [--apply]     (dry-run by default)
"""
import json
import os
import shutil
import sys
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

MODELS = os.path.join(REPO, 'data', 'tables', 'models.jsonl')

WINDOW_COST_TMPL = (
    'window-cost {day} (xKiro rule): free SKU draws the metered window at its '
    'list-equivalent, so normalized carries the PAID-sibling list blend; '
    'public_price stays 0 = the monthly-pool truth. Router stops preferring '
    'powerful free SKUs. Window-cost-pending: no paid sibling in the models.dev '
    'catalog (TR-070); stamp when the paid SKU is listed.'
)

#: Vendor -> the models.dev provider id that is authoritative for its prices.
VENDOR_PROVIDER = {
    'mistralai': 'mistral', 'sensenova': 'sensenova', 'google': 'google',
    'xiaomi': 'xiaomi-token-plan-cn', 'poolside': 'poolside',
}


def find_paid_sibling(md, model):
    """Paid-sibling list blend for a free SKU -> dict, or None when unmatched.

    STRICT exact-leaf matching only: a prefix match is a DIFFERENT model, so
    'minimax-m2' must never price 'minimax-m2.1-highspeed'. When resellers
    disagree on the leaf there is no single "list-equivalent", so the caller
    gets {'ambiguous': [...]} and stamps pending with the evidence recorded.
    """
    leaf = model.split('/')[-1]
    stem = leaf.replace(':free', '').replace('-free', '')
    vendor = model.split('/')[0] if '/' in model else ''
    want = VENDOR_PROVIDER.get(vendor, vendor)
    exact, loose = [], []
    for pid, mods in md.items():
        for mid, pr in mods.items():
            if not (pr[0] or pr[1]):
                continue          # a free listing is not a paid basis
            if mid == stem:
                exact.append((pid, mid, pr))
            elif mid.startswith(stem) or stem.startswith(mid):
                loose.append((pid, mid, pr))
    if not exact:
        return None if not loose else {
            'unmatched': True,
            'near': [(p, m, pr[0], pr[1]) for p, m, pr in loose[:3]]}
    vendor_hits = [c for c in exact if c[0] == want]
    if vendor_hits:
        exact = vendor_hits
    blends = {(c[2][0] + c[2][1]) / 2 for c in exact}
    if len(blends) > 1:
        return {'ambiguous': [{'provider': p, 'model': m, 'in': pr[0],
                               'out': pr[1]} for p, m, pr in sorted(exact)]}
    pid, mid, pr = sorted(exact)[0]
    return {'provider': pid, 'model': mid, 'in': pr[0], 'out': pr[1],
            'blend': round((pr[0] + pr[1]) / 2, 6), 'reseller': pid != want}


def main():
    apply = '--apply' in sys.argv
    import router_pricing_audit as pa

    md = pa.modelsdev()
    rows = [json.loads(l) for l in open(MODELS) if l.strip()]
    today = date.today().isoformat()

    fixed_date = fixed_wc = fixed_pending = held = 0
    for r in rows:
        ev = str(r.get('price_evidence') or '')
        if 'sub-bucket 12/req/31250' in ev and '2026-09-16' not in ev:
            r['price_evidence'] = ev.replace(
                'opencode-go: sub-bucket 12/req/31250 (rate=opencode.ai/go)',
                'opencode-go: sub-bucket 12/req/31250 '
                '(rate=opencode.ai/go, observed 2026-09-16)')
            fixed_date += 1
        elif (r.get('normalized_price') == 0 and not r.get('disabled')
              and not r.get('archive')):
            # NOTE: pending lanes are re-examined, not skipped — 'pending' is a
            # promise to look again, and a paid sibling can appear later. The
            # write below only lands when the classification actually changes.
            sib = find_paid_sibling(md, r['model'])
            if sib and 'blend' in sib:
                tail = f" (sibling {sib['provider']}/{sib['model']} " \
                       f"{sib['in']}/{sib['out']} per M"
                tail += ', reseller listing)' if sib.get('reseller') else ')'
                print('  %-12s %-38s -> window-cost %.4f %s' % (
                    r['provider'], r['model'][:38], sib['blend'], tail))
                r['normalized_price'] = sib['blend']
                new_ev = WINDOW_COST_TMPL.format(day=today) + tail
                if new_ev == ev:
                    continue          # re-examined, nothing changed
                r['price_evidence'] = new_ev
                fixed_wc += 1
            elif sib and 'ambiguous' in sib:
                # Resellers disagree on this exact leaf -> no single
                # list-equivalent. Stamp pending, record what was seen.
                seen = '; '.join('%s/%s %s/%s' % (x['provider'], x['model'],
                                                  x['in'], x['out'])
                                 for x in sib['ambiguous'][:4])
                print('  %-12s %-38s -> PENDING (resellers disagree: %s)' % (
                    r['provider'], r['model'][:38], seen))
                r['price_evidence'] = (
                    f'window-cost-pending {today} (TR-070): reseller catalog '
                    f'listings for this exact leaf disagree ({seen}) so there is '
                    'no single list-equivalent to bill the window at; stamp when '
                    'the vendor publishes a rate.')
                fixed_pending += 1
            elif sib and 'near' in sib:
                # Prefix-only matches are DIFFERENT models (ling-3.0-flash vs
                # ling-3.0-flash-sante; minimax-m2 vs m2.1-highspeed), so there is
                # no defensible sibling rate to bill the window at. Record the
                # candidates that were rejected and hold the lane.
                seen = '; '.join('%s/%s %s/%s' % x for x in sib['near'])
                print('  %-12s %-38s -> HELD (no exact sibling; prefix-only '
                      'candidates rejected: %s)' % (
                          r['provider'], r['model'][:38], seen))
                r['price_evidence'] = (
                    f'window-cost-pending {today} (TR-070): no EXACT paid sibling '
                    f'for this leaf — only prefix matches ({seen}), which are '
                    'different models and must not be used to price the window. '
                    'Stamp when the vendor lists this SKU or its exact sibling.')
                held += 1
            else:
                print('  %-12s %-38s -> window-cost-pending (no paid sibling)' % (
                    r['provider'], r['model'][:38]))
                r['price_evidence'] = (
                    f'window-cost-pending {today} (TR-070): zero-price SKU on a '
                    'plan provider with no paid sibling priced anywhere in the '
                    'models.dev catalog; stamp the window-cost when the paid SKU '
                    'is listed. Until then the router has no basis to prefer it.')
                fixed_pending += 1

    print(f'\nfixed: {fixed_date} dated opencode-go stamps, '
          f'{fixed_wc} window-costs, {fixed_pending} pending tags, '
          f'{held} held for a human call (prefix-only match)')
    if not apply:
        print('DRY RUN — nothing written. Re-run with --apply.')
        return 0
    shutil.copy(MODELS, MODELS + '.bak')
    with open(MODELS, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    print(f'wrote {MODELS} (backup at {MODELS}.bak)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
