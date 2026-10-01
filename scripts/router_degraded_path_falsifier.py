#!/usr/bin/env python3
"""router_degraded_path_falsifier.py — can ANY unservable rating still exit as a bare no-hops? (TR-194)

WHY THIS EXISTS
---------------
The 2026-09-26 incident: the proxy produced bare `no-hops` ledger rows (no
chain, no exclusion, no reason) for ratings nothing in the registry could
satisfy. The code half of the fix landed in c49b18a — the eligibility-stage
early return in `router_spawn.resolve` no longer dead-ends before the gate
stage and the fallback-lane section; an unsatisfiable rating now either

  * SERVES DEGRADED off an always-run fallback lane (each hop carrying
    `requirements_unmet`), or
  * FAILS CLOSED with the structured error doc (`reasons` + `exclusions`
    lists present, the error message naming the cause).

This script is the PROOF half: it replays a frozen sample of REAL ledger
ratings through the resolver's own semantics and classifies every
UNSATISFIABLE rating as exactly one of

  SERVED-DEGRADED   a fallback lane serves it; the unmet requirement is named
  FAIL-CLOSED       the structured error doc (reasons/exclusions lists present)
  BARE-NO-HOPS      VIOLATION — the incident shape: no chain, no structure

The sample and the unsatisfiable measurement are TR-185's
(router_tier_coverage.load_sample / load_tables / build / clears) — reused,
not re-derived. Two independent eligibility derivations must agree:
`_build_chain` emptiness (the resolver's own builder) and
`any(clears(...))` over tier_coverage.build()'s eligible pool. A
disagreement is itself reported as a violation of the measurement.

SEMANTICS (the resolver's, never a re-implementation)
-----------------------------------------------------
Eligibility comes from `router_spawn._build_chain` (the same function
`resolve` calls). Gate state is NOT part of the claim this falsifies: the
2026-09-26 incident was an ELIGIBILITY dead-end (94% of rated no-hops rows),
and the fallback stage is therefore replayed with every provider's quota row
PRESENT and OPEN (the resolver's policy plane is fail-closed on absence, so
absent state would silently gate the fallback lanes and the run would
measure the test rig, not the code). Health/circuit state starts empty for
the same reason. Run with --state-dir to replay the gates of a real state
dir instead.

Exit code: 0 when no BARE-NO-HOPS verdict exists (the claim holds on this
sample), 1 when at least one rating still dead-ends bare (the claim is
falsified — fix the code, then re-run). A reporting tool that cannot fail
proves nothing.

CLI
  router_degraded_path_falsifier.py [--sample rated|rated-nohops] [--ts-max EPOCH]
                                    [--json] [--ledger PATH] [--registry PATH]
                                    [--state-dir PATH]
"""
import argparse
import collections
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, 'scripts'))  # sibling tools

# The resolver's OWN semantics (c49b18a) — imported, never re-derived.
from router_spawn import (_alias_tiers, _fold_tier_names,  # noqa: E402
                          _build_chain, _resolve_fallback)
# TR-185's measurement plane — the sample loader and eligibility census.
from router_tier_coverage import load_tables, load_sample, build, clears  # noqa: E402

LEDGER = os.environ.get('ROUTER_OUTCOMES', os.path.join(_REPO, 'data', 'state', 'outcomes.jsonl'))
REGISTRY = os.environ.get('ROUTING_REGISTRY', os.path.join(_REPO, 'registry.json'))

SATISFIABLE = 'SATISFIABLE'
SERVED_DEGRADED = 'SERVED-DEGRADED'
FAIL_CLOSED = 'FAIL-CLOSED'
BARE_NO_HOPS = 'BARE-NO-HOPS'          # the incident shape — always a VIOLATION


def _resolver_replay(tables, reqs_items, qs=None, hs=None, cs=None, qgates=None,
                     profile_id=None, limit=1024):
    """The two stages `resolve` runs for a rating, in order (c49b18a shape).

    Returns the outcome OBJECT the resolver would hand the proxy:
      {'chain': [...], 'head': ...}                    — satisfiable
      {'degraded_fallback': True, 'fallback': [...]}   — served degraded
      {'error': ..., 'reasons': [...], 'exclusions': [...]} — fail closed
      {'error': 'no chain'}                            — the BARE incident shape
    """
    chain = _build_chain(tables, reqs_items, limit=limit)
    if chain:
        return {'chain': chain, 'head': chain[0]}
    # Stage 4.5: the always-run lanes, under the caller's gate plane.
    lanes = _resolve_fallback(tables, qs or {}, hs or {}, cs or {}, reqs_items,
                              limit=limit, profile_id=profile_id, qgates=qgates or {})
    if lanes:
        return {'degraded_fallback': True, 'fallback': lanes, 'head': lanes[0]}
    # Nothing eligible, no fallback could serve: the post-c49b18a error doc.
    return {'error': 'no chain — no eligible model and no fallback lane could serve',
            'reasons': [], 'exclusions': []}


def _open_quota_plane(tables):
    """Every provider PRESENT and OPEN (the falsifier's gate plane — see docstring)."""
    provs = {r.get('id') for r in (tables.get('providers') or []) if r.get('id')}
    provs |= {m.get('provider') for m in (tables.get('models') or []) if m.get('provider')}
    return {p: {'status': 'open'} for p in provs}


def _unmet_named(fallback_head, tiers, folded, reqs_items):
    """The unmet requirement named, straight off the fallback hop (or re-derived
    from the lane's alias tiers when the hop carries no list)."""
    unmet = fallback_head.get('requirements_unmet')
    if unmet:
        return [{'category': c, 'required': lvl, 'have': have} for c, lvl, have in unmet]
    mt = _alias_tiers(tiers, folded, fallback_head.get('model'))
    return [{'category': c, 'required': lvl,
             'have': (mt.get(c) if mt.get(c) is not None else -1)}
            for c, lvl in reqs_items
            if (mt.get(c) if mt.get(c) is not None else -1) < lvl]


def classify_outcome(outcome):
    """Outcome object -> (verdict, detail). The classifier the tests pin.

    BARE-NO-HOPS is decided on SHAPE (no chain, no fallback, no structured
    lists), so it fires on the pre-c49b18a resolver too — the falsifier must
    be able to fail.
    """
    if not isinstance(outcome, dict):
        return BARE_NO_HOPS, {'why': 'resolver outcome is not an object'}
    if outcome.get('degraded_fallback') and outcome.get('fallback'):
        return SERVED_DEGRADED, {'fallback_head': {
            'provider': outcome['fallback'][0].get('provider'),
            'model': outcome['fallback'][0].get('model')}}
    if outcome.get('chain'):
        return SATISFIABLE, {'chain_length': len(outcome['chain'])}
    if 'error' in outcome:
        structured = (isinstance(outcome.get('reasons'), list)
                      and isinstance(outcome.get('exclusions'), list))
        if structured:
            return FAIL_CLOSED, {'error': outcome.get('error'),
                                 'reasons': len(outcome.get('reasons') or []),
                                 'exclusions': len(outcome.get('exclusions') or [])}
        return BARE_NO_HOPS, {'why': "error doc without structured reasons/exclusions",
                              'error': outcome.get('error')}
    return BARE_NO_HOPS, {'why': 'no chain, no fallback, no error structure'}


def falsify(tables, sample_rows, qs=None, hs=None, cs=None, qgates=None):
    """Per-rating verdicts + the before/after census, over a frozen sample.

    `before` models the incident semantics (the pre-c49b18a early return):
    every UNSATISFIABLE rating dead-ended bare. `after` is the replay.
    """
    tiers_t, folded_t, eligible, registry = build(tables)
    # Default gate plane: every provider PRESENT and OPEN. The policy plane is
    # fail-closed on absence (TR-203), so a bare {} would silently gate the
    # fallback lanes and the run would measure the test rig, not the code.
    if qs is None:
        qs = _open_quota_plane(tables)
    tiers = {}
    for r in tables.get('model_tier') or []:
        tiers.setdefault(r.get('model'), {})[r.get('category')] = r.get('tier')
    folded = _fold_tier_names(tiers)

    rows = []
    for row, reqs in sample_rows:
        reqs_items = sorted(reqs.items())
        e = any(clears(_alias_tiers(tiers_t, folded_t, m.get('model')), reqs)
                for m in eligible)
        outcome = _resolver_replay(tables, reqs_items, qs=qs, hs=hs, cs=cs,
                                   qgates=qgates, profile_id=row.get('profile_id'))
        verdict, detail = classify_outcome(outcome)
        # Two derivations of the same fact must agree: the resolver's builder
        # (chain emptiness) and tier_coverage's clears() over the eligible pool.
        derivation_ok = (verdict == SATISFIABLE) == bool(e)
        if verdict == SERVED_DEGRADED:
            detail['unmet'] = _unmet_named(outcome['fallback'][0], tiers, folded, reqs_items)
        rows.append({'session_id': row.get('session_id'),
                     'ts': row.get('ts'),
                     'required_categories': reqs,
                     'complexity_source': row.get('complexity_source'),
                     'hops_attempted': row.get('hops_attempted'),
                     'unsatisfiable': verdict != SATISFIABLE,
                     'derivation_disagreement': not derivation_ok,
                     'verdict': verdict, **detail})

    n = len(rows)
    unsat = [r for r in rows if r['unsatisfiable']]
    after = collections.Counter(r['verdict'] for r in unsat)
    bare = [r for r in unsat if r['verdict'] == BARE_NO_HOPS]
    disagree = [r for r in rows if r['derivation_disagreement']]

    # Per-category before/after (the categories the classifier actually asked).
    cats = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        for c in (r['required_categories'] or {}):
            cats[c]['asked'] += 1
            if r['unsatisfiable']:
                cats[c]['unsat_before'] += 1          # pre-fix: all of these were bare
                cats[c][f"after_{r['verdict']}"] += 1
    return {
        'n': n,
        'satisfiable': n - len(unsat),
        'unsatisfiable': len(unsat),
        'before': {'unsatisfiable_bare_no_hops': len(unsat),
                   'rate': (len(unsat) / n) if n else None,
                   'note': 'incident semantics (pre-c49b18a): every unsatisfiable '
                           'rating dead-ended as a bare no-hops (no chain, no reason)'},
        'after': {'served_degraded': after.get(SERVED_DEGRADED, 0),
                  'fail_closed': after.get(FAIL_CLOSED, 0),
                  'bare_no_hops_violations': after.get(BARE_NO_HOPS, 0),
                  'bare_rate': (after.get(BARE_NO_HOPS, 0) / n) if n else None},
        'per_category': {c: dict(v) for c, v in sorted(cats.items())},
        'violations': bare,
        'derivation_disagreements': len(disagree),
        'rows': rows,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--sample', choices=('rated', 'rated-nohops'), default='rated-nohops')
    ap.add_argument('--ledger', default=LEDGER)
    ap.add_argument('--registry', default=REGISTRY)
    ap.add_argument('--ts-max', type=float, default=None,
                    help='freeze the sample: ignore ledger rows with ts > this epoch')
    ap.add_argument('--state-dir', default=None,
                    help='replay the gates of a REAL router state dir (quota/health/'
                         'circuit) instead of the all-open plane')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    tables, src = load_tables(args.registry)
    rows = load_sample(args.ledger, args.sample, ts_max=args.ts_max)

    qs, hs, cs, qgates = None, None, None, None
    if args.state_dir:
        # Real-gate replay: the same three files resolve() reads, same shapes.
        def _load(name):
            try:
                with open(os.path.join(args.state_dir, name)) as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001 — fail-open, same as the resolver
                return {}
        qdoc = _load('quota-state.json')
        qs = qdoc.get('providers') or {}
        qg = qdoc.get('plan_quota_gates') or {}
        qgates = {k: v for k, v in qg.items() if isinstance(v, dict) and v.get('active')}
        hs = _load('health-state.json').get('providers') or {}
        cs = _load('circuit-state.json').get('pairs') or {}

    res = falsify(tables, rows, qs=qs, hs=hs, cs=cs, qgates=qgates)
    out = {'registry': src, 'ledger': args.ledger, 'sample': args.sample,
           'ts_max': args.ts_max, 'state_dir': args.state_dir,
           'fix_commit': 'c49b18a', 'metric': res}

    if args.json:
        print(json.dumps(out, indent=1, sort_keys=True, default=str))
    else:
        print(f'registry   : {src}')
        print(f'ledger     : {args.ledger}')
        print(f'sample     : {args.sample} — {res["n"]} rated proxied rows'
              + (f', ts <= {args.ts_max}' if args.ts_max is not None else ''))
        if not res['n']:
            print('no sample rows — nothing to falsify')
            return 0
        b, a = res['before'], res['after']
        print(f'BEFORE (incident semantics): unsatisfiable = bare no-hops '
              f'{b["unsatisfiable_bare_no_hops"]}/{res["n"]} = {b["rate"]:.1%}')
        print(f'AFTER  (c49b18a replay)     : '
              f'SERVED-DEGRADED {a["served_degraded"]} | FAIL-CLOSED {a["fail_closed"]} '
              f'| BARE-NO-HOPS {a["bare_no_hops_violations"]} '
              f'(bare rate {a["bare_rate"]:.1%})')
        print('per-category (asked | unsatisfiable-before | served-degraded | fail-closed | bare):')
        for c, v in res['per_category'].items():
            print(f'  {c:<14} asked {v.get("asked", 0):>4}  before {v.get("unsat_before", 0):>4}'
                  f'  deg {v.get("after_SERVED-DEGRADED", 0):>4}'
                  f'  closed {v.get("after_FAIL-CLOSED", 0):>4}'
                  f'  bare {v.get("after_BARE-NO-HOPS", 0):>4}')
        if res['derivation_disagreements']:
            print(f'DERIVATION DISAGREEMENTS: {res["derivation_disagreements"]} '
                  f'(_build_chain emptiness vs tier_coverage.clears disagree)')
        for v in res['violations'][:8]:
            print(f'VIOLATION {v["session_id"]}: {v["required_categories"]} -> '
                  f'{v.get("why", "bare no-hops")}')
    # The falsifier must be able to fail: a bare verdict on this sample is the
    # incident shape alive in the tree — exit 1, never a green lie.
    return 1 if (res['after']['bare_no_hops_violations']
                 or res['derivation_disagreements']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
