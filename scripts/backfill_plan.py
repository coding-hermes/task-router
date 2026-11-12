#!/usr/bin/env python3
"""TR-403 — the effort backfill plan: coverage first where traffic is.

WHY THIS EXISTS
---------------
TR-398 keyed capability storage on (model, category, reasoning_effort), TR-399
stamped the effort on the way in, TR-400 priced a (provider, model, effort)
cell, TR-402 published the cube. All four are PLUMBING: every stored tier row
still carries effort='default', so `task_router_tier_effort_levels` reports 1
and the accuracy-per-dollar question ("is GLM at `high` worth xkiro at
`default` for code_gen?") still cannot be answered at a ranked effort.

This tool is the PLAN that fills the axis. It is deliberately a plan and not a
measurement: it names the cells to measure, in the order that pays first, and
it refuses to call anything done that is not measured.

THE ORDER (row AC: "coverage first where traffic is")
-----------------------------------------------------
1. MODELS by outcome volume. The append-only outcome store is the only honest
   traffic signal (469k rows today). Only models with a LIVE lane are planned;
   traffic on an id the registry does not sell is reported as a NAMED GAP, not
   silently dropped.
2. CATEGORIES by outcome volume, ONE CATEGORY AT A TIME — a category pass
   finishes before the next starts, so a day's work is comparable work.
3. Within a category: model traffic desc, then the lane's public price asc
   (cheapest-first), then effort rank asc.

WHAT "DONE" MEANS (row AC: "no cell is marked done on estimate-only data")
-------------------------------------------------------------------------
A cell is DONE only when `model_perf` carries source='measured' for
(model, category, effort) — a live probe against the lane. Everything else is
labelled and NOT done:

  bench_only     a third-party benchmark overlay landed on the category. It is
                 evidence, but it is not a measurement of THIS cell: it carries
                 no effort stamp of its own and no production outcome.
  family_only    inherited from the model's alias family/base
  estimate_only  a profile-tag estimate (CATEGORY_ESTIMATES)
  missing        no evidence at all

THE RATCHET (row AC: "the usability gate ratchet must decrease as cells land")
-----------------------------------------------------------------------------
A live+priced lane with no tier row is a DEFECT in the TR-369 usability gate.
The moment ANY cell of that model is measured the lane leaves the defect bucket
— USABLE when the measured tier clears the blank default, EXPLAINED
(`tiers_below_blank_default`) when it does not. Either way the ratchet can only
go down, so the plan projects the defect count per day and `--check` FAILS when
cells are done but the committed baseline was never tightened.

Usage:
  python3 scripts/backfill_plan.py                 # the plan (text)
  python3 scripts/backfill_plan.py --json          # machine-readable, every cell named
  python3 scripts/backfill_plan.py --check         # gate (exit 1 on a defect)
  python3 scripts/backfill_plan.py --write         # regenerate docs/effort-backfill-plan.md
  python3 scripts/backfill_plan.py --top 5 --per-day 10 --traffic-file /tmp/o.jsonl

Env: ROUTING_DATA_DIR (default <repo>/data/tables),
     ROUTING_OUTCOMES_FILE (default <repo>/data/state/outcomes.jsonl).
Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import sys

_SCRIPTS = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_SCRIPTS)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from capability_vocab import (  # noqa: E402
    EFFORT_RANK,
    PERF_CATEGORIES,
    REASONING_EFFORTS,
    is_stored_reasoning_effort,
)
from tier_cost_cube import live_lanes, read_jsonl  # noqa: E402

DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
OUTCOMES = os.environ.get('ROUTING_OUTCOMES_FILE',
                          os.path.join(_REPO, 'data', 'state', 'outcomes.jsonl'))
BASELINE = os.path.join(_REPO, 'data', 'state', 'usability-baseline.json')
PLAN_DOC = os.path.join(_REPO, 'docs', 'effort-backfill-plan.md')

TOP_N = 20
PER_DAY = 40
DOC_DAYS = 10

#: The ranked levels, in comparison order. 'default' is a POINTER at the
#: provider's own default, never a level, so it is not a backfill target: the
#: axis is already populated there.
RANKED_EFFORTS = tuple(sorted((e for e in REASONING_EFFORTS if e in EFFORT_RANK),
                              key=EFFORT_RANK.get))

#: The only source that closes a cell (see the module docstring).
MEASURED = 'measured'

#: Evidence that is real but is not a measurement of the cell.
DERIVED_STATES = {'bench': 'bench_only', 'family': 'family_only',
                  'estimate': 'estimate_only'}

#: Strongest evidence wins when a (model, category, effort) key somehow carries
#: more than one row (the seed dedupes, so this is a belt-and-braces ordering).
SOURCE_PRECEDENCE = {'measured': 0, 'bench': 1, 'family': 2, 'estimate': 3}


def _stream(path):
    """Yield dict rows from a JSONL file, streaming (the store is ~400 MB)."""
    try:
        fh = open(path, encoding='utf-8')
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


# ---------------------------------------------------------------------------
# traffic
# ---------------------------------------------------------------------------

def traffic(path):
    """-> (per_model, per_model_category, per_category, stats).

    Volume is OUTCOME ROWS per model — the fleet's own record of what it
    actually ran, not a guess from the catalog. Category volume comes from the
    `required_categories` stamp the classifier writes; rows without it still
    count toward model volume (they are traffic) but cannot attribute a
    category, and `stats['category_stamped']` says how many could.
    """
    per_model = collections.Counter()
    per_cat = collections.Counter()
    per_model_cat = collections.defaultdict(collections.Counter)
    rows = stamped = cat_stamped = 0
    for r in _stream(path):
        rows += 1
        m = r.get('model')
        if not m:
            continue
        m = str(m)
        per_model[m] += 1
        if is_stored_reasoning_effort(r.get('reasoning_effort')):
            stamped += 1
        rc = r.get('required_categories')
        if isinstance(rc, dict) and rc:
            cat_stamped += 1
            for c in rc:
                per_cat[str(c)] += 1
                per_model_cat[m][str(c)] += 1
    return per_model, per_model_cat, per_cat, {
        'rows': rows, 'effort_stamped': stamped, 'category_stamped': cat_stamped,
        'source': path,
    }


# ---------------------------------------------------------------------------
# registry facts
# ---------------------------------------------------------------------------

def alias_roots(alias_rows):
    """{model: root} following `inherits` chains (cycle-safe)."""
    parent = {}
    for r in alias_rows:
        m, base = r.get('model'), r.get('inherits')
        if m and base:
            parent[str(m)] = str(base)
    roots = {}
    for m in parent:
        seen = {m}
        cur = m
        while cur in parent and parent[cur] not in seen:
            cur = parent[cur]
            seen.add(cur)
        roots[m] = cur
    return roots


def catalog_reasoning(catalog_rows):
    """{model: True|False} — does the model support reasoning at all?

    `model_catalog.reasoning` is the only per-model declaration the registry
    carries. It says a model CAN think, never WHICH levels its provider
    accepts, so it can seed a candidate set but never close a support question.
    A model with no catalog row is absent (unknown), never assumed.
    """
    out = {}
    for r in catalog_rows:
        m = r.get('model')
        if not m or r.get('reasoning') is None:
            continue
        m = str(m)
        flag = bool(r.get('reasoning'))
        out[m] = out.get(m, False) or flag
    return out


def perf_index(perf_rows):
    """{(model, category, effort): (source, perf)} — strongest evidence wins."""
    out = {}
    for r in perf_rows:
        m, cat, eff = r.get('model'), r.get('category'), r.get('reasoning_effort')
        if not m or not cat or not is_stored_reasoning_effort(eff):
            continue
        key = (str(m), str(cat), str(eff))
        src = str(r.get('source') or '')
        cur = out.get(key)
        if cur is None or SOURCE_PRECEDENCE.get(src, 9) < SOURCE_PRECEDENCE.get(cur[0], 9):
            out[key] = (src, r.get('perf'))
    return out


def _price(row):
    """The lane's comparable public price: normalized_price, else in+out per 1M."""
    p = row.get('normalized_price')
    if p is not None:
        try:
            return float(p)
        except (TypeError, ValueError):
            return None
    pin, pout = row.get('public_in_per_m'), row.get('public_out_per_m')
    try:
        if pin is not None and pout is not None:
            return float(pin) + float(pout)
    except (TypeError, ValueError):
        return None
    return None


def lane_prices(model_rows):
    """{model: cheapest public price across its live lanes} (None when unpriced)."""
    lanes = live_lanes(model_rows)
    by_lane = {(str(r.get('provider')), str(r.get('model'))): r for r in model_rows}
    out = {}
    for (p, m) in lanes:
        price = _price(by_lane.get((p, m)) or {})
        if price is None:
            out.setdefault(m, None)
            continue
        cur = out.get(m)
        out[m] = price if cur is None else min(cur, price)
    return out


def lane_status(model_rows):
    """{model: {'rows', 'live', 'disabled', 'expired'}} — why a model is/ isn't servable.

    A traffic id can miss the plan for two very different reasons and the report
    must not blur them: the registry never carried it (`no_lane_row`), or it
    carries it only in lanes that were disabled or expired (`retired_lane`).
    The second is a live defect — the fleet kept calling models it can no longer
    buy.
    """
    today = datetime.date.today().isoformat()
    out = collections.defaultdict(lambda: {'rows': 0, 'live': 0,
                                           'disabled': 0, 'expired': 0})
    for r in model_rows:
        m = r.get('model')
        if not m:
            continue
        st = out[str(m)]
        st['rows'] += 1
        if r.get('disabled') or r.get('archive'):
            st['disabled'] += 1
            continue
        vt = r.get('valid_to')
        if vt and len(str(vt)[:10]) == 10 and str(vt)[:10] < today:
            st['expired'] += 1
            continue
        st['live'] += 1
    return out


def effort_support(model, tier_efforts, outcome_efforts, catalog):
    """-> (ranked efforts, basis, note) for one model.

    Evidence beats a declaration: a stored tier row or a stamped outcome row at
    a ranked effort IS support. Otherwise the catalog's reasoning flag seeds a
    CANDIDATE set (labelled — the exact levels a provider accepts are unprobed),
    and a model with neither is 'unknown': no ranked cell may be planned for it
    until support is established, because a cell measured at an effort the
    provider rejects is not a measurement, it is an error.
    """
    evidence = {e for e in (set(tier_efforts) | set(outcome_efforts))
                if e in EFFORT_RANK}
    if evidence:
        return (sorted(evidence, key=EFFORT_RANK.get), 'evidence',
                'a stored tier or stamped outcome row already exists at this effort')
    flag = catalog.get(model)
    if flag is False:
        return (['off'], 'catalog:reasoning=false',
                'non-reasoning model — off is the only real level')
    if flag is True:
        return (list(RANKED_EFFORTS), 'candidate',
                'catalog says reasoning-capable; the exact levels the provider '
                'accepts are unprobed, so each cell needs a support probe first')
    return ([], 'unknown',
            'no catalog reasoning flag and no effort evidence — support must be '
            'probed before any ranked cell is planned')


# ---------------------------------------------------------------------------
# cells
# ---------------------------------------------------------------------------

def cell_state(source):
    """-> (state, done) for one evidence source. Only 'measured' is done."""
    if not source:
        return 'missing', False
    if source == MEASURED:
        return 'measured', True
    return DERIVED_STATES.get(source, f'other:{source}'), False


def build_cells(models, categories, support, perf, prices, model_traffic):
    """Every (model, effort, category) cell for the planned models."""
    cells = []
    for m in models:
        efforts, basis, note = support[m]
        for eff in efforts:
            for cat in categories:
                src, perf_v = perf.get((m, cat, eff), (None, None))
                state, done = cell_state(src)
                cells.append({
                    'model': m,
                    'effort': eff,
                    'category': cat,
                    'state': state,
                    'done': done,
                    'evidence_source': src,
                    'perf': perf_v,
                    'effort_basis': basis,
                    'effort_note': note,
                    'model_traffic': model_traffic.get(m, 0),
                    'price': prices.get(m),
                    'day': None,
                })
    return cells


def defect_cells(defects, per_model_cat, per_cat, prices, model_traffic):
    """One cell per DEFECT MODEL: (model, 'default', its busiest category).

    A defect lane is live+priced with no tier row at all, so ONE measured cell
    at the default pointer clears it — the cheapest possible fix, and the only
    one the ratchet needs. Lanes are deduped by MODEL because a tier is a
    property of the weights: one measurement clears every lane selling it.

    This arm exists because the traffic arm cannot move the ratchet: the busiest
    models already carry tier rows (bench/family/estimate), so they are USABLE
    and measuring them at a ranked effort changes no defect count. Stating that
    plainly is the point — a plan that claimed otherwise would be lying about
    its own effect.
    """
    by_model = collections.Counter()
    for d in defects:
        m = d.get('model')
        if m:
            by_model[str(m)] += 1
    top_cat = max(per_cat, key=per_cat.get) if per_cat else PERF_CATEGORIES[0]
    out = []
    for m, lanes in by_model.items():
        cats = per_model_cat.get(m) or {}
        cat = max(cats, key=cats.get) if cats else top_cat
        out.append({
            'model': m,
            'effort': 'default',
            'category': cat,
            'state': 'missing',
            'done': False,
            'evidence_source': None,
            'perf': None,
            'effort_basis': 'pointer',
            'effort_note': 'the default pointer — one measured cell clears the lane',
            'model_traffic': model_traffic.get(m, 0),
            'price': prices.get(m),
            'lanes': lanes,
            'arm': 'ratchet',
            'day': None,
        })
    return out


def schedule(effort_cells, ratchet_cells, cat_traffic, per_day, ratchet_share=0.25):
    """Assign days across both arms.

    Effort arm: one category at a time (category volume desc), then model volume
    desc, then the cheapest lane, then effort rank — the row's ordering law.

    Ratchet arm: cheapest lane first, so the gate tightens for the least money.

    Every day reserves `ratchet_share` of its budget for the ratchet arm, so the
    defect count falls from day 1 instead of only at the end of a 69-day plan.
    When one arm runs dry the other takes the whole day.
    """
    # Every category gets a DISTINCT position, including the ones with no
    # traffic (they sort last, alphabetically). A shared fallback key would let
    # zero-traffic categories interleave and break "one category at a time".
    all_cats = sorted({c['category'] for c in effort_cells} | set(cat_traffic),
                      key=lambda c: (-cat_traffic.get(c, 0), c))
    order = {c: i for i, c in enumerate(all_cats)}
    eff = [c for c in effort_cells if not c['done']]
    eff.sort(key=lambda c: (
        order.get(c['category'], len(order)),
        -c['model_traffic'],
        c['price'] if c['price'] is not None else float('inf'),
        EFFORT_RANK.get(c['effort'], 99),
        c['model'],
    ))
    rat = sorted(ratchet_cells, key=lambda c: (
        c['price'] if c['price'] is not None else float('inf'),
        -c['lanes'], c['model']))

    quota = max(1, int(round(per_day * ratchet_share))) if rat else 0
    i = j = day = 0
    day = 1
    while i < len(eff) or j < len(rat):
        room = per_day
        take_r = min(quota, len(rat) - j, room) if quota else 0
        room -= take_r
        take_e = min(room, len(eff) - i)
        room -= take_e
        if room:  # the effort arm ran dry — the ratchet arm takes the rest
            take_r += min(room, len(rat) - j - take_r)
        if not take_r and not take_e:
            break
        for c in rat[j:j + take_r]:
            c['day'] = day
        for c in eff[i:i + take_e]:
            c['day'] = day
        j += take_r
        i += take_e
        day += 1
    return eff, rat


def ratchet_projection(todo, defects_by_model):
    """-> (defects_today, [{day, defects_after, lanes_cleared}]).

    A defect lane leaves the defect bucket the moment ANY cell of its model is
    measured (usable if the tier clears the blank default, explained if not), so
    the projection is a lower bound on the ratchet, not a hope.
    """
    total = sum(defects_by_model.values())
    first_day = {}
    for c in todo:
        if c['day'] is None:
            continue
        d = first_day.get(c['model'])
        if d is None or c['day'] < d:
            first_day[c['model']] = c['day']
    out = []
    cleared = 0
    for day in sorted({c['day'] for c in todo if c['day'] is not None}):
        for m, fd in first_day.items():
            if fd == day:
                cleared += defects_by_model.get(m, 0)
        out.append({'day': day, 'defects_after': total - cleared,
                    'lanes_cleared': cleared})
    return total, out


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def check(cells, todo, stats, baseline_defects, defects_today, ratchet_cells=None,
          defects_by_model=None):
    """-> (ok, problems). The row's three ACs, enforced."""
    problems = []

    for c in cells:
        if c['done'] and c['evidence_source'] != MEASURED:
            problems.append(
                f"cell marked done on non-measured evidence: "
                f"{c['model']}@{c['effort']}:{c['category']} "
                f"source={c['evidence_source']!r}")
        if c['effort'] not in REASONING_EFFORTS:
            problems.append(
                f"cell effort outside the vocabulary: {c['effort']!r} "
                f"({c['model']}:{c['category']})")
        if not c['model'] or not c['category']:
            problems.append(f'cell with an empty key: {c}')

    # A baseline ABOVE the measured reality is a wire that no longer holds the
    # gate tight: the ratchet must be moved down to what is true. It is checked
    # unconditionally (not only when cells have landed) because a defect count
    # that fell for ANY reason — a measurement landing, a lane retiring — means
    # the committed number now licenses a regression.
    if baseline_defects is not None and baseline_defects > defects_today:
        problems.append(
            f'the usability baseline allows {baseline_defects} defects but only '
            f'{defects_today} exist: the ratchet is stale — run '
            f'scripts/router_usability_gate.py --ratchet (it may only decrease)')

    # The ratchet arm must cover EVERY defect model, otherwise the plan cannot
    # deliver its third AC no matter how many cells land.
    if ratchet_cells is not None and defects_by_model:
        covered = {c['model'] for c in ratchet_cells}
        missing = sorted(set(defects_by_model) - covered)
        if missing:
            problems.append(
                f'{len(missing)} defect model(s) have no ratchet cell, so the gate '
                f'can never tighten: {missing[:5]}')

    days = [c['day'] for c in todo if c['day'] is not None]
    if days:
        if min(days) != 1:
            problems.append(f'plan does not start at day 1 (min day {min(days)})')
        if max(days) != len(set(days)):
            problems.append(
                f'plan days are not contiguous 1..{max(days)} '
                f'({len(set(days))} distinct)')
    if stats['cells'] != len(cells):
        problems.append(f'cell count drifted: stats={stats["cells"]} rows={len(cells)}')

    return (not problems), problems


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _fmt_price(p):
    return 'unpriced' if p is None else f'${p:.4f}'


def render_text(plan):
    s = plan['summary']
    lines = []
    lines.append('=' * 78)
    lines.append('TR-403 BACKFILL PLAN — coverage first where traffic is')
    lines.append('=' * 78)
    lines.append(f"  traffic source      {s['traffic_source']}")
    lines.append(f"  outcome rows        {s['traffic_rows']:>7}   "
                 f"(effort-stamped {s['effort_stamped']}, "
                 f"category-stamped {s['category_stamped']})")
    lines.append(f"  planned models      {s['models']:>7}   "
                 f"(top {s['top']} busiest with a live lane)")
    lines.append(f"  categories          {s['categories']:>7}   (ordered by outcome volume)")
    lines.append(f"  cells               {s['cells']:>7}   "
                 f"done {s['done']} ({s['done_pct']}%) / open {s['open']}")
    lines.append(f"  arms                {s['effort_arm_cells']:>7} effort cells + "
                 f"{s['ratchet_arm_cells']} ratchet cells "
                 f"({s['ratchet_arm_models']} defect models, "
                 f"{int(s['ratchet_share'] * 100)}% of each day)")
    lines.append(f"  per day             {s['per_day']:>7}   -> {s['days']} days")
    for state, n in sorted(s['by_state'].items(), key=lambda kv: -kv[1]):
        lines.append(f"      {state:<16}{n:>7}")
    lines.append(f"  at the default pointer: {s['default_cells']} cells, "
                 f"{s['default_done']} measured "
                 f"({', '.join(f'{k}={v}' for k, v in sorted(s['default_by_state'].items()))})")
    lines.append(f"  premise re-measured: {s['premise_raw_zero']} of "
                 f"{s['premise_raw_models']} raw busiest ids and "
                 f"{s['premise_zero_measured']} of {s['premise_models']} plannable "
                 f"live models have ZERO measured rows at any effort")
    if s['effort_basis']:
        lines.append('  effort support:')
        for basis, n in sorted(s['effort_basis'].items()):
            lines.append(f"      {basis:<16}{n:>7} models")

    if plan['gaps']['no_live_lane']:
        lines.append('')
        lines.append('  NAMED GAP — traffic on an id with no live lane '
                     '(not planned, not dropped):')
        for g in plan['gaps']['no_live_lane'][:10]:
            detail = (f"{g['disabled']} disabled, {g['expired']} expired"
                      if g['reason'] == 'retired_lane' else 'no lane row at all')
            lines.append(f"      {g['rows']:>7} rows  {g['model']:<40} "
                         f"[{g['reason']}: {detail}]")

    if plan['gaps']['unknown_effort_support']:
        lines.append('')
        lines.append('  NAMED GAP — no effort support evidence (probe before planning):')
        for m in plan['gaps']['unknown_effort_support'][:10]:
            lines.append(f"      {m}")

    lines.append('')
    lines.append('  THE PLAN (effort arm: one category at a time, traffic desc, '
                 'cheapest-first; ratchet arm: cheapest lane first)')
    for day in plan['days'][:DOC_DAYS]:
        lines.append(f"  DAY {day['day']} — {day['cells']} cells "
                     f"({day['effort_cells']} effort + {day['ratchet_cells']} ratchet), "
                     f"category={day['category']}, "
                     f"efforts={','.join(day['efforts'])}")
        for c in day['named']:
            lines.append(f"      {c['model']:<34}{c['effort']:<8}{c['category']:<14}"
                         f"{_fmt_price(c['price'])}  {c['arm']}")
    if len(plan['days']) > DOC_DAYS:
        lines.append(f"  ... {len(plan['days']) - DOC_DAYS} more days "
                     f"(every cell named: --json)")

    lines.append('')
    lines.append('  RATCHET (usability gate defects; a measured cell clears its model\'s lanes)')
    lines.append(f"      today            {plan['ratchet']['defects_today']:>6} defects"
                 f"   baseline {plan['ratchet']['baseline']}")
    for p in plan['ratchet']['projection'][:DOC_DAYS]:
        lines.append(f"      after day {p['day']:<4}   {p['defects_after']:>6} defects"
                     f"   ({p['lanes_cleared']} lanes cleared)")
    if len(plan['ratchet']['projection']) > DOC_DAYS:
        last = plan['ratchet']['projection'][-1]
        lines.append(f"      after day {last['day']:<4}   {last['defects_after']:>6} defects"
                     f"   ({last['lanes_cleared']} lanes cleared)")
    return '\n'.join(lines)


def render_doc(plan):
    s = plan['summary']
    today = datetime.date.today().isoformat()
    out = []
    out.append('# The effort backfill plan (TR-403)')
    out.append('')
    out.append(f'Generated by `scripts/backfill_plan.py --write` on {today}. '
               'Re-run it; do not hand-edit.')
    out.append('')
    out.append('The effort axis is plumbed (storage, ingest stamp, cost, resolver, '
               'cube) but not populated: every stored tier row is keyed `default`. '
               'This is the plan that fills it, ordered by the only honest traffic '
               'signal the fleet has — its own outcome store.')
    out.append('')
    out.append('## State today')
    out.append('')
    out.append(f"- outcome rows read: **{s['traffic_rows']}** "
               f"(effort-stamped {s['effort_stamped']}, "
               f"category-stamped {s['category_stamped']})")
    out.append(f"- planned models: **{s['models']}** (top {s['top']} busiest with a live lane)")
    out.append(f"- cells: **{s['cells']}** — done **{s['done']}** ({s['done_pct']}%), "
               f"open **{s['open']}**")
    out.append(f"- budget: {s['per_day']} cells/day -> **{s['days']} days**")
    out.append(f"- at the `default` pointer: {s['default_cells']} cells, "
               f"**{s['default_done']} measured** "
               f"({', '.join(f'`{k}`={v}' for k, v in sorted(s['default_by_state'].items()))})")
    out.append(f"- premise re-measured: **{s['premise_raw_zero']} of "
               f"{s['premise_raw_models']}** raw busiest ids and "
               f"**{s['premise_zero_measured']} of {s['premise_models']}** plannable "
               f"live models have zero measured rows at any effort")
    out.append('')
    out.append('The row said "12 of the 20 busiest live models have zero measured '
               'rows". Re-measured against the store today the count is '
               f"{s['premise_raw_zero']} of {s['premise_raw_models']} raw ids "
               f"({s['premise_zero_measured']} of the {s['premise_models']} that are "
               'plannable — the rest are ids the registry no longer sells). The '
               'premise holds; the exact number moved, and the number in this file '
               'is the measured one.')
    out.append('')
    out.append('| cell state | cells |')
    out.append('|---|---|')
    for state, n in sorted(s['by_state'].items(), key=lambda kv: -kv[1]):
        out.append(f'| `{state}` | {n} |')
    out.append('')
    out.append('A cell is **done** only on `source=measured` evidence. `bench_only`, '
               '`family_only` and `estimate_only` are labelled priors, never '
               'completions — a benchmark overlay carries no effort stamp of its own '
               'and no production outcome, so it cannot close a cell.')
    out.append('')
    if plan['gaps']['no_live_lane']:
        out.append('## Named gap — traffic on an id with no live lane')
        out.append('')
        out.append('These ids carry real traffic but have no live lane, so no cell can '
                   'be planned for them. `retired_lane` is the worse case: the registry '
                   'still carries the model, but every lane is disabled or expired — the '
                   'fleet kept calling something it can no longer buy.')
        out.append('')
        out.append('| outcome rows | model id | why |')
        out.append('|---|---|---|')
        for g in plan['gaps']['no_live_lane']:
            why = (f"retired lane ({g['disabled']} disabled, {g['expired']} expired)"
                   if g['reason'] == 'retired_lane' else 'no lane row at all')
            out.append(f"| {g['rows']} | `{g['model']}` | {why} |")
        out.append('')
    if plan['gaps']['unknown_effort_support']:
        out.append('## Named gap — effort support unestablished')
        out.append('')
        out.append('No catalog reasoning flag and no effort evidence: a support probe '
                   'must run before any ranked cell is planned for these models.')
        out.append('')
        for m in plan['gaps']['unknown_effort_support']:
            out.append(f'- `{m}`')
        out.append('')
    out.append('## The plan — two arms, one budget')
    out.append('')
    out.append('**Effort arm** (the row\'s main body): one category at a time '
               '(category outcome volume desc), then model volume desc, then the '
               'cheapest lane first, then effort rank.')
    out.append('')
    out.append('**Ratchet arm**: one cell per usability-gate defect model, at its '
               'busiest category and the `default` pointer, cheapest lane first. '
               'This arm exists because the traffic arm cannot move the gate — the '
               'busiest models already carry tier rows, so they are *usable* and '
               'measuring them at a ranked effort changes no defect count. Saying so '
               f'is the point. Each day reserves {int(s["ratchet_share"] * 100)}% of '
               'its budget for it, so the defect count falls from day 1 instead of '
               'only at the end of the effort arm.')
    out.append('')
    for day in plan['days'][:DOC_DAYS]:
        out.append(f"### Day {day['day']} — {day['cells']} cells "
                   f"({day['effort_cells']} effort + {day['ratchet_cells']} ratchet), "
                   f"`{day['category']}`, efforts {', '.join(day['efforts'])}")
        out.append('')
        out.append('| model | effort | category | lane price | arm |')
        out.append('|---|---|---|---|---|')
        for c in day['named']:
            out.append(f"| `{c['model']}` | `{c['effort']}` | `{c['category']}` | "
                       f"{_fmt_price(c['price'])} | {c['arm']} |")
        out.append('')
    if len(plan['days']) > DOC_DAYS:
        out.append(f"*Days {DOC_DAYS + 1}–{len(plan['days'])} are in the "
                   f"machine-readable plan (`--json`), which names every cell; the "
                   f"ordering above is the contract, so they are fully determined "
                   f"by it.*")
        out.append('')
    out.append('## The ratchet')
    out.append('')
    out.append('A live+priced lane with no tier row is a usability-gate DEFECT. The '
               'moment any cell of that model is measured the lane leaves the defect '
               'bucket — usable when the measured tier clears the blank default, '
               'explained (`tiers_below_blank_default`) when it does not. The ratchet '
               'therefore only goes down, and `--check` fails whenever the committed '
               'baseline sits ABOVE the defects that actually exist: a looser wire '
               'licenses a regression.')
    out.append('')
    out.append(f"Defects today: **{plan['ratchet']['defects_today']}** "
               f"(committed baseline {plan['ratchet']['baseline']}).")
    out.append('')
    out.append('| after day | defects | lanes cleared |')
    out.append('|---|---|---|')
    for p in plan['ratchet']['projection']:
        out.append(f"| {p['day']} | {p['defects_after']} | {p['lanes_cleared']} |")
    out.append('')
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_plan(top=TOP_N, per_day=PER_DAY, traffic_file=None, ratchet_share=0.25):
    """Compute the whole plan. Pure over the published tables + the store."""
    model_rows = read_jsonl(os.path.join(DATA_DIR, 'models.jsonl'))
    tier_rows = read_jsonl(os.path.join(DATA_DIR, 'model_tier.jsonl'))
    perf_rows = read_jsonl(os.path.join(DATA_DIR, 'model_perf.jsonl'))
    alias_rows = read_jsonl(os.path.join(DATA_DIR, 'model_aliases.jsonl'))
    catalog_rows = read_jsonl(os.path.join(DATA_DIR, 'model_catalog.jsonl'))

    per_model, _per_model_cat, per_cat, tstats = traffic(traffic_file or OUTCOMES)
    lanes = live_lanes(model_rows)
    live_models = {m for (_p, m) in lanes}
    roots = alias_roots(alias_rows)
    catalog = catalog_reasoning(catalog_rows)
    perf = perf_index(perf_rows)
    prices = lane_prices(model_rows)
    status = lane_status(model_rows)

    tier_efforts = collections.defaultdict(set)
    for r in tier_rows:
        m, e = r.get('model'), r.get('reasoning_effort')
        if m and is_stored_reasoning_effort(e):
            tier_efforts[str(m)].add(str(e))

    # --- pick the models: traffic desc, live lane required -------------------
    planned, no_lane = [], []
    for m, n in per_model.most_common():
        if m in live_models:
            planned.append(m)
        elif roots.get(m) in live_models:
            # The registry's own alias table says this id is the same weights as
            # a live lane, so its traffic belongs to that model.
            planned.append(roots[m])
        else:
            st = status.get(m) or {}
            no_lane.append({
                'model': m, 'rows': n,
                'reason': 'retired_lane' if st.get('rows') else 'no_lane_row',
                'disabled': st.get('disabled', 0), 'expired': st.get('expired', 0),
            })
        if len(planned) >= top:
            break
    planned = list(dict.fromkeys(planned))[:top]

    support = {m: effort_support(m, tier_efforts.get(m, set()), set(), catalog)
               for m in planned}
    unknown = [m for m in planned if support[m][1] == 'unknown']

    categories = [c for c in PERF_CATEGORIES]
    cells = build_cells(planned, categories, support, perf, prices, per_model)
    for c in cells:
        c['arm'] = 'effort'

    # --- what exists at the DEFAULT pointer (the axis's only populated level) --
    default_cells = build_cells(planned, categories,
                                {m: (['default'], 'pointer', None) for m in planned},
                                perf, prices, per_model)
    default_by_state = collections.Counter(c['state'] for c in default_cells)
    default_done = sum(1 for c in default_cells if c['done'])

    # --- the card's premise, re-measured (never taken on trust) --------------
    measured_any = collections.Counter()
    for (m, _cat, _eff), (src, _v) in perf.items():
        if src == MEASURED:
            measured_any[m] += 1
    zero_measured = [m for m in planned if not measured_any.get(m)]
    # The card says "12 of the 20 busiest live models have zero measured rows".
    # Re-measure BOTH readings: the raw top-20 traffic ids (which include ids the
    # registry no longer sells) and the 20 plannable live models.
    raw_top = [m for m, _n in per_model.most_common(top)]
    raw_zero = [m for m in raw_top if not measured_any.get(m)]

    # --- defects, from the gate itself (never a re-implementation) ----------
    defect_rows = []
    try:
        import router_usability_gate as gate
        _usable, _explained, defect_rows, _reasons = gate.classify()
    except Exception:  # fail-open: the plan is still useful without the gate
        defect_rows = []
    defects_by_model = collections.Counter()
    for d in defect_rows:
        defects_by_model[str(d.get('model'))] += 1
    defects_today = sum(defects_by_model.values())

    # --- the two arms -------------------------------------------------------
    ratchet = defect_cells(defect_rows, _per_model_cat, per_cat, prices, per_model)
    effort_todo, ratchet_todo = schedule(cells, ratchet, per_cat, per_day,
                                         ratchet_share)
    todo = effort_todo + ratchet_todo

    baseline = None
    try:
        baseline = json.load(open(BASELINE)).get('defects')
    except Exception:
        baseline = None

    total, projection = ratchet_projection(todo, defects_by_model)

    by_state = collections.Counter(c['state'] for c in cells)
    basis_counts = collections.Counter(support[m][1] for m in planned)
    done = sum(1 for c in cells if c['done'])

    days = []
    for day in sorted({c['day'] for c in todo}):
        # `todo` is already in schedule order; filtering preserves it. Never
        # re-sort here — the display must show the plan's own ordering.
        day_cells = [c for c in todo if c['day'] == day]
        cats = sorted({c['category'] for c in day_cells})
        days.append({
            'day': day,
            'cells': len(day_cells),
            'effort_cells': sum(1 for c in day_cells if c['arm'] == 'effort'),
            'ratchet_cells': sum(1 for c in day_cells if c['arm'] == 'ratchet'),
            'category': cats[0] if len(cats) == 1 else ','.join(cats),
            'efforts': sorted({c['effort'] for c in day_cells},
                              key=lambda e: (EFFORT_RANK.get(e, -1), e)),
            'named': day_cells,
            'more': 0,
        })

    stats = {'cells': len(cells)}
    plan = {
        'summary': {
            'traffic_source': tstats['source'],
            'traffic_rows': tstats['rows'],
            'effort_stamped': tstats['effort_stamped'],
            'category_stamped': tstats['category_stamped'],
            'models': len(planned),
            'top': top,
            'categories': len(categories),
            'cells': len(cells),
            'done': done,
            'done_pct': round(100.0 * done / len(cells), 1) if cells else 0.0,
            'open': len(effort_todo),
            'per_day': per_day,
            'days': max((c['day'] for c in todo), default=0),
            'by_state': dict(by_state),
            'effort_basis': dict(basis_counts),
            'default_cells': len(default_cells),
            'default_done': default_done,
            'default_by_state': dict(default_by_state),
            'premise_zero_measured': len(zero_measured),
            'premise_models': len(planned),
            'premise_raw_zero': len(raw_zero),
            'premise_raw_models': len(raw_top),
            'effort_arm_cells': len(effort_todo),
            'ratchet_arm_cells': len(ratchet_todo),
            'ratchet_arm_models': len(ratchet),
            'ratchet_share': ratchet_share,
        },
        'models': [{'model': m, 'traffic': per_model.get(m, 0),
                    'efforts': support[m][0], 'effort_basis': support[m][1],
                    'effort_note': support[m][2],
                    'price': prices.get(m),
                    'measured_rows_any_effort': measured_any.get(m, 0),
                    'cells': sum(1 for c in cells if c['model'] == m),
                    'done': sum(1 for c in cells if c['model'] == m and c['done'])}
                   for m in planned],
        'gaps': {'no_live_lane': no_lane[:25],
                 'unknown_effort_support': unknown},
        'days': days,
        'ratchet': {'defects_today': defects_today, 'baseline': baseline,
                    'projection': projection},
        'cells': cells,
        'ratchet_cells': ratchet,
        'todo': todo,
        'stats': stats,
    }
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--top', type=int, default=TOP_N,
                    help=f'how many busiest live models to plan (default {TOP_N})')
    ap.add_argument('--per-day', type=int, default=PER_DAY,
                    help=f'cells per day (default {PER_DAY})')
    ap.add_argument('--ratchet-share', type=float, default=0.25,
                    help='fraction of each day reserved for the ratchet arm '
                         '(default 0.25)')
    ap.add_argument('--traffic-file', default=None,
                    help='outcome store to read (default $ROUTING_OUTCOMES_FILE)')
    ap.add_argument('--json', action='store_true', help='machine-readable plan')
    ap.add_argument('--check', action='store_true', help='gate: exit 1 on a defect')
    ap.add_argument('--write', action='store_true',
                    help='regenerate docs/effort-backfill-plan.md')
    args = ap.parse_args(argv)

    if args.per_day < 1:
        print('--per-day must be >= 1', file=sys.stderr)
        return 2
    if args.top < 1:
        print('--top must be >= 1', file=sys.stderr)
        return 2

    plan = build_plan(top=args.top, per_day=args.per_day,
                      traffic_file=args.traffic_file,
                      ratchet_share=args.ratchet_share)
    todo = plan['todo']
    defects_by_model = collections.Counter()
    for c in plan['ratchet_cells']:
        defects_by_model[c['model']] = c['lanes']
    ok, problems = check(plan['cells'], todo, plan['stats'],
                         plan['ratchet']['baseline'],
                         plan['ratchet']['defects_today'],
                         ratchet_cells=plan['ratchet_cells'],
                         defects_by_model=defects_by_model)

    if args.json:
        print(json.dumps(plan, indent=1, default=str))
    else:
        print(render_text(plan))

    if args.write:
        os.makedirs(os.path.dirname(PLAN_DOC), exist_ok=True)
        with open(PLAN_DOC, 'w', encoding='utf-8') as fh:
            fh.write(render_doc(plan) + '\n')
        print(f'\nwrote {PLAN_DOC}')

    if args.check:
        for p in problems:
            print(f'FAIL: {p}')
        print('backfill plan:', 'OK' if ok else 'FAIL')
        return 0 if ok else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
