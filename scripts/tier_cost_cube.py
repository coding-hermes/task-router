#!/usr/bin/env python3
"""TR-402 — the published (provider, model, effort, category) cube.

The effort axis landed in pieces: TR-398 keyed capability storage on
(model, category, reasoning_effort), TR-399 stamped the effort on the way in,
TR-400 priced a (provider, model, effort) cell. This module is the JOIN those
three pieces were built for, and it exists in ONE place so the warehouse
(SQL view) and every reader (this script, the /metrics exporter, a reviewer
with a laptop) cannot drift:

  model_tier.jsonl     (model, category, effort)      -> tier  [a MODEL fact]
  cost_effort.jsonl    (provider, model, effort)      -> cost  [a LANE fact]
  models.jsonl         (provider, model, live?)       -> which lanes exist

A tier is a property of the weights, so the same model sold by six providers
gets six cube rows with the same tier and six different costs — that shape is
the point, not an accident. `CUBE_VIEW_SQL` is the same join as SQL and is
what `router_seed.py` installs as `v_tier_cost_effort`; `build_cube` is the
stdlib rendering of it over the published JSONL files.

WHY THE VIEW AND NOT JUST A QUERY IN A DASHBOARD
------------------------------------------------
The row AC is "a consumer can reconstruct the full cube from published data
alone". Published data means the committed `data/tables/*.jsonl` (also carried
verbatim inside `registry.json` and the DuckBrain ns mirror) — no live
database, no private store, no second writer. Both surfaces below are derived
from exactly those bytes.

NULLS CARRY THEIR REASON
------------------------
A cube cell with no measurement is NULL with `cost_null_reason` naming the
gap; it is never 0 (0 reads as free and silently wins a price ranking) and
never a guessed multiplier. `--check` fails a cell that lost its reason, a
duplicate cell, or a tier row carrying an effort outside the vocabulary.

Usage:
  python3 scripts/tier_cost_cube.py                 # coverage + JSON summary
  python3 scripts/tier_cost_cube.py --rows          # cube rows as JSONL
  python3 scripts/tier_cost_cube.py --check         # data-quality gate (exit 1)
  python3 scripts/tier_cost_cube.py --category code_gen --effort high
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, _SCRIPTS_DIR)

from capability_vocab import REASONING_EFFORTS  # noqa: E402

DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))

COLUMNS = ('provider', 'model', 'category', 'reasoning_effort', 'tier',
           'tier_source', 'tier_source_ref', 'est_cost_per_task',
           'in_per_m', 'out_per_m', 'cost_basis', 'n_samples',
           'cost_null_reason', 'tier_per_dollar')

#: Reason stamped on a cost cell that was NULL with no explanation of its own.
#: The cube never invents a NULL silently: an unexplained hole means the
#: upstream generator is broken, and the string says so instead of "null".
NULL_REASON_MISSING = ('cost cell is NULL and the generator supplied no '
                       'null_reason (upstream defect, not a measurement gap)')

#: Reason stamped when a model has a tier row but no cost row at that effort
#: at all (a lane the cost generator never enumerated).
NULL_REASON_NO_CELL = 'no cost_effort cell for this lane at this effort'

#: `tier_per_dollar` is the accuracy-per-dollar ratio the dashboards rank on.
#: A ratio needs a POSITIVE tier and a POSITIVE cost: a tier <= 0 is "at or
#: below this category's median", which is not an accuracy worth buying, and
#: a 0 cost is a missing measurement dressed as free. Both are NULL with a
#: reason rather than a number that would sort to the top.
RATIO_NO_TIER = 'tier <= 0: not an accuracy worth a per-dollar ratio'
RATIO_NO_COST = 'no measured cost at this effort: ratio undefined'


def read_jsonl(path):
    """Rows of a published JSONL file; missing/unreadable -> [] (fail-open)."""
    rows = []
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def live_lanes(model_rows, today=None):
    """The resolver's own live-lane rule: provider+model, not disabled.

    Mirrors the SQL view's WHERE clause exactly — `valid_to` in the past and
    `archive` both retire a lane — so the stdlib cube and `v_tier_cost_effort`
    count the same cells. A lane whose `valid_to` is unparseable is treated as
    live (the SQL comparison against a non-date string is NULL, i.e. not
    excluded); the two surfaces must not disagree on a malformed row.
    """
    today = today or datetime.date.today().isoformat()
    lanes = set()
    for r in model_rows:
        if r.get('disabled') or r.get('archive'):
            continue
        if not r.get('provider') or not r.get('model'):
            continue
        valid_to = r.get('valid_to')
        if valid_to:
            vt = str(valid_to)[:10]
            if len(vt) == 10 and vt < today:
                continue
        lanes.add((str(r['provider']), str(r['model'])))
    return lanes


def _ratio(tier, cost):
    """(value, null_reason) for tier_per_dollar — see RATIO_* above."""
    try:
        tier_f = float(tier)
    except (TypeError, ValueError):
        return None, RATIO_NO_TIER
    if tier_f <= 0:
        return None, RATIO_NO_TIER
    try:
        cost_f = float(cost)
    except (TypeError, ValueError):
        return None, RATIO_NO_COST
    if cost_f <= 0:
        return None, RATIO_NO_COST
    return round(tier_f / cost_f, 6), None


def build_cube(tier_rows, cost_rows, model_rows):
    """Join the three published tables into cube rows (one per cell).

    Every cube cell is (provider, model, category, reasoning_effort). A cell
    exists when a live lane has a tier row for (model, category, effort); its
    cost comes from the (provider, model, effort) cost cell when one exists.
    Deterministic order (sorted by all four keys) so `--rows` diffs cleanly.
    """
    lanes = live_lanes(model_rows)
    live_models = {m for (_p, m) in lanes}
    costs = {}
    for c in cost_rows:
        key = (str(c.get('provider') or ''), str(c.get('model') or ''),
               str(c.get('reasoning_effort') or ''))
        costs[key] = c

    out = []
    for t in tier_rows:
        model = str(t.get('model') or '')
        category = t.get('category')
        effort = str(t.get('reasoning_effort') or '')
        if not model or not category or model not in live_models:
            continue
        for provider in sorted({p for (p, m) in lanes if m == model}):
            cost = costs.get((provider, model, effort))
            if cost is None:
                est = None
                in_per_m = out_per_m = None
                basis = 'unmeasured'
                n_samples = 0
                null_reason = NULL_REASON_NO_CELL
            else:
                est = cost.get('est_cost_per_task')
                in_per_m = cost.get('in_per_m')
                out_per_m = cost.get('out_per_m')
                basis = cost.get('basis')
                n_samples = cost.get('n_samples')
                null_reason = cost.get('null_reason')
                if est is None and not null_reason:
                    null_reason = NULL_REASON_MISSING
                if est is not None:
                    # A measured cell must not carry a stale gap reason.
                    null_reason = None
            ratio, ratio_reason = _ratio(t.get('tier'), est)
            out.append({
                'provider': provider,
                'model': model,
                'category': str(category),
                'reasoning_effort': effort,
                'tier': t.get('tier'),
                'tier_source': t.get('tier_source'),
                'tier_source_ref': t.get('source_ref'),
                'est_cost_per_task': est,
                'in_per_m': in_per_m,
                'out_per_m': out_per_m,
                'cost_basis': basis,
                'n_samples': n_samples,
                'cost_null_reason': null_reason,
                'tier_per_dollar': ratio,
                # Not a published column — the reason the ratio is NULL, kept
                # out of COLUMNS so the row shape stays the contract.
                '_ratio_null_reason': ratio_reason,
            })
    out.sort(key=lambda r: (r['provider'], r['model'], r['category'],
                            r['reasoning_effort']))
    return out


def check(cube_rows, tier_rows):
    """Data-quality gate over the cube; returns (ok, problems, stats).

    Three defects are named, each of which would silently corrupt a
    per-effort ranking: a duplicate cell (two costs, one lane), an unexplained
    NULL (a hole pretending to be data), and a tier row whose effort is
    outside `capability_vocab.REASONING_EFFORTS` (a typo that would hide the
    row from every effort-filtered reader).
    """
    problems = []
    stats = {'cells': len(cube_rows), 'measured': 0, 'unmeasured': 0,
             'explained_null': 0, 'efforts': {}, 'categories': {}}

    seen = {}
    for r in cube_rows:
        key = (r['provider'], r['model'], r['category'], r['reasoning_effort'])
        if key in seen:
            problems.append(f'duplicate cube cell: {key}')
        seen[key] = True
        eff = r['reasoning_effort'] or '<blank>'
        stats['efforts'][eff] = stats['efforts'].get(eff, 0) + 1
        stats['categories'][r['category']] = \
            stats['categories'].get(r['category'], 0) + 1
        if r['est_cost_per_task'] is None:
            stats['unmeasured'] += 1
            if r['cost_null_reason']:
                stats['explained_null'] += 1
            else:
                problems.append(
                    f'unexplained NULL cost: {key[:4]}')
        else:
            stats['measured'] += 1

    for t in tier_rows:
        eff = t.get('reasoning_effort')
        if not eff:
            problems.append(
                f'tier row without an effort: {t.get("model")!r} / '
                f'{t.get("category")!r} — the effort axis key is empty')
        elif str(eff) not in REASONING_EFFORTS:
            problems.append(
                f'tier row with unknown effort {eff!r} (vocabulary: '
                f'{list(REASONING_EFFORTS)})')

    stats['measured_ratio'] = (round(stats['measured'] / stats['cells'], 4)
                               if stats['cells'] else 0.0)
    stats['explained_null_ratio'] = (
        round(stats['explained_null'] / stats['unmeasured'], 4)
        if stats['unmeasured'] else 1.0)
    return (not problems), problems, stats


#: The warehouse half of the cube — installed by router_seed.py as
#: `v_tier_cost_effort`. Kept as text here (not inline in the seed) so the
#: semantics the tests exercise are byte-for-byte the semantics that ship.
#:
#: The base is LIVE LANE x TIER ROW (the full cube), with the cost LEFT
#: joined: capability is a model fact, so every provider serving that model
#: inherits the tier while keeping its own cost, and a lane the cost generator
#: never enumerated is still a cell — NULL with a reason, not a missing row.
CUBE_VIEW_SQL = """
CREATE VIEW v_tier_cost_effort AS
SELECT l.provider, l.model, t.category, t.reasoning_effort,
       t.tier, t.tier_source, t.source_ref AS tier_source_ref,
       c.est_cost_per_task, c.in_per_m, c.out_per_m,
       COALESCE(c.basis, 'unmeasured') AS cost_basis,
       COALESCE(c.n_samples, 0) AS n_samples,
       CASE WHEN c.est_cost_per_task IS NULL
            THEN COALESCE(NULLIF(c.null_reason, ''),
                          CASE WHEN c.provider IS NULL THEN '%s'
                               ELSE '%s' END)
            ELSE NULL END AS cost_null_reason,
       CASE WHEN t.tier > 0 AND c.est_cost_per_task > 0
            THEN round(t.tier / c.est_cost_per_task, 6)
            ELSE NULL END AS tier_per_dollar
FROM (SELECT m.provider, m.model FROM models m
      WHERE (m.valid_to IS NULL OR m.valid_to > CURRENT_DATE) AND m.archive = false
        AND (m.disabled IS NULL OR NOT m.disabled)) l
JOIN model_tier t ON t.model = l.model
LEFT JOIN cost_effort c ON c.provider = l.provider AND c.model = l.model
                       AND c.reasoning_effort = t.reasoning_effort
""" % (NULL_REASON_NO_CELL.replace("'", "''"),
       NULL_REASON_MISSING.replace("'", "''"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--data-dir', default=DATA_DIR,
                    help='published tables directory (default %(default)s)')
    ap.add_argument('--rows', action='store_true',
                    help='emit cube rows as JSONL instead of the summary')
    ap.add_argument('--check', action='store_true',
                    help='run the data-quality gate; exit 1 on a defect')
    ap.add_argument('--category', help='filter to one task category')
    ap.add_argument('--effort', help='filter to one reasoning effort')
    args = ap.parse_args(argv)

    data_dir = args.data_dir
    tier_rows = read_jsonl(os.path.join(data_dir, 'model_tier.jsonl'))
    cost_rows = read_jsonl(os.path.join(data_dir, 'cost_effort.jsonl'))
    model_rows = read_jsonl(os.path.join(data_dir, 'models.jsonl'))
    if not tier_rows:
        print(f'no tier rows under {data_dir} — nothing to reconstruct',
              file=sys.stderr)
        return 2

    cube = build_cube(tier_rows, cost_rows, model_rows)
    if args.category:
        cube = [r for r in cube if r['category'] == args.category]
    if args.effort:
        cube = [r for r in cube if r['reasoning_effort'] == args.effort]

    if args.rows:
        for r in cube:
            print(json.dumps(r, sort_keys=True))
        return 0

    ok, problems, stats = check(cube, tier_rows)
    summary = {
        'data_dir': data_dir,
        'sources': {'model_tier': len(tier_rows),
                    'cost_effort': len(cost_rows),
                    'models': len(model_rows)},
        'cube': stats,
        'filters': {'category': args.category, 'effort': args.effort},
        'ok': ok,
        'problems': problems[:20],
        'problem_count': len(problems),
    }
    print(json.dumps(summary, indent=1, sort_keys=True))
    if args.check and not ok:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
