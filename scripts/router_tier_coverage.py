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

CLI
  router_tier_coverage.py [--sample rated|rated-nohops] [--json]
                          [--ledger PATH] [--registry PATH]
Exit 0 always (reporting tool).
"""
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
PERF_TABLE = os.path.join(_REPO, 'data', 'tables', 'model_perf.jsonl')


def load_tables(path):
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
    for name in ('models', 'model_tier', 'task_profiles', 'task_profile_requirements'):
        p = os.path.join(data_dir, f'{name}.jsonl')
        rows = []
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        t[name] = rows
    return t, data_dir


def load_perf_sources(path):
    """Load model_perf.jsonl -> {category: {source: count}} and {category: total_count}."""
    cat_sources = collections.defaultdict(lambda: collections.Counter())
    cat_totals = collections.Counter()
    if not os.path.exists(path):
        return cat_sources, cat_totals
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            cat = row.get('category')
            src = row.get('source', 'unknown')
            if cat:
                cat_sources[cat][src] += 1
                cat_totals[cat] += 1
    return cat_sources, cat_totals


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


def build(tables):
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


def clears(mt, reqs):
    """True when the alias-aware tier dict clears EVERY requirement."""
    for cat, lvl in reqs.items():
        have = mt.get(cat)
        if (have if have is not None else -1) < lvl:
            return False
    return True


def measure(tables, sample_rows, req_overrides=None):
    """Measure satisfiability. req_overrides: {category: new_level} to simulate relaxation."""
    tiers, folded, eligible, registry = build(tables)
    ok_e = ok_r = 0
    cat_ask = collections.Counter()
    cat_fail = collections.Counter()
    cat_max = {}
    per_row = []
    for row, reqs in sample_rows:
        # Apply overrides if simulating relaxation
        effective_reqs = dict(reqs)
        if req_overrides:
            for cat, new_lvl in req_overrides.items():
                if cat in effective_reqs:
                    effective_reqs[cat] = new_lvl
        for c, l in effective_reqs.items():
            cat_ask[c] += 1
            cat_max[c] = max(cat_max.get(c, -99), l)
        e = any(clears(_alias_tiers(tiers, folded, m.get('model')), effective_reqs) for m in eligible)
        r = any(clears(_alias_tiers(tiers, folded, m.get('model')), effective_reqs) for m in registry)
        ok_e += bool(e)
        ok_r += bool(r)
        if not e:
            worst = None
            for cat, lvl in effective_reqs.items():
                n = sum(1 for m in eligible
                        if (_alias_tiers(tiers, folded, m.get('model')).get(cat)
                            if _alias_tiers(tiers, folded, m.get('model')).get(cat) is not None
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
    }


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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--sample', choices=('rated', 'rated-nohops'), default='rated-nohops')
    ap.add_argument('--ledger', default=LEDGER)
    ap.add_argument('--registry', default=REGISTRY)
    ap.add_argument('--ts-max', type=float, default=None,
                    help='freeze the sample: ignore ledger rows with ts > this epoch')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    tables, src = load_tables(args.registry)
    rows = load_sample(args.ledger, args.sample, ts_max=args.ts_max)
    res = measure(tables, rows)
    cov = coverage_table(tables, cats=res['cat_ask'], levels=res['cat_max'])
    perf_sources, perf_totals = load_perf_sources(PERF_TABLE)

    if args.json:
        # Build perf evidence dict for JSON output
        perf_evidence = {}
        for cat in sorted(res['cat_ask']):
            sources = dict(perf_sources.get(cat, {}))
            perf_evidence[cat] = {
                'total_models_with_perf': perf_totals.get(cat, 0),
                'by_source': sources,
            }
        print(json.dumps({'registry': src, 'sample': args.sample, 'ts_max': args.ts_max,
                          'metric': res, 'coverage': cov, 'perf_evidence': perf_evidence},
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

    # === TR-185: perf evidence source attribution ===
    if res['cat_fail']:
        print('\nperf evidence sources (from data/tables/model_perf.jsonl):')
        print(f'  {"category":<14}{"total":>7}{"measured":>9}{"bench":>7}{"family":>8}{"estimate":>9}')
        for cat, n in collections.Counter(res['cat_fail']).most_common(8):
            total = perf_totals.get(cat, 0)
            src_counts = perf_sources.get(cat, {})
            measured = src_counts.get('measured', 0)
            bench = src_counts.get('bench', 0)
            family = src_counts.get('family', 0)
            estimate = src_counts.get('estimate', 0)
            print(f'  {cat:<14}{total:>7}{measured:>9}{bench:>7}{family:>8}{estimate:>9}')

        print('\nwhy coverage is thin (measurement, not guess):')
        for cat, n in collections.Counter(res['cat_fail']).most_common(8):
            total = perf_totals.get(cat, 0)
            src_counts = perf_sources.get(cat, {})
            if total == 0:
                print(f'  {cat}: 0 perf evidence rows in model_perf.jsonl — '
                      f'no model has been measured/benchmarked on {cat} tasks')
            else:
                measured = src_counts.get('measured', 0)
                bench = src_counts.get('bench', 0)
                family = src_counts.get('family', 0)
                estimate = src_counts.get('estimate', 0)
                # Determine dominant source
                sources_present = []
                if measured: sources_present.append(f'{measured} measured')
                if bench: sources_present.append(f'{bench} bench')
                if family: sources_present.append(f'{family} family-inherited')
                if estimate: sources_present.append(f'{estimate} estimate')
                if sources_present:
                    print(f'  {cat}: {total} perf rows exist ({", ".join(sources_present)}) '
                          f'but tier coverage is thin — perf data exists but tier table lacks entries')
                else:
                    print(f'  {cat}: {total} perf rows but all sources are unknown/unclassified')

        print('\nproposed fixes:')
        for cat, n in collections.Counter(res['cat_fail']).most_common(8):
            total = perf_totals.get(cat, 0)
            asked_level = res['cat_max'].get(cat, 0)
            clearing = cov[cat]['eligible_clearing_level']
            if clearing is None:
                clearing = 0
            # EXPANDS EVIDENCE fix
            if total == 0:
                print(f'  [EXPANDS EVIDENCE] {cat}: run {cat} benchmarks on N models '
                      f'to populate model_perf.jsonl (currently 0 perf rows)')
            else:
                measured = perf_sources.get(cat, {}).get('measured', 0)
                if measured == 0:
                    print(f'  [EXPANDS EVIDENCE] {cat}: upgrade {total} family/estimate rows '
                          f'to measured perf via actual benchmarks (currently 0 measured)')
                else:
                    print(f'  [EXPANDS EVIDENCE] {cat}: add more measured benchmarks '
                          f'(currently {measured}/{total} are measured)')
            # RELAXES REQUIREMENT fix
            if asked_level > 1 and clearing < (asked_level * len(rows) * 0.1):
                relaxed = max(1, asked_level - 2)
                # Simulate relaxation
                relaxed_res = measure(tables, rows, req_overrides={cat: relaxed})
                delta = res['rate_eligible'] - relaxed_res['rate_eligible']
                print(f'  [RELAXES REQUIREMENT] {cat}: lower tier requirement from '
                      f'{asked_level} to {relaxed} (owner decision — reduces unsatisfiable by {delta:.1%})')

        # === Before/after unsatisfiable rate ===
        print(f'\nunsatisfiable rate:')
        print(f'  Before: {res["unsat_eligible"]}/{res["n"]} = {res["rate_eligible"]:.1%} '
              f'unsatisfiable (n={res["n"]} {args.sample} rows)')
        # Simulate relaxing the top blocking category
        top_cat = collections.Counter(res['cat_fail']).most_common(1)
        if top_cat:
            cat = top_cat[0][0]
            asked_level = res['cat_max'].get(cat, 0)
            if asked_level > 1:
                relaxed = max(1, asked_level - 2)
                relaxed_res = measure(tables, rows, req_overrides={cat: relaxed})
                print(f'  After relaxing {cat} {asked_level}→{relaxed}: '
                      f'{relaxed_res["unsat_eligible"]}/{relaxed_res["n"]} = '
                      f'{relaxed_res["rate_eligible"]:.1%} unsatisfiable (n={relaxed_res["n"]})')
                delta = res['rate_eligible'] - relaxed_res['rate_eligible']
                print(f'  Delta: -{delta:.1%}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
