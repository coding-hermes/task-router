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

# TR-390: how a tier row's provenance classifies for the measured-vs-estimate
# report. Derived from tier_source, never asserted: 'bench'/'measured' rows
# name a measurement (benchmarks.jsonl row or a lane-carried perf_* column),
# 'estimate'/'family' rows name a documented fill (survey estimate or alias
# inheritance). Any other source lands in 'other' so the per-category
# measured + estimate + other identity always reconciles against the raw
# model_tier row count.
MEASURED_TIER_SOURCES = ('bench', 'measured')
ESTIMATE_TIER_SOURCES = ('estimate', 'family')


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


def category_census():
    """Per-category measured-vs-estimate census over the tier rows (TR-390).

    WHY: 14 of the 24 capability categories were populated mostly by
    estimates, and nothing distinguished a measured tier from a guessed one.
    This block counts, for EVERY registry category, the tier rows classified
    by their own tier_source: measured (bench | measured) vs estimate
    (estimate | family) vs other (a source this report does not know —
    reported, never silently dropped). The category list comes from
    capability_vocab (the one vocabulary the seed/server/spawn spell), so a
    category with zero rows still appears.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import capability_vocab
        cats = list(capability_vocab.PERF_CATEGORIES)
    except Exception as exc:  # noqa: BLE001 — report the gap, never crash the gate
        return {'categories': {}, 'totals': {},
                'reason': f'capability-vocab-error: {exc}'}
    counts = {c: {'measured': 0, 'estimate': 0, 'other': 0} for c in cats}
    for t in _jsonl('model_tier.jsonl'):
        c = t.get('category')
        if c not in counts:
            continue  # a tier row outside the vocabulary is the vocab's bug, not this report's
        src = t.get('tier_source')
        if src in MEASURED_TIER_SOURCES:
            counts[c]['measured'] += 1
        elif src in ESTIMATE_TIER_SOURCES:
            counts[c]['estimate'] += 1
        else:
            counts[c]['other'] += 1
    totals = {'measured': 0, 'estimate': 0, 'other': 0}
    for c in counts.values():
        for k in totals:
            totals[k] += c[k]
    for c in counts.values():
        c['total'] = c['measured'] + c['estimate'] + c['other']
    totals['total'] = sum(totals.values())
    return {'categories': counts, 'totals': totals}


def profile_gap_census():
    """AC2 (TR-390): the 20 cheapest live priced lanes per task profile, and
    the required categories each of them lacks tier evidence for.

    WHY: a lane with no tier row reads the blank default (-1) in EVERY
    category, so a $0 lane ranks on nothing yet still heads every chain the
    price order builds. For each task profile (task_profile_requirements.jsonl)
    this census takes the 20 cheapest live+priced lanes in the resolver's own
    chain order — PAYG bucket last, then plan_tier, then effective price
    (normalized_price * token_factor), then model, provider — and lists every
    required category that has NO tier row for the lane's exact model id.
    A gap is acceptable ONLY when the lane carries an explicit unknown-by-design
    model_notes.jsonl row (source mentioning TR-390); anything else is an
    unexplained unknown. Categories declared unmeasured
    (unmeasured_categories.jsonl) are still REPORTED as missing — the
    declaration is the by-design reason, and the note cites it — because
    "no lane can hold it" and "this lane was never measured" must stay
    distinguishable in the output.
    """
    models = [m for m in _jsonl('models.jsonl') if _live(m)]
    providers = {p.get('id'): p.get('plan') for p in _jsonl('providers.jsonl')}
    payg = {pid for pid, plan in providers.items() if plan == 'PAYG'}
    priced = [m for m in models if m.get('normalized_price') is not None]

    def _sort_key(m):
        eff = (m.get('normalized_price') or 0.0) * (m.get('token_factor') or 1.0)
        return (1 if m.get('provider') in payg else 0,
                m.get('plan_tier') if m.get('plan_tier') is not None else 1 << 30,
                eff, m.get('model') or '', m.get('provider') or '')

    priced.sort(key=_sort_key)
    tier_by_model = collections.defaultdict(set)
    for t in _jsonl('model_tier.jsonl'):
        if t.get('tier') is not None:
            tier_by_model[t.get('model')].add(t.get('category'))
    req_by_profile = collections.defaultdict(set)
    for r in _jsonl('task_profile_requirements.jsonl'):
        req_by_profile[r['task_id']].add(r['category'])
    noted = {(n.get('provider'), n.get('model')) for n in _jsonl('model_notes.jsonl')
             if 'TR-390' in str(n.get('source') or '')}

    out = {}
    for profile, cats in sorted(req_by_profile.items()):
        lanes = []
        for m in priced[:20]:
            missing = sorted(cats - tier_by_model.get(m.get('model'), set()))
            lanes.append({
                'provider': m.get('provider'), 'model': m.get('model'),
                'price': m.get('normalized_price'),
                'missing': missing,
                'has_tr390_note': (m.get('provider'), m.get('model')) in noted,
            })
        out[profile] = {'required': sorted(cats), 'lanes': lanes}
    return out


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
    ap.add_argument('--profile-census', action='store_true',
                    help='print the AC2 cheapest-20-per-profile evidence census '
                         '(gaps without an explicit TR-390 unknown-by-design note)')
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

    # TR-390: per-category measured-vs-estimate census. Derived from the
    # tier rows' own provenance, never asserted.
    cats_report = category_census()

    report = {
        'live_lane_rows': total_live,
        'usable': len(usable),
        'explained_gaps': len(explained),
        'defects': len(defects),
        'usable_share_pct': round(share, 1),
        'reasons': dict(reasons),
        'baseline_defects': (baseline or {}).get('defects'),
        'eligibility': eligibility,
        'categories': cats_report['categories'],
        'categories_total': cats_report['totals'],
    }
    if cats_report.get('reason'):
        report['categories_reason'] = cats_report['reason']

    census = None
    if args.profile_census:
        census = profile_gap_census()
        unexplained = [(pid, l) for pid, pc in census.items()
                       for l in pc['lanes'] if l['missing'] and not l['has_tr390_note']]
        if not args.json:
            print('\n  profile census (AC2, TR-390 — 20 cheapest priced lanes per profile):')
            print(f'      unexplained gaps (missing evidence, no TR-390 note): {len(unexplained)}')
            for pid, pc in census.items():
                gaps = [l for l in pc['lanes'] if l['missing']]
                noted = [l for l in gaps if l['has_tr390_note']]
                print(f'      {pid:<12} {len(gaps)}/{len(pc["lanes"])} lanes missing evidence; '
                      f'{len(noted)} carry the TR-390 unknown-by-design note')
                for l in gaps:
                    if not l['has_tr390_note']:
                        print(f'          UNEXPLAINED {l["provider"]:<14} {str(l["model"])[:38]} '
                              f'missing: {",".join(l["missing"])}')

    if args.json:
        payload = {**report,
                   'defect_samples': defects[:25],
                   'explained_samples': explained[:10]}
        if census is not None:
            payload['profile_census'] = census
        print(json.dumps(payload, indent=1))
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
        if cats_report.get('categories'):
            ct = cats_report['totals']
            print('  categories (tier rows by provenance, TR-390): '
                  f'measured {ct["measured"]}, estimate {ct["estimate"]}, '
                  f'other {ct["other"]}, total {ct["total"]}')
            for c in sorted(cats_report['categories']):
                row = cats_report['categories'][c]
                print(f'      {c:<16} measured {row["measured"]:>5}   '
                      f'estimate {row["estimate"]:>5}   '
                      f'other {row["other"]:>3}   total {row["total"]:>5}')
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
