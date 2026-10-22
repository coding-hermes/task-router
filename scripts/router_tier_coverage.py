#!/usr/bin/env python3
"""router_tier_coverage.py — can the ratings the classifier ACTUALLY produces be served?

WHY THIS EXISTS
---------------
The proxy ledger records, per routed task, the rating matrix the classifier
produced (`required_categories`: {category: level}) and how many hops the
request took (`hops_attempted`). A rating is UNSATISFIABLE when NO lane in the
registry clears its conjunction of category levels: then the eligibility stage
finds nothing, and every request carrying that matrix dead-ends (or degrades to
an always-run fallback lane that does not meet the bars).

That is a DATA problem (sparse tier coverage exactly where the classifier asks),
not a resolver problem, and it cannot be seen from a per-category census alone:
`terminal` covering 91 models means nothing until you know the conjunction the
classifier asked for. This tool joins the two.

SEMANTICS (deliberately the resolver's, not a re-implementation)
----------------------------------------------------------------
Eligibility mirrors `router_spawn._build_chain`:
  * active lane: not archived, not retired (`row_is_retired`), available_from
    passed, not explicitly disabled;
  * priced (`normalized_price` not NULL) — an unpriced lane is never served;
  * tier per category via `_alias_tiers` (TR-043: a variant inherits its base's
    tier for categories it has no row of its own), missing tier = -1 (the
    registry's blank default), never 0.
Two satisfiability verdicts are reported, because they answer different
questions:
  * `selig` (ELIGIBLE): could this rating have been served by a LIVE, PRICED
    lane? This is the number that corresponds to a dead-end.
  * `sreg`  (REGISTRY): does ANY registry lane clear the conjunction at all,
    ignoring price/lifecycle/disabling? This is "does the tier table know a
    model for this shape of work" — the coverage question.

SAMPLE
------
`--sample rated-nohops` (default): proxied rows that carry a rating
(required_categories non-null) and took zero hops (hops_attempted == 0) — the
rows where the rating visibly did not serve. `--sample rated`: every proxied row
that carries a rating, served or not.

EVIDENCE ATTRIBUTION (TR-185)
-----------------------------
For every category the classifier asks for, the report now joins the perf
EVIDENCE that stands behind the tier table: `tables.model_perf` (mirrored at
data/tables/model_perf.jsonl) carries one row per (model, category) with the
`source` of its perf value — seed-deduped, max perf wins (the winning row's
source is reported, TR-064 R2):
    measured  a live probe was run against the lane itself
    bench     a third-party benchmark overlay landed on the category
    family    the value is inherited from the model's alias family/base
    estimate  a profile-tag estimate (CATEGORY_ESTIMATES), no benchmark exists
Each BLOCKING category (thinnest support in the unsatisfiable rows) gets:
  * its source breakdown (measured=/bench=/family=/estimate=),
  * a measured WHY-THIN statement (which sources are empty/small, what the
    asked level means against that pool — never a guess),
  * two proposals, typed:
      EXPANDS EVIDENCE    run real {category} benchmarks/probes and land them
                          via data/tables/benchmarks.jsonl + router_seed.py
      RELAXES REQUIREMENT lower the asked tier level (owner decision); the
                          report SIMULATES the effect on the same frozen
                          sample and states the delta — no invented perf.

BEFORE/AFTER (AC of TR-185)
---------------------------
`--relax cat:level` (repeatable, or `--relax off`) caps a category's required
level in every rating and re-runs eligibility over the SAME sample rows. With
no flag the report simulates the dominant blocking category at (hardest - 2)
— e.g. terminal 4→2 — and prints before/after unsatisfiable rates with the
sample size n, so the delta is a measurement, not a claim.

CLI
  router_tier_coverage.py [--sample rated|rated-nohops] [--json]
                          [--ledger PATH] [--registry PATH]
                          [--ts-max EPOCH] [--relax cat:level ...|off]
Exit 0 always (reporting tool).
"""
from __future__ import annotations
import argparse
import collections
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, 'scripts'))  # sibling tools
from router_spawn import (row_is_retired, _alias_tiers, _fold_tier_names,  # noqa: E402
                          _today)

REGISTRY = os.environ.get('ROUTING_REGISTRY', os.path.join(_REPO, 'registry.json'))
LEDGER = os.environ.get('ROUTER_OUTCOMES', os.path.join(_REPO, 'data', 'state', 'outcomes.jsonl'))


def load_tables(path) -> tuple:
    """registry.json (live store) -> {tables: {...}}; {} on any error."""
    try:
        with open(path) as f:
            doc = json.load(f)
        t = doc.get('tables') or {}
        if t:
            return t, path
    except Exception:
        pass
    data_dir = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
    t = {}
    for name in ('models', 'model_tier', 'model_perf', 'task_profiles',
                 'task_profile_requirements'):
        p = os.path.join(data_dir, f'{name}.jsonl')
        rows = []
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        t[name] = rows
    return t, data_dir


def load_sample(path, sample, ts_max=None):
    """[(row, reqs_dict)] for proxied rows carrying a rating.

    `ts_max` freezes the sample: the ledger is written by a LIVE proxy, so a
    before/after comparison must cut the sample at one timestamp or the two runs
    would be answering about different requests.
    """
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            if '"router-proxy"' not in line or 'required_categories' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get('source_system') != 'router-proxy':
                continue
            if ts_max is not None and float(d.get('ts') or 0) > ts_max:
                continue
            reqs = d.get('required_categories')
            if not isinstance(reqs, dict) or not reqs:
                continue
            if sample == 'rated-nohops' and d.get('hops_attempted') != 0:
                continue
            out.append((d, {str(k): int(v) for k, v in reqs.items()}))
    return out


def build(tables) -> tuple:
    tiers, folded = {}, None
    for r in tables.get('model_tier') or []:
        tiers.setdefault(r.get('model'), {})[r.get('category')] = r.get('tier')
    folded = _fold_tier_names(tiers)
    today = _today()

    eligible, registry = [], []
    for m in tables.get('models') or []:
        registry.append(m)
        if m.get('archive') or row_is_retired(m):
            continue
        af = m.get('available_from')
        if af and str(af)[:10] > today:
            continue
        if m.get('disabled'):
            continue
        if m.get('normalized_price') is None:
            continue
        eligible.append(m)
    return tiers, folded, eligible, registry


def clears(mt, reqs) -> bool:
    """True when the alias-aware tier dict clears EVERY requirement."""
    for cat, lvl in reqs.items():
        have = mt.get(cat)
        if (have if have is not None else -1) < lvl:
            return False
    return True


def measure(tables, sample_rows):
    tiers, folded, eligible, registry = build(tables)
    # TR-185: _alias_tiers is a pure function of the lane id — memoize it once
    # per lane so the satisfiability pass, the per-category blocking census and
    # the relax simulations all share one alias walk instead of millions.
    tcache = {}

    def at(m):
        t = tcache.get(m)
        if t is None:
            t = _alias_tiers(tiers, folded, m)
            tcache[m] = t
        return t

    ok_e = ok_r = 0
    cat_ask = collections.Counter()          # how often the classifier asks a category
    cat_fail = collections.Counter()         # how often that category was the blocker
    cat_max = {}                             # hardest level asked per category
    per_row = []
    for row, reqs in sample_rows:
        for c, l in reqs.items():
            cat_ask[c] += 1
            cat_max[c] = max(cat_max.get(c, -99), l)
        e = any(clears(at(m.get('model')), reqs) for m in eligible)
        r = any(clears(at(m.get('model')), reqs) for m in registry)
        ok_e += bool(e)
        ok_r += bool(r)
        if not e:
            # which requirements had the thinnest support across ELIGIBLE lanes
            worst = None
            for cat, lvl in reqs.items():
                n = sum(1 for m in eligible
                        if (at(m.get('model')).get(cat)
                            if at(m.get('model')).get(cat) is not None
                            else -1) >= lvl)
                if worst is None or n < worst[1]:
                    worst = (cat, n)
            if worst:
                cat_fail[worst[0]] += 1
        per_row.append({'session_id': row.get('session_id'), 'reqs': reqs,
                        'satisfiable_eligible': bool(e), 'satisfiable_registry': bool(r),
                        'provider': row.get('provider'), 'model': row.get('model'),
                        'hops_attempted': row.get('hops_attempted'),
                        'success': row.get('success')})
    n = len(sample_rows)
    return {
        'n': n,
        'unsat_eligible': n - ok_e, 'sat_eligible': ok_e,
        'unsat_registry': n - ok_r, 'sat_registry': ok_r,
        'rate_eligible': (n - ok_e) / n if n else None,
        'rate_registry': (n - ok_r) / n if n else None,
        'cat_ask': dict(cat_ask), 'cat_fail': dict(cat_fail), 'cat_max': cat_max,
        'rows': per_row,
        # private (stripped from --json): simulation state
        '_tiers': tcache,
        '_eligible': [m.get('model') for m in eligible],
        '_n_registry': len(registry),
    }


def evidence_attribution(tables) -> tuple:
    """Per-category perf-evidence source breakdown (TR-185).

    Mirrors the seed's dedup exactly (router_seed.py: max perf wins per
    (model, category); the WINNING row's source is the evidence source) so the
    counts describe the evidence the tier table was actually built from.
    Returns ({cat: {rows, models, sources{source: n}}}, {source: n} overall).
    """
    rows = tables.get('model_perf') or []
    raw_per_cat = collections.Counter()
    raw_src = collections.Counter()
    best = {}
    for r in rows:
        cat = r.get('category')
        raw_per_cat[cat] += 1
        raw_src[r.get('source')] += 1
        key = (str(r.get('model') or '').lower(), cat)
        p = r.get('perf') or 0
        if key not in best or p > best[key][0]:
            best[key] = (p, r.get('source'))
    per_cat = collections.defaultdict(lambda: {'models': set(), 'src': collections.Counter()})
    for (m, cat), (p, src) in best.items():
        e = per_cat[cat]
        e['models'].add(m)
        e['src'][src] += 1
    out = {cat: {'rows': raw_per_cat[cat], 'models': len(e['models']),
                 'sources': dict(e['src'])}
           for cat, e in per_cat.items()}
    return out, dict(raw_src)


def relax_reqs(reqs, caps) -> dict:
    """Cap the required level of every capped category (min keeps lower asks)."""
    return {c: (min(l, caps[c]) if c in caps else l) for c, l in reqs.items()}


def simulate_relax(res, caps) -> dict:
    """Re-run ELIGIBLE satisfiability over the SAME sample with capped reqs."""
    tiers, eligible, rows = res['_tiers'], res['_eligible'], res['rows']
    sat = 0
    for row in rows:
        reqs = relax_reqs(row['reqs'], caps)
        sat += any(clears(tiers[m], reqs) for m in eligible)
    n = len(rows)
    return {'n': n, 'sat': sat, 'unsat': n - sat,
            'rate': (n - sat) / n if n else None}


def coverage_table(tables, cats=None, levels=None):
    """Per-category lane coverage: how many lanes carry a tier, and how many
    clear the level the classifier actually asks most often."""
    tiers, folded, eligible, registry = build(tables)
    tier_models = collections.defaultdict(set)
    for r in tables.get('model_tier') or []:
        tier_models[r.get('category')].add(str(r.get('model')).lower())
    out = {}
    for cat in sorted(cats or tier_models):
        n_tier = len(tier_models.get(cat, ()))
        want = (levels or {}).get(cat)
        n_clear = None
        if want is not None:
            n_clear = sum(1 for m in eligible
                          if (_alias_tiers(tiers, folded, m.get('model')).get(cat)
                              if _alias_tiers(tiers, folded, m.get('model')).get(cat) is not None
                              else -1) >= want)
        out[cat] = {'models_with_tier': n_tier,
                    'asked_level': want, 'eligible_clearing_level': n_clear}
    return out


def _src(ev, name):
    return (ev or {}).get('sources', {}).get(name, 0)


def why_thin(cat, ev, n_registry, clearing, hardest) -> str:
    """Measured (never guessed) statement of WHY a category's coverage is thin.

    Every clause is a count from tables.model_perf / the coverage table —
    a NULL (measured=0) always carries its reason, per the fleet's census law.
    """
    if not ev:
        return (f'{cat}: zero perf evidence — NO model has a {cat} row in '
                f'model_perf (measured=0, bench=0, family=0, estimate=0): the '
                f'category was added but no benchmark, probe or estimate ever '
                f'touched it, so every lane defaults to tier -1')
    m_, b_, f_, e_ = (_src(ev, 'measured'), _src(ev, 'bench'),
                      _src(ev, 'family'), _src(ev, 'estimate'))
    clauses = []
    if m_ == 0 and b_ == 0:
        clauses.append(f'no model has ever been measured or benchmarked on {cat} '
                       f'tasks (measured=0, bench=0) — all evidence is inherited')
    elif m_ == 0:
        clauses.append(f'no lane has been live-measured on {cat} tasks '
                       f'(measured=0); only {b_} third-party benchmark rows exist')
    else:
        clauses.append(f'only {m_} live measurements and {b_} benchmark rows exist')
    detail = []
    if f_:
        detail.append(f'{f_} rows are family-inherited')
    if e_:
        detail.append(f'{e_} are estimates')
    if detail:
        clauses.append('; '.join(detail))
    if clearing is not None:
        clauses.append(f'level {hardest} (the hardest asked) has only {clearing} '
                       f'eligible lanes clearing it')
    return (f'{cat}: {ev["models"]} models carry evidence '
            f'(measured={m_}, bench={b_}, family={f_}, estimate={e_}) — '
            + '; '.join(clauses))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--sample', choices=('rated', 'rated-nohops'), default='rated-nohops')
    ap.add_argument('--ledger', default=LEDGER)
    ap.add_argument('--registry', default=REGISTRY)
    ap.add_argument('--ts-max', type=float, default=None,
                    help='freeze the sample: ignore ledger rows with ts > this epoch')
    ap.add_argument('--relax', action='append', default=None, metavar='CAT:LEVEL',
                    help='simulate capping CAT at LEVEL (repeatable); '
                         '"--relax off" disables; default = dominant blocker at hardest-2')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    tables, src = load_tables(args.registry)
    rows = load_sample(args.ledger, args.sample, ts_max=args.ts_max)
    res = measure(tables, rows)
    cov = coverage_table(tables, cats=res['cat_ask'], levels=res['cat_max'])

    # ---- TR-185: evidence attribution + proposals + relax simulation ---------
    proposals = []
    relax = None
    try:
        ev_by_cat, ev_totals = evidence_attribution(tables)
        n_registry_models = res['_n_registry']
        blocking = sorted(res['cat_fail'].items(), key=lambda kv: -kv[1])

        # proposals, typed: EXPANDS EVIDENCE vs RELAXES REQUIREMENT
        marginals = {}
        for cat, _n in blocking:
            ev = ev_by_cat.get(cat) or {}
            m_, b_ = _src(ev, 'measured'), _src(ev, 'bench')
            hardest = res['cat_max'].get(cat, 0)
            clearing = cov.get(cat, {}).get('eligible_clearing_level')
            if m_ == 0 or b_ < res['cat_ask'].get(cat, 0):
                proposals.append({
                    'category': cat, 'kind': 'EXPANDS EVIDENCE',
                    'text': (f'Add measured perf for {cat}: run {cat} probes/benchmarks '
                             f'on the tiered families and land them via '
                             f'data/tables/benchmarks.jsonl + router_seed.py '
                             f'(today: measured={m_}, bench={b_} rows)'
                             + ('' if m_ else ' — nothing has ever been measured'))})
            cap = max(-1, hardest - 2)
            if hardest > cap:
                sim = simulate_relax(res, {cat: cap})
                marginals[cat] = sim
                drop = res['unsat_eligible'] - sim['unsat']
                pct = (100.0 * drop / res['unsat_eligible']) if res['unsat_eligible'] else 0.0
                why_zero = ''
                if drop == 0:
                    # MEASURED diagnosis of the zero, never hand-waving: how many
                    # unsat rows ask AT or ABOVE the new cap (they move nothing),
                    # how few eligible lanes clear the CAPPED level at all, and
                    # what dropping the category outright would buy (the bound).
                    at_cap = sum(1 for r in res['rows']
                                 if not r['satisfiable_eligible']
                                 and r['reqs'].get(cat, -99) <= cap
                                 and cat in r['reqs'])
                    clear_at_cap = sum(1 for m in res['_eligible']
                                       if (res['_tiers'].get(m, {}).get(cat)
                                           if res['_tiers'].get(m, {}).get(cat) is not None
                                           else -1) >= cap)
                    drop_bound = simulate_relax(res, {cat: -1})
                    bound = res['unsat_eligible'] - drop_bound['unsat']
                    why_zero = (f' — zero because {at_cap}/{res["unsat_eligible"]} '
                                f'unsat rows already ask {cat} <= {cap} while the '
                                f'co-required bars still fail, and only '
                                f'{clear_at_cap} eligible lanes clear '
                                f'{cat} >= {cap} at all; dropping {cat} outright '
                                f'(level -1) would free {bound} rows — the '
                                f'co-requirements, not the {cat} bar alone, are '
                                f'the wall')
                proposals.append({
                    'category': cat, 'kind': 'RELAXES REQUIREMENT',
                    'text': (f'Lower {cat} tier requirement from {hardest} to {cap} '
                             f'(owner decision) — alone reduces unsatisfiable by '
                             f'{drop} rows ({pct:.1f}%): '
                             f'{res["unsat_eligible"]}->{sim["unsat"]} of {res["n"]}'
                             + why_zero)})

        # headline before/after on the SAME frozen sample
        targets = None
        if args.relax and args.relax != ['off']:
            targets = {}
            for spec in args.relax:
                c, _, lvl = spec.partition(':')
                targets[c.strip()] = int(lvl)
        elif blocking and args.relax is None:
            # default simulation ONLY when --relax was not given at all;
            # an explicit "--relax off" disables it.
            dom = blocking[0][0]
            targets = {dom: max(-1, res['cat_max'].get(dom, 0) - 2)}
        if targets:
            after = simulate_relax(res, targets)
            relax = {'targets': targets, 'before': {'n': res['n'],
                                                    'unsat': res['unsat_eligible'],
                                                    'rate': res['rate_eligible']},
                     'after': after,
                     'delta_rows': res['unsat_eligible'] - after['unsat'],
                     'delta_points': ((res['rate_eligible'] - after['rate'])
                                      if res['rate_eligible'] is not None and
                                      after['rate'] is not None else None)}
    except Exception as exc:                                   # fail-open report
        ev_by_cat, ev_totals = {}, {}
        proposals = []
        relax = None
        print(f'(evidence attribution unavailable: {exc!r})')

    if args.json:
        res_public = {k: v for k, v in res.items() if not k.startswith('_')}
        print(json.dumps({'registry': src, 'sample': args.sample, 'ts_max': args.ts_max,
                          'metric': res_public, 'coverage': cov,
                          'evidence': ev_by_cat, 'evidence_totals': ev_totals,
                          'proposals': proposals, 'relax': relax},
                         indent=1, sort_keys=True))
        return 0

    print(f'registry   : {src}')
    print(f'ledger     : {args.ledger}')
    print(f'sample     : {args.sample} — {res["n"]} rated proxied rows '
          f'(required_categories non-null'
          + (', hops_attempted == 0' if args.sample == 'rated-nohops' else '')
          + (f', ts <= {args.ts_max}' if args.ts_max else '') + ')')
    if not res['n']:
        return 0
    print(f'UNSATISFIABLE (no ELIGIBLE lane clears the conjunction): '
          f'{res["unsat_eligible"]}/{res["n"]} = {res["rate_eligible"]:.1%}')
    print(f'UNSATISFIABLE (no REGISTRY lane clears it at all)      : '
          f'{res["unsat_registry"]}/{res["n"]} = {res["rate_registry"]:.1%}')
    print(f'  -> merely gated (rated, unsatisfiable=False): '
          f'{res["sat_eligible"]}/{res["n"]}')
    print('\nper-category (asked | lanes with a tier | eligible lanes clearing the asked level):')
    print(f'  {"category":<14}{"asked":>7}{"tiered":>8}{"clearing":>10}{"hardest":>9}')
    for cat in sorted(res['cat_ask'], key=lambda c: -res['cat_ask'][c]):
        c = cov[cat]
        print(f'  {cat:<14}{res["cat_ask"][cat]:>7}{c["models_with_tier"]:>8}'
              f'{(c["eligible_clearing_level"] if c["eligible_clearing_level"] is not None else -1):>10}'
              f'{res["cat_max"][cat]:>9}')
    if res['cat_fail']:
        print('\nblocking category (thinnest support in the unsatisfiable rows):')
        for cat, n in collections.Counter(res['cat_fail']).most_common(8):
            print(f'  {cat:<14}{n}')

    # ---- TR-185: evidence source attribution ---------------------------------
    if ev_by_cat or ev_totals:
        print('\nevidence sources per category (tables.model_perf — seed-deduped, '
              'max perf wins; mirrored at data/tables/model_perf.jsonl):')
        tot = ' '.join(f'{s}={n}' for s, n in sorted(ev_totals.items(), key=lambda kv: -kv[1]))
        print(f'  model_perf rows: {sum(ev_totals.values())} total — {tot}')
        print(f'  {"category":<14}{"rows":>7}{"models":>8}{"measured":>10}{"bench":>7}'
              f'{"family":>8}{"estimate":>10}')
        for cat in sorted(res['cat_ask'], key=lambda c: -res['cat_ask'][c]):
            ev = ev_by_cat.get(cat)
            if not ev:
                print(f'  {cat:<14}{0:>7}{0:>8}{0:>10}{0:>7}{0:>8}{0:>10}'
                      f'   <- no perf evidence at all')
                continue
            s = ev['sources']
            print(f'  {cat:<14}{ev["rows"]:>7}{ev["models"]:>8}'
                  f'{s.get("measured", 0):>10}{s.get("bench", 0):>7}'
                  f'{s.get("family", 0):>8}{s.get("estimate", 0):>10}')

    if res['cat_fail']:
        print('\nwhy coverage is thin (measurement, from model_perf sources — no guesses):')
        for cat, _n in sorted(res['cat_fail'].items(), key=lambda kv: -kv[1]):
            ev = ev_by_cat.get(cat)
            clr = cov.get(cat, {}).get('eligible_clearing_level')
            hardest = res['cat_max'].get(cat, 0)
            print(f'  - {why_thin(cat, ev, n_registry_models, clr, hardest)}')
            asked = res['cat_ask'].get(cat, 0)
            unsat_asking = sum(1 for r in res['rows']
                               if not r['satisfiable_eligible'] and cat in r['reqs'])
            print(f'      asked in {asked} rated rows; blocker in {unsat_asking}/'
                  f'{res["unsat_eligible"]} unsatisfiable rows')

    if proposals:
        print('\nproposals (typed — EXPANDS EVIDENCE adds measurements, '
              'RELAXES REQUIREMENT lowers the bar; owner decision):')
        for p in proposals:
            print(f'  [{p["kind"]}] {p["text"]}')

    if relax:
        b, a = relax['before'], relax['after']
        print(f'\nbefore/after unsatisfiable rate (same frozen sample, n={relax["before"]["n"]}'
              f' {args.sample} rows; simulation caps '
              + ', '.join(f'{c} ask at {l}' for c, l in sorted(relax['targets'].items()))
              + '; eligibility rules otherwise identical):')
        print(f'  before: {b["unsat"]}/{b["n"]} = {b["rate"]:.1%} unsatisfiable')
        print(f'  after : {a["unsat"]}/{a["n"]} = {a["rate"]:.1%} unsatisfiable'
              f'  (delta {relax["delta_rows"]:+d} rows, '
              f'{(relax["delta_points"] or 0) * 100:+.1f} pts)')
        # WHY the delta is what it is, as measurement: the ask-level distribution
        # of each capped category among the (still-)unsatisfiable rows, and the
        # co-required partners that keep the conjunction unserved.
        for c in sorted(relax['targets']):
            dist = collections.Counter(r['reqs'][c] for r in res['rows']
                                       if not r['satisfiable_eligible'] and c in r['reqs'])
            print(f'  {c}: ask levels among unsat rows '
                  + ' '.join(f'L{k}={v}' for k, v in sorted(dist.items()))
                  + f' (hardest asked {res["cat_max"].get(c)}; '
                  f'{(cov.get(c) or {}).get("eligible_clearing_level")} eligible '
                  f'lanes clear the hardest level)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
