#!/usr/bin/env python3
"""TR-400 — cost per (provider, model, reasoning_effort).

Thinking burns output tokens, so the cost of a task depends on the effort it
ran at. This tool computes `est_cost_per_task` for every live lane at every
effort the lane is known to support, so pairing can rank accuracy-per-dollar
AT the effort actually billed instead of at one default.

WHAT IS MEASURED, WHAT IS NULL
------------------------------
The task shape (tokens in / out / reasoning) is measured from the outcome
store, grouped by (provider, model, reasoning_effort). Nothing is invented:

  * a cell with outcome rows at that effort  -> basis 'measured', the medians
    of the rows that ran at that effort;
  * a cell with NO outcome rows at that effort -> est_cost_per_task NULL with
    a reason naming the gap. TR-399 landed the effort stamp on the ingest
    side; until stamped rows accumulate, the ranked efforts are honestly
    uncosted rather than priced off a guessed multiplier. A guessed ladder
    would make 'high' look cheap and silently re-rank the chain — the exact
    defect this row exists to prevent.

Prices are the lane's public split (public_in_per_m / public_out_per_m) from
the registry; a lane with no published split is NULL with that reason (never
0, which would read as free).

MONOTONICITY (row AC)
---------------------
The same model at a higher effort must never be priced BELOW its lower-effort
cost. `--check` enforces it over every comparable pair (both cells costed) and
exits 1 on a violation. Today only 'default' cells are costed, so the check
reports 0 comparable pairs — stated, not hidden.

Usage:
  python3 scripts/cost_effort.py            # write data/tables/cost_effort.jsonl
  python3 scripts/cost_effort.py --check    # verify monotonicity + coverage
  python3 scripts/cost_effort.py --report   # coverage of the 20 busiest models
  python3 scripts/cost_effort.py --dry-run  # compute, print, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

_SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_SCRIPTS_DIR)
sys.path.insert(0, _SCRIPTS_DIR)

from capability_vocab import (  # noqa: E402
    EFFORT_RANK, REASONING_EFFORTS, is_stored_reasoning_effort,
)

DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
OUTCOMES = os.environ.get('ROUTING_OUTCOMES_FILE',
                          os.path.join(_REPO, 'data', 'state', 'outcomes.jsonl'))
OUT = os.path.join(DATA_DIR, 'cost_effort.jsonl')

#: The effort a lane is assumed to have run at when its outcome rows carry no
#: stamp. Pre-TR-399 rows are unstamped; they are the lane's DEFAULT-effort
#: history (that is what the resolver billed), so they are counted there and
#: the basis says so. They are never spread across the ranked efforts.
UNSTAMPED_EFFORT = 'default'

COLUMNS = ('provider', 'model', 'reasoning_effort', 'est_cost_per_task',
           'tokens_in', 'tokens_out', 'tokens_reasoning', 'in_per_m', 'out_per_m',
           'n_samples', 'basis', 'source_ref', 'null_reason')


def _read_jsonl(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def load_lanes():
    """Live lanes: provider+model, not disabled, not archived."""
    lanes = {}
    for r in _read_jsonl(os.path.join(DATA_DIR, 'models.jsonl')):
        p, m = r.get('provider'), r.get('model')
        if not p or not m or r.get('disabled') or r.get('archive'):
            continue
        lanes[(str(p), str(m))] = r
    return lanes


def supported_efforts():
    """{model: set(efforts)} from model_tier — the efforts the lane has tiers at.

    TR-398 seeded the effort axis; a lane supports an effort when the tier
    table carries rows at it. Case-folded on the model id, the same join key
    the resolver uses.
    """
    out = defaultdict(set)
    for r in _read_jsonl(os.path.join(DATA_DIR, 'model_tier.jsonl')):
        m, e = r.get('model'), r.get('reasoning_effort')
        if not m:
            continue
        if not is_stored_reasoning_effort(e):
            continue
        out[str(m).lower()].add(e)
    return out


def measure_outcomes():
    """{(provider, model, effort): {n, in[], out[], reasoning[]}} from the store."""
    acc = defaultdict(lambda: {'n': 0, 'in': [], 'out': [], 'reasoning': []})
    for r in _read_jsonl(OUTCOMES):
        p, m = r.get('provider'), r.get('model')
        if not p or not m:
            continue
        eff = r.get('reasoning_effort')
        if not is_stored_reasoning_effort(eff):
            eff = UNSTAMPED_EFFORT
        tin = _num(r.get('tokens_in'))
        tout = _num(r.get('tokens_out'))
        if tin is None and tout is None:
            continue
        a = acc[(str(p), str(m), eff)]
        a['n'] += 1
        if tin is not None:
            a['in'].append(tin)
        if tout is not None:
            a['out'].append(tout)
        tr = _num(r.get('tokens_reasoning'))
        if tr is not None:
            a['reasoning'].append(tr)
    return acc


def _med(xs):
    return statistics.median(xs) if xs else None


def build_rows(lanes=None, efforts=None, measured=None):
    """The cost_effort rows — one per (provider, model, supported effort)."""
    lanes = load_lanes() if lanes is None else lanes
    efforts = supported_efforts() if efforts is None else efforts
    measured = measure_outcomes() if measured is None else measured

    rows = []
    for (p, m), lane in sorted(lanes.items()):
        in_pm = _num(lane.get('public_in_per_m'))
        out_pm = _num(lane.get('public_out_per_m'))
        effs = set(efforts.get(str(m).lower()) or ())
        # An effort with measured outcomes is supported even when the tier
        # table has no row for it yet (evidence of use beats a missing tier).
        for (mp, mm, me) in measured:
            if mp == p and mm == m:
                effs.add(me)
        if not effs:
            effs = {UNSTAMPED_EFFORT}
        for eff in sorted(effs, key=lambda e: (EFFORT_RANK.get(e, -1), e)):
            a = measured.get((p, m, eff)) or {}
            n = a.get('n') or 0
            tin, tout, trea = _med(a.get('in')), _med(a.get('out')), _med(a.get('reasoning'))
            row = {'provider': p, 'model': m, 'reasoning_effort': eff,
                   'est_cost_per_task': None, 'tokens_in': tin, 'tokens_out': tout,
                   'tokens_reasoning': trea, 'in_per_m': in_pm, 'out_per_m': out_pm,
                   'n_samples': n, 'basis': None, 'source_ref': None,
                   'null_reason': None}
            if in_pm is None or out_pm is None:
                row['null_reason'] = ('no published price split for this lane '
                                      '(public_in_per_m/public_out_per_m null)')
                row['basis'] = 'unpriced'
            elif not n:
                row['null_reason'] = (f'no outcome rows at effort={eff!r} '
                                      '(TR-399 stamping landed; per-effort '
                                      'measurement pending)')
                row['basis'] = 'unmeasured'
            elif tin is None and tout is None:
                row['null_reason'] = 'outcome rows carry no token counts'
                row['basis'] = 'unmeasured'
            else:
                tin = tin or 0.0
                tout = tout or 0.0
                trea = trea or 0.0
                # Reasoning tokens are billed as OUTPUT (row premise).
                cost = (tin * in_pm + (tout + trea) * out_pm) / 1_000_000.0
                row['est_cost_per_task'] = round(cost, 8)
                row['basis'] = 'measured'
                row['source_ref'] = (f'outcomes.jsonl n={n} median tokens '
                                     f'(in={tin:g} out={tout:g} reasoning={trea:g}) '
                                     f'x public split {in_pm:g}/{out_pm:g} per 1M')
            rows.append(row)
    return rows


def check(rows):
    """Monotonicity + coverage. -> (ok, problems, stats)."""
    problems = []
    by_lane = defaultdict(dict)
    for r in rows:
        if r['est_cost_per_task'] is not None:
            by_lane[(r['provider'], r['model'])][r['reasoning_effort']] = r['est_cost_per_task']
    pairs = 0
    for (p, m), cells in by_lane.items():
        ranked = [(EFFORT_RANK[e], e, c) for e, c in cells.items() if e in EFFORT_RANK]
        ranked.sort()
        for i in range(len(ranked)):
            for j in range(i + 1, len(ranked)):
                pairs += 1
                if ranked[j][2] < ranked[i][2]:
                    problems.append(
                        f'{p}/{m}: {ranked[j][1]} (${ranked[j][2]:.8f}) priced BELOW '
                        f'{ranked[i][1]} (${ranked[i][2]:.8f})')
    costed = sum(1 for r in rows if r['est_cost_per_task'] is not None)
    stats = {'rows': len(rows), 'costed': costed, 'null': len(rows) - costed,
             'comparable_pairs': pairs}
    return (not problems), problems, stats


def busiest(measured, n=20):
    """The n models with the most outcome rows (traffic), by model id."""
    per_model = defaultdict(int)
    for (_p, m, _e), a in measured.items():
        per_model[m] += a.get('n') or 0
    return [m for m, _c in sorted(per_model.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def report(rows, measured):
    top = busiest(measured)
    by_model = defaultdict(list)
    for r in rows:
        by_model[r['model']].append(r)
    lines = []
    for m in top:
        cells = by_model.get(m) or []
        if not cells:
            lines.append(f'{m}: NO LIVE LANE ROW (traffic but no registry lane)')
            continue
        costed = [c for c in cells if c['est_cost_per_task'] is not None]
        nulls = [c for c in cells if c['est_cost_per_task'] is None]
        lines.append(f'{m}: {len(costed)} costed / {len(nulls)} explained null '
                     f'of {len(cells)} cells')
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', help='verify monotonicity + coverage')
    ap.add_argument('--report', action='store_true', help='20-busiest-model coverage')
    ap.add_argument('--dry-run', action='store_true', help='write nothing')
    args = ap.parse_args(argv)

    measured = measure_outcomes()
    rows = build_rows(measured=measured)
    ok, problems, stats = check(rows)

    if args.report:
        for line in report(rows, measured):
            print(line)
        print(f'rows={stats["rows"]} costed={stats["costed"]} null={stats["null"]} '
              f'comparable_pairs={stats["comparable_pairs"]}')
        return 0

    if args.check:
        print(f'rows={stats["rows"]} costed={stats["costed"]} null={stats["null"]} '
              f'comparable_pairs={stats["comparable_pairs"]}')
        for p in problems:
            print('VIOLATION:', p)
        print('monotonicity:', 'OK' if ok else 'FAIL')
        return 0 if ok else 1

    if not args.dry_run:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = OUT + '.tmp'
        with open(tmp, 'w') as f:
            for r in rows:
                f.write(json.dumps({k: r[k] for k in COLUMNS}, ensure_ascii=False) + '\n')
        os.replace(tmp, OUT)
        print(f'wrote {OUT} ({len(rows)} rows, {stats["costed"]} costed)')
    else:
        print(json.dumps(rows[:3], indent=1))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
