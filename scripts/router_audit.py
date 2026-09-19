#!/usr/bin/env python3
"""router_audit.py — TR-064 coverage audit.

Answers: which ACTIVE, PRICED lanes can actually be routed, and where does
their capability evidence come from?

A lane is routeable only if `model_tier` carries a row for every category a
profile can require. A lane with no tier rows is chain-invisible (the resolver
reads a missing tier as -1) — today that happens silently. This command makes
it loud, with provenance (measured | bench | family | estimate) so inherited
evidence is never mistaken for measured evidence.

Usage:
  python3 scripts/router_audit.py audit-tiers [--json] [--provider P]
                                              [--explain MODEL] [--no-fail]
Exit: 0 = every active priced lane has coverage and provenance;
      2 = uncovered lanes and/or tier rows missing provenance (--no-fail: 0).
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROVENANCE = ('measured', 'bench', 'family', 'estimate')


def registry_path():
    return os.environ.get('ROUTING_REGISTRY') or os.path.join(REPO, 'registry.json')


def load_tables(path=None):
    with open(path or registry_path()) as f:
        return json.load(f).get('tables', {})


def active_lanes(tables):
    """Lanes the router could select: live, not archived, not disabled, priced."""
    out = []
    for m in tables.get('models') or []:
        if m.get('valid_to') or m.get('archive') or m.get('disabled'):
            continue
        price = m.get('normalized_price')
        if price is None and m.get('public_price') is None:
            continue  # unpriced lanes are never selected — different audit
        out.append(m)
    return out


def category_universe(tables):
    """Categories any profile can require — the vocabulary that decides
    eligibility. Data-driven: never a hardcoded category list."""
    cats = {r.get('category') for r in tables.get('task_profile_requirements') or []
            if r.get('category')}
    return sorted(cats)


def lane_report(tables, lane, universe, tiers_by_model, profile_reqs):
    model = lane.get('model')
    rows = tiers_by_model.get(model, [])
    covered = {r['category'] for r in rows}
    missing = [c for c in universe if c not in covered]
    srcs = Counter(r.get('tier_source') for r in rows)
    domain = [s for s in srcs if s] or ['none']
    eligible = 0
    eligible_evidenced = 0
    tiers = {r['category']: r['tier'] for r in rows}
    for pid, reqs in profile_reqs.items():
        if not all(tiers.get(c, -1) >= lvl for c, lvl in reqs.items()):
            continue
        eligible += 1
        # evidenced = EVERY requirement category of that profile has a real tier
        # row for this lane; a pass resting on the -1 default is not evidence.
        if reqs and all(c in tiers for c in reqs):
            eligible_evidenced += 1
    return {
        'provider': lane.get('provider'), 'model': model,
        'price': lane.get('normalized_price'),
        'covered': len(covered), 'missing': missing,
        'coverage_pct': round(100.0 * len(covered) / max(1, len(universe)), 1),
        'tier_source': domain[0] if len(domain) == 1 else 'mixed',
        'tier_sources': dict(srcs),
        'eligible_profiles': eligible,
        'eligible_profiles_evidenced': eligible_evidenced,
    }


def audit_tiers(tables, provider=None):
    universe = category_universe(tables)
    tiers_by_model = defaultdict(list)
    unprovenanced = 0
    for t in tables.get('model_tier') or []:
        tiers_by_model[t.get('model')].append(t)
        if not t.get('tier_source'):
            unprovenanced += 1
    profile_reqs = {}
    reqs_by_pid = defaultdict(dict)
    for r in tables.get('task_profile_requirements') or []:
        reqs_by_pid[r['task_id']][r['category']] = r.get('level', 0)
    profile_reqs = dict(reqs_by_pid)

    lanes = active_lanes(tables)
    if provider:
        lanes = [l for l in lanes if l.get('provider') == provider]
    reports = [lane_report(tables, l, universe, tiers_by_model, profile_reqs)
               for l in lanes]
    covered = [r for r in reports if r['covered'] > 0 and not r['missing']]
    partial = [r for r in reports if r['covered'] > 0 and r['missing']]
    invisible = [r for r in reports if r['covered'] == 0]
    # the metric that matters: how many profiles can this lane actually serve?
    routeable = [r for r in reports if r['eligible_profiles'] > 0]
    evidenced = [r for r in reports if r['eligible_profiles_evidenced'] > 0]
    default_driven = [r for r in routeable if r['eligible_profiles_evidenced'] == 0]
    unserved = [r for r in reports if r['eligible_profiles'] == 0]
    by_provider = defaultdict(lambda: {'lanes': 0, 'invisible': 0, 'evidenced': 0})
    for r in reports:
        p = by_provider[r['provider']]
        p['lanes'] += 1
        p['invisible'] += (r['covered'] == 0)
        p['evidenced'] += (r['eligible_profiles_evidenced'] > 0)
    return {
        'universe': universe,
        'lanes': len(reports),
        'full_coverage': len(covered),
        'partial_coverage': len(partial),
        'invisible': len(invisible),
        'routeable': len(routeable),
        'routeable_evidenced': len(evidenced),
        'routeable_default_driven': len(default_driven),
        'serves_no_profile': len(unserved),
        'routeable_pct': round(100.0 * len(routeable) / max(1, len(reports)), 1),
        'unprovenanced_tier_rows': unprovenanced,
        'provenance_all': dict(Counter(r['tier_source'] for r in covered + partial)),
        'by_provider': {k: dict(v) for k, v in sorted(by_provider.items())},
        'invisible_lanes': sorted(f"{r['provider']}/{r['model']}" for r in invisible),
        'serves_no_profile_lanes': sorted(f"{r['provider']}/{r['model']}" for r in unserved),
        'partial_lanes': sorted(f"{r['provider']}/{r['model']}" for r in partial),
        'lanes_detail': reports,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('audit-tiers')
    a.add_argument('--json', action='store_true')
    a.add_argument('--provider')
    a.add_argument('--explain', metavar='MODEL')
    a.add_argument('--no-fail', action='store_true',
                   help='always exit 0 (report-only mode)')
    args = ap.parse_args()

    tables = load_tables()
    rep = audit_tiers(tables, provider=args.provider)

    if args.explain:
        for r in rep['lanes_detail']:
            if r['model'] == args.explain:
                print(json.dumps(r, indent=1))
        return 0

    if args.json:
        out = {k: v for k, v in rep.items() if k != 'lanes_detail'}
        print(json.dumps(out, indent=1))
    else:
        print(f"category universe : {len(rep['universe'])} categories")
        print(f"active priced lanes: {rep['lanes']}")
        print(f"  full coverage    : {rep['full_coverage']}")
        print(f"  partial coverage : {rep['partial_coverage']}")
        print(f"  INVISIBLE (0 tiers): {rep['invisible']}")
        print(f"  ROUTEABLE (serves >=1 profile): {rep['routeable']} "
              f"({rep['routeable_pct']}%)")
        print(f"    of those, EVIDENCE-BACKED : {rep['routeable_evidenced']}")
        print(f"    DEFAULT-DRIVEN (missing-tier -1 satisfies the bar): "
              f"{rep['routeable_default_driven']}")
        print(f"  serves NO profile: {rep['serves_no_profile']}")
        print(f"  provenance (covered lanes): {rep['provenance_all']}")
        print(f"  tier rows missing provenance: {rep['unprovenanced_tier_rows']}")
        print('  per provider (evidence-backed lanes/total): ' + ', '.join(
            f"{p}:{v['evidenced']}/{v['lanes']}" for p, v in rep['by_provider'].items()))
        if rep['invisible_lanes']:
            print(f"\ninvisible lanes ({len(rep['invisible_lanes'])}) — excluded from every"
                  f" requirement-bearing chain:")
            for l in rep['invisible_lanes'][:40]:
                print(f"  {l}")
            if len(rep['invisible_lanes']) > 40:
                print(f"  … +{len(rep['invisible_lanes']) - 40} more")

    bad = rep['invisible'] > 0 or rep['unprovenanced_tier_rows'] > 0
    return 0 if (args.no_fail or not bad) else 2


if __name__ == '__main__':
    sys.exit(main())
