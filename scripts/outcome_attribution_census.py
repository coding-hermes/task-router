#!/usr/bin/env python3
"""outcome_attribution_census.py — who is charged for a failed outcome row?

The TR-354 outcome-floor gate reads a lane's completion rate and excludes a
lane whose record is catastrophic (>= 8 known outcomes AND rate <= 0.35). The
rate is computed in `router_outcomes.compute_averages` as

    n_success_known = known - n_router_side
    success_rate    = n_completed / n_success_known

so the ONLY thing that keeps a failure out of the denominator is a
`failure_reason` that appears in `ROUTER_SIDE_FAILURE_REASONS`. Everything
else — including a failure with no reason at all — is charged to the model.

That makes failure attribution a routing input, and it makes a mis-read of the
ledger expensive: an audit that selects failures on the wrong field reports a
phantom defect and proposes a fix to a producer that is already correct.

THE FIELD TRAP THIS TOOL EXISTS TO PREVENT
  `success_reason` is the SUCCESS-side field. It is written on rows that
  SUCCEEDED (the scheduler's tick verdicts: `no_scheduler_tick_for_session`,
  `tick_commits_observed`) and — inconsistently — on 1,985 rows that carry
  `success: false` with `success_reason: tick_concluded_no_commits`. It is
  null on the other 61,680 failures. So `success_reason is null` is not an
  attribution signal in EITHER direction: it over-selects (every failure with
  no success-side reason) and under-selects (it drops the 1,985 tick verdicts
  that do carry one). The failure-side field is `failure_reason`. This tool
  selects on `failure_reason`, reports the two fields separately, and names
  the tick-verdict class so the difference is visible in the output, not just
  in this docstring.

WHAT IT REPORTS
  1. Failure rows bucketed four ways: router-side (excluded from the
     denominator), attributed-other (a named reason the gate still charges to
     the model), tick-verdict (a scheduler tick verdict written as a model
     failure — `success_reason` set, `failure_reason` null), and unattributed
     (no reason at all — charged, and the class a producer fix can remove).
  2. The same buckets by source_system and by month, so a legacy bulk import
     is distinguishable from live traffic.
  3. The floor impact: lanes at/below the floor at the gate's own thresholds,
     and how many would clear under each counterfactual — dropping the
     unattributed pre-model rows, and dropping the tick-verdict rows. Both are
     computed with the gate's own arithmetic rather than guessed.

A "pre-model" row is a failure with no tokens and under a second of wall time:
the request never reached a model, so the row is a statement about the
transport, not about the model's quality. It is reported as a SUBSET of the
unattributed class, never as a synonym for it.

This tool MEASURES. It does not reclassify: which reasons belong in
`ROUTER_SIDE_FAILURE_REASONS` is a doctrine decision that moves routing for
every profile, and it is not made here.

Usage:
  python3 scripts/outcome_attribution_census.py            # text report
  python3 scripts/outcome_attribution_census.py --json     # machine census
  python3 scripts/outcome_attribution_census.py --store /path/to/outcomes.jsonl

Exit code is 0 on a successful read (this is a census, not a gate), 2 when the
store cannot be read.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import sys
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

import router_outcomes as ro  # noqa: E402

#: A failure under this wall time with no token accounting never reached a
#: model. Same threshold the audit that filed the attribution card used, kept
#: as a named constant so the definition is one edit, not a scattered literal.
PREMODELL_WALL_S = 1.0

#: The gate's own defaults (TR-354). Read from the same env knobs the resolver
#: reads so a census run under a tuned host reports the gate that host runs.
DEFAULT_MIN_KNOWN = 8
DEFAULT_RATE_FLOOR = 0.35


def _floor_cfg() -> tuple:
    """(min_known, rate_floor) exactly as `router_spawn._outcome_floor_cfg`."""
    try:
        min_known = max(0, int(os.environ.get('ROUTER_OUTCOME_FLOOR_MIN_KNOWN') or DEFAULT_MIN_KNOWN))
    except ValueError:
        min_known = DEFAULT_MIN_KNOWN
    if str(os.environ.get('ROUTER_OUTCOME_FLOOR') or '') == '0':
        min_known = 0
    try:
        rate = max(0.0, float(os.environ.get('ROUTER_OUTCOME_FLOOR_RATE') or DEFAULT_RATE_FLOOR))
    except ValueError:
        rate = DEFAULT_RATE_FLOOR
    return min_known, rate


def load_rows(path: Any=None) -> list:
    """Every parseable row of the outcome store. A torn line is skipped, not
    fatal: the store is append-only and a killed writer can leave a partial
    tail, which must not blind the census to the other 489k rows."""
    path = path or ro.outcomes_path()
    rows = []
    with open(path, 'r', encoding='utf-8') as fh:
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
    return rows


def is_premodel(row: Any) -> bool:
    """A failure that never reached a model: no tokens, sub-second wall time."""
    if row.get('success') is not False:
        return False
    if row.get('tokens_in') is not None or row.get('tokens_out') is not None:
        return False
    wall = row.get('wall_time_s')
    return wall is not None and wall < PREMODELL_WALL_S


def is_tick_verdict(row: Any) -> bool:
    """A scheduler tick verdict written as a model failure.

    The hermes driver records a tick that concluded with no commits as
    `success: false` + `success_reason: tick_concluded_no_commits` and no
    `failure_reason`, so the TR-354 gate charges it to the model. A tick that
    made no commits is a statement about the tick, not about the model's
    quality — but reclassifying it is a doctrine decision, so this tool only
    counts it.
    """
    return (row.get('success') is False
            and bool(row.get('success_reason'))
            and not row.get('failure_reason'))


def classify_failure(row: Any) -> str:
    """'router-side' | 'attributed-other' | 'tick-verdict' | 'unattributed'.

    Mirrors `compute_averages`: the reason string decides, and a missing reason
    is its own class (charged to the model, and the class a producer fix can
    remove). A non-failure returns 'not-a-failure'.
    """
    if row.get('success') is not False:
        return 'not-a-failure'
    reason = str(row.get('failure_reason') or '')
    if reason in ro.ROUTER_SIDE_FAILURE_REASONS:
        return 'router-side'
    if reason:
        return 'attributed-other'
    if is_tick_verdict(row):
        return 'tick-verdict'
    return 'unattributed'


def _month(row: Any) -> str:
    ts = row.get('ts')
    if not isinstance(ts, (int, float)):
        return 'unknown'
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime('%Y-%m')
    except (OverflowError, OSError, ValueError):
        return 'unknown'


def _lane_key(row: Any) -> tuple:
    return (str(row.get('provider') or ''), str(row.get('model') or ''))


def _rate(brows: Any) -> tuple:
    """(attributable, completed, rate) for a bucket, using the gate's arithmetic:
    router-side failures leave the denominator, everything else stays in it."""
    known = len(brows)
    router_side = sum(1 for r in brows if classify_failure(r) == 'router-side')
    attributable = known - router_side
    completed = sum(1 for r in brows if r.get('success') is True)
    return attributable, completed, (completed / attributable if attributable else None)


def floor_impact(rows: Any, min_known: Any=None, rate_floor: Any=None) -> dict:
    """The TR-354 gate's verdict over the store, plus both counterfactuals.

    `floored` = lanes the gate would exclude (attributable >= min_known, rate
    <= rate_floor). `would_clear_without_unattributed_premodel` and
    `would_clear_without_tick_verdicts` are the subsets that clear once each
    class is dropped from the denominator — the numbers an audit wants,
    computed with the gate's own arithmetic instead of a hand-rolled
    approximation.
    """
    if min_known is None or rate_floor is None:
        cfg_min, cfg_rate = _floor_cfg()
        min_known = cfg_min if min_known is None else min_known
        rate_floor = cfg_rate if rate_floor is None else rate_floor
    buckets = collections.defaultdict(list)
    for row in rows:
        if row.get('success') is None:
            continue
        buckets[_lane_key(row)].append(row)
    floored, clear_premodel, clear_tick = [], [], []
    for key, brows in buckets.items():
        if min_known <= 0:
            break
        attributable, completed, rate = _rate(brows)
        if rate is None or attributable < min_known or rate > rate_floor:
            continue
        entry = {'provider': key[0], 'model': key[1], 'n_known': len(brows),
                 'n_attributable': attributable, 'n_completed': completed,
                 'success_rate': round(rate, 4)}
        floored.append(entry)
        for label, drop, sink in (
                ('premodel', lambda r: classify_failure(r) == 'unattributed' and is_premodel(r),
                 clear_premodel),
                ('tick', is_tick_verdict, clear_tick)):
            keep = [r for r in brows if not drop(r)]
            attributable2, completed2, rate2 = _rate(keep)
            if rate2 is None or attributable2 < min_known or rate2 <= rate_floor:
                continue
            sink.append({**entry, 'dropped': label,
                         'n_attributable_after': attributable2,
                         'n_completed_after': completed2,
                         'success_rate_after': round(rate2, 4)})
    floored.sort(key=lambda e: (e['success_rate'], -e['n_known']))
    clear_premodel.sort(key=lambda e: (e['success_rate'], -e['n_known']))
    clear_tick.sort(key=lambda e: (e['success_rate'], -e['n_known']))
    return {'min_known': min_known, 'rate_floor': rate_floor,
            'n_floored': len(floored), 'floored': floored,
            'n_would_clear_without_unattributed_premodel': len(clear_premodel),
            'would_clear_without_unattributed_premodel': clear_premodel,
            'n_would_clear_without_tick_verdicts': len(clear_tick),
            'would_clear_without_tick_verdicts': clear_tick}


def census(rows: Any, min_known: Any=None, rate_floor: Any=None) -> dict:
    """The whole census over `rows` — the shape `--json` emits."""
    failures = [r for r in rows if r.get('success') is False]
    classes = collections.Counter(classify_failure(r) for r in failures)
    by_source = collections.defaultdict(collections.Counter)
    by_month = collections.defaultdict(collections.Counter)
    for row in failures:
        cls = classify_failure(row)
        by_source[str(row.get('source_system') or 'unknown')][cls] += 1
        by_month[_month(row)][cls] += 1
    unattributed = [r for r in failures if classify_failure(r) == 'unattributed']
    premodel = [r for r in unattributed if is_premodel(r)]
    tick = [r for r in failures if classify_failure(r) == 'tick-verdict']
    return {
        'store': ro.outcomes_path(),
        'n_rows': len(rows),
        'n_failures': len(failures),
        'n_success_reason_on_failures': sum(1 for r in failures if r.get('success_reason')),
        'classes': dict(classes),
        'n_unattributed': len(unattributed),
        'n_unattributed_premodel': len(premodel),
        'n_tick_verdict': len(tick),
        'unattributed_by_source': {k: v['unattributed'] for k, v in by_source.items()
                                   if v['unattributed']},
        'unattributed_premodel_by_source': dict(collections.Counter(
            str(r.get('source_system') or 'unknown') for r in premodel)),
        'tick_verdict_by_source': dict(collections.Counter(
            str(r.get('source_system') or 'unknown') for r in tick)),
        'by_source': {k: dict(v) for k, v in sorted(by_source.items())},
        'by_month': {k: dict(v) for k, v in sorted(by_month.items())},
        'floor': floor_impact(rows, min_known, rate_floor),
    }


def _render(result: Any) -> str:
    out = []
    out.append(f"store: {result['store']}")
    out.append(f"rows: {result['n_rows']:,}   failures: {result['n_failures']:,}")
    out.append("")
    out.append("failure attribution (the denominator the TR-354 floor reads):")
    for cls in ('router-side', 'attributed-other', 'tick-verdict', 'unattributed'):
        n = result['classes'].get(cls, 0)
        pct = 100.0 * n / max(result['n_failures'], 1)
        out.append(f"  {cls:<18} {n:>8,}  {pct:5.1f}%")
    out.append(f"  (success_reason present on failures: {result['n_success_reason_on_failures']:,}"
               " — that field is the SUCCESS-side reason; it is not an attribution signal)")
    out.append("")
    out.append(f"unattributed: {result['n_unattributed']:,} "
               f"(pre-model subset: {result['n_unattributed_premodel']:,})")
    if result['unattributed_premodel_by_source']:
        out.append("  pre-model by source: " + ", ".join(
            f"{k}={v:,}" for k, v in sorted(result['unattributed_premodel_by_source'].items(),
                                            key=lambda kv: -kv[1])))
    out.append(f"tick-verdict: {result['n_tick_verdict']:,} "
               "(success_reason set, failure_reason null — charged to the model)")
    if result['tick_verdict_by_source']:
        out.append("  by source: " + ", ".join(
            f"{k}={v:,}" for k, v in sorted(result['tick_verdict_by_source'].items(),
                                            key=lambda kv: -kv[1])))
    out.append("")
    out.append("by month (router-side / attributed-other / tick-verdict / unattributed):")
    for month, counts in result['by_month'].items():
        out.append(f"  {month}  {counts.get('router-side', 0):>7,} "
                   f"{counts.get('attributed-other', 0):>7,} "
                   f"{counts.get('tick-verdict', 0):>7,} "
                   f"{counts.get('unattributed', 0):>7,}")
    out.append("")
    floor = result['floor']
    out.append(f"outcome floor (min_known={floor['min_known']}, rate<={floor['rate_floor']}):")
    out.append(f"  lanes at/below the floor: {floor['n_floored']}")
    for label, key in (
            ('unattributed pre-model rows',
             'would_clear_without_unattributed_premodel'),
            ('tick-verdict rows', 'would_clear_without_tick_verdicts')):
        n = floor['n_' + key]
        out.append(f"  lanes that would clear if {label} were dropped: {n}")
        for entry in floor[key][:10]:
            out.append("    %-42s n=%d ok=%d %.3f -> n=%d ok=%d %.3f" % (
                f"{entry['provider']}/{entry['model']}", entry['n_attributable'],
                entry['n_completed'], entry['success_rate'],
                entry['n_attributable_after'], entry['n_completed_after'],
                entry['success_rate_after']))
    return "\n".join(out)


def main(argv: Any=None) -> int:
    ap = argparse.ArgumentParser(
        description='outcome attribution census — who is charged for a failed row')
    ap.add_argument('--store', default=None,
                    help='outcome store path (default: ROUTING_OUTCOMES_FILE or data/state/outcomes.jsonl)')
    ap.add_argument('--json', action='store_true', help='machine census')
    ap.add_argument('--min-known', type=int, default=None,
                    help='override the floor sample threshold (default: gate config)')
    ap.add_argument('--rate-floor', type=float, default=None,
                    help='override the floor rate (default: gate config)')
    args = ap.parse_args(argv)
    path = args.store or ro.outcomes_path()
    try:
        rows = load_rows(path)
    except OSError as exc:
        print(f'outcome_attribution_census: cannot read {path}: {exc}', file=sys.stderr)
        return 2
    result = census(rows, args.min_known, args.rate_floor)
    if args.store:
        result['store'] = path
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_render(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
