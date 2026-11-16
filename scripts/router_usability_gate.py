#!/usr/bin/env python3
"""Usability gate (TR-369) — presence is not usability, and silence is not a reason.

WHY THIS EXISTS
---------------
The registry grew to 2,064 rows while only a few hundred could actually be put in
a ladder, and nothing failed. Every pass looked healthy because it counted ROWS.
Bane's law: "a NULL must carry a reason" — an unexplained gap is junk, not data.

THE RULE
--------
A live lane row must be in exactly one of two states:

  USABLE   live + priced + carries at least one category tier
  EXPLAINED  not usable, and the reason is DERIVED FROM THE DATA, not asserted:
             - disabled / archived / expired   (deliberately out of service)
             - unpriced                        (cannot be ranked until priced)
             - no tier row for this model      (never measured)
             - only tiers below the blank default (measured, but worse than
               every unmeasured lane — evidence that cannot order anything)

Anything else is a DEFECT: a lane the resolver will happily consider and then
rank on nothing. Those are the ones this gate fails on.

RATCHET, NOT A CLIFF
--------------------
Today's genuinely-unmeasured count is large; failing every commit on it would
stop the fleet instead of cleaning it. So the count is compared against a
committed BASELINE and may only DECREASE (--ratchet writes a new, lower number).
A regression — new live lanes with no evidence, or evidence being dropped — makes
the gate red immediately. The number is in git, so progress and drift are both
visible in the diff.

    router_usability_gate.py              # report + ratchet check (exit 1 if worse)
    router_usability_gate.py --ratchet    # tighten the baseline to today's count
    router_usability_gate.py --json       # machine-readable report

Read-only except --ratchet, which writes the baseline file.
"""
import argparse
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DATA = os.environ.get('ROUTING_DATA_DIR') or os.path.join(ROOT, 'data', 'tables')
BASELINE = os.path.join(ROOT, 'data', 'state', 'usability-baseline.json')
MISSING_TIER_DEFAULT = -1


def _jsonl(name):
    out = []
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _live(row, today='2026-11-04'):
    if row.get('disabled') or row.get('archive') or row.get('archived'):
        return False
    vt = row.get('valid_to')
    if vt and str(vt)[:10] < today:
        return False
    return True


def classify():
    """Return (usable, explained, defects, detail) over the live lane rows."""
    models = _jsonl('models.jsonl')
    tiers = _jsonl('model_tier.jsonl')

    tier_by_model = collections.defaultdict(dict)
    for t in tiers:
        if t.get('tier') is not None:
            tier_by_model[t.get('model')][t.get('category')] = t.get('tier')

    usable, explained, defects = [], [], []
    reasons = collections.Counter()

    for row in models:
        if not _live(row):
            continue
        name = row.get('model')
        cats = tier_by_model.get(name) or {}
        priced = row.get('normalized_price') is not None
        lane = {'provider': row.get('provider'), 'model': name}

        if cats and priced:
            best = max(cats.values())
            if best > MISSING_TIER_DEFAULT:
                usable.append(lane)
                continue
            reasons['tiers_below_blank_default'] += 1
            explained.append({**lane, 'why': 'tiers_below_blank_default'})
            continue
        if not priced:
            reasons['unpriced'] += 1
            explained.append({**lane, 'why': 'unpriced'})
            continue
        # live + priced + no tier rows at all
        reasons['never_measured'] += 1
        defects.append({**lane, 'why': 'never_measured'})

    return usable, explained, defects, reasons


def _eligibility_census():
    """The resolver's eligibility census, or an explained absence.

    Imported lazily so this report keeps working (with the reason stated) when
    the resolver module cannot be imported — a published list must never fail
    silently, and it must never invent a verdict it could not compute.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import router_spawn
        tables, _src, _fb, _warn = router_spawn._load_registry_with_meta()
        census = router_spawn.eligibility_census(tables)
        return {'rule': router_spawn.ELIGIBILITY_RULE,
                'enabled': router_spawn._eligibility_cfg()['enabled'],
                'live_lanes': census['live_lanes'],
                'counts': census['counts'],
                'share_eligible': census['share_eligible'],
                'evidence_models': census['evidence_models']}
    except Exception as exc:  # noqa: BLE001 — report the gap, never crash the gate
        return {'rule': 'unavailable', 'enabled': None,
                'reason': f'eligibility-census-error: {exc}'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--ratchet', action='store_true',
                    help='write today\'s defect count as the new baseline (may only decrease)')
    args = ap.parse_args()

    usable, explained, defects, reasons = classify()
    total_live = len(usable) + len(explained) + len(defects)
    share = (100.0 * len(usable) / total_live) if total_live else 0.0

    baseline = None
    if os.path.exists(BASELINE):
        try:
            baseline = json.load(open(BASELINE))
        except Exception:
            baseline = None

    # ELIGIBILITY GATE (owner ruling 2026-11-11): the published list carries the
    # resolver's own verdict vocabulary, so a reader of this report and a reader
    # of a resolve payload see the SAME names for the same lanes. Derived from
    # the same tables (perf/bench rows), never re-asserted here.
    eligibility = _eligibility_census()

    report = {
        'live_lane_rows': total_live,
        'usable': len(usable),
        'explained_gaps': len(explained),
        'defects': len(defects),
        'usable_share_pct': round(share, 1),
        'reasons': dict(reasons),
        'baseline_defects': (baseline or {}).get('defects'),
        'eligibility': eligibility,
    }

    if args.json:
        print(json.dumps({**report,
                          'defect_samples': defects[:25],
                          'explained_samples': explained[:10]}, indent=1))
    else:
        print('=' * 70)
        print('USABILITY GATE — presence is not usability')
        print('=' * 70)
        print(f'  live lane rows      {total_live:>6}')
        print(f'  USABLE              {len(usable):>6}   ({share:.1f}%)')
        print(f'  explained gaps      {len(explained):>6}   (reason derived from data)')
        print(f'  DEFECTS             {len(defects):>6}   (live, priced, never measured)')
        if eligibility.get('counts'):
            print('  eligibility (resolver verdicts, same names as the payload):')
            for k, v in sorted(eligibility['counts'].items()):
                print(f'      {k:<28}{v:>6}')
        for k, v in reasons.most_common():
            print(f'      {k:<28}{v:>6}')
        if defects:
            print('\n  sample defects (a live lane the resolver will rank on nothing):')
            for d in defects[:10]:
                print(f'      {d["provider"]:<18}{str(d["model"])[:40]}')

    failed = False
    if baseline and baseline.get('defects') is not None:
        allowed = baseline['defects']
        if len(defects) > allowed:
            failed = True
            if not args.json:
                print(f'\nFAIL: defects rose to {len(defects)} (baseline allows {allowed}). '
                      'New live lanes without evidence, or evidence was dropped.')
        else:
            if not args.json:
                print(f'\nOK: {len(defects)} defects <= baseline {allowed}'
                      f'{" (ratchet down)" if len(defects) < allowed else ""}')

    if args.ratchet:
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        prev = (baseline or {}).get('defects')
        if prev is not None and len(defects) > prev:
            print(f'refusing to ratchet UP ({prev} -> {len(defects)}): fix the regression first')
            return 1
        json.dump({
            'defects': len(defects),
            'usable': len(usable),
            'live_lane_rows': total_live,
            'usable_share_pct': round(share, 1),
            'reasons': dict(reasons),
            'note': ('Usability ratchet: `defects` (live+priced+never-measured lanes) may only '
                     'decrease. Regenerate with scripts/router_usability_gate.py --ratchet.'),
        }, open(BASELINE, 'w'), indent=1)
        print(f'baseline written: defects={len(defects)} usable={len(usable)} '
              f'({share:.1f}%) -> {BASELINE}')

    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
