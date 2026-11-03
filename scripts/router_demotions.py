#!/usr/bin/env python3
"""router_demotions.py — TR-357 M3: lane demotion on consecutive observed failures.

M1 (404a378) made observed task failure RECORDED: apply_tick_verdicts stamps
success + success_reason ('tick_commits_observed' / 'tick_concluded_no_commits')
onto hermes outcome rows. This module turns those verdicts into lane DEMOTION —
recorded, never silent:

  Trigger   >= ROUTER_DEMOTE_AFTER_FAILURES (default 3) CONSECUTIVE observed
              task failures — success=false rows carrying an M1 tick verdict —
              for one lane (provider, model). Only M1's tick verdicts are
              evidence here: transport-class failures (402s, timeouts, circuit
              breaker territory — router_circuit.py) carry no tick reason and
              are never counted, so one failure is never double-counted by two
              different planes. Unobserved rows (success=null, tick not yet
              concluded) do not break a streak — the streak runs over OBSERVED
              verdicts only, and an unobserved row is no evidence either way.
  Action    the lane may not hold chain position 0 while demoted.
              router_spawn.resolve() displaces a demoted head to the first
              survivor that is neither demoted nor hop-0-ineligible (M2), one
              swap, hop numbers untouched, the lane never excluded, no price
              moves, no other position moves.
  Recovery  clear-on-first-success: the FIRST observed success (success=true
              tick verdict) clears the demotion — one 'clear' record, and the
              lane is head-eligible again on the next resolve. Deliberately
              not a decay: a single observed task completion is exactly the
              evidence the demotion was waiting for.
  Recorded  every TRANSITION appends one JSONL record to the demotions log
              (data/state/demotions.jsonl, ROUTING_DEMOTIONS_FILE override —
              the outcomes-store doctrine: per-user runtime state, gitignored,
              path resolved at call time):
                {ts, ts_iso, event: demote|clear, provider, model, lane,
                 consecutive_failure_count, threshold, verdict_reasons,
                 verdicts, reason, task}
              `verdicts` carries the triggering verdict identities (the store
              session_ids, oldest first, capped at the most recent 50 — the
              count field is the truth). TRANSITIONS ONLY: a re-evaluation
              that finds the lane already in the recorded state appends
              nothing, so a crash between the verdict stamp and this pass
              cannot fabricate duplicate demotions on re-run — the log IS the
              state, and re-running completes exactly the missing transition.
              Concurrent evaluators serialize on an flock sidecar (the
              router_circuit pattern) and re-read state under it.

Fail-open everywhere: evaluate() never raises (summary dict, 'error' key on
failure, exit 0), the reader is tolerant (missing/corrupt file = no demotions,
with the reason on the resolve payload's outcome_demotions block). Evaluation
is an EXPLICIT pass — this CLI, or whatever cadence the operator wires after
an import — resolve() never writes demotion state.

This is LANE-level, OUTCOME-EVIDENCE-driven demotion. It does not touch
router_circuit.py's provider-level transport breaker (TR-288), any price, or
any other lane's position (the board constraint: the defect was unrecorded
observed failure, not that free lanes exist).

Subcommands:
  evaluate   one demotion pass over the outcome store (default)
  show       active demotions + the most recent records (health view)
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import sys
import time
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DEMOTIONS = os.path.join(REPO, 'data', 'state', 'demotions.jsonl')

#: the knob (the brief's suggested name, same convention family as
#: ROUTER_HOP0_* / ROUTER_OUTCOME_FLOOR_*). 0 disables the evaluator
#: entirely; malformed env falls back to the default, never crashes a pass.
THRESHOLD_ENV = 'ROUTER_DEMOTE_AFTER_FAILURES'
DEFAULT_THRESHOLD = 3

#: the ONLY verdict classes that count as an observed TASK outcome — exactly
#: the reasons router_outcomes.apply_tick_verdicts stamps (TR-357 M1).
#: Anything else (transport rows, legacy rows with a bare success flag) is
#: not task evidence and never counts, in either direction.
TICK_REASONS = ('tick_commits_observed', 'tick_concluded_no_commits')

#: the verdict-identity list on a record is capped at the most recent 50;
#: `consecutive_failure_count` always carries the true streak length.
_VERDICT_CAP = 50


def demotions_path() -> str:
    """The demotions log path (ROUTING_DEMOTIONS_FILE override — the same
    call-time resolution as outcomes_path()/averages_path(), so a long-lived
    process follows an env change and tests stay hermetic)."""
    return os.environ.get('ROUTING_DEMOTIONS_FILE') or _DEFAULT_DEMOTIONS


def threshold() -> int:
    """ROUTER_DEMOTE_AFTER_FAILURES in force right now (0 disables;
    malformed -> default, never crash a pass)."""
    raw = os.environ.get(THRESHOLD_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_THRESHOLD
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return DEFAULT_THRESHOLD


def load_demotions(path: Any = None) -> list:
    """Tolerant whole-file scan of the demotions log: returns the dict rows;
    malformed lines are skipped with a warning instead of raising (the
    load_outcome_rows rule — a torn append must not stall the reader)."""
    path = path or demotions_path()
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding='utf-8', errors='replace') as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                print(f'demotions: skipped malformed line {lineno} in {path}',
                      file=sys.stderr)
                continue
            if isinstance(r, dict):
                rows.append(r)
    return rows


def active_demotions(path: Any = None) -> dict:
    """{(provider, model): record} for lanes whose LAST log event is
    'demote'. The log IS the state: last event per lane wins, so a cleared
    lane is simply absent. A malformed record never wedges the map."""
    active: dict = {}
    for r in load_demotions(path):
        p, m, ev = r.get('provider'), r.get('model'), r.get('event')
        if not isinstance(p, str) or not isinstance(m, str):
            continue
        if ev == 'demote':
            active[(p, m)] = r
        elif ev == 'clear':
            active.pop((p, m), None)
    return active


def _observed_tick_rows(rows: Any) -> dict:
    """{(provider, model): [observed tick rows in file order]} — only rows
    carrying an M1 tick verdict (a bool success AND a tick success_reason).
    File order is append order, which is chronology for an append-only store."""
    lanes: dict = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if r.get('success_reason') not in TICK_REASONS:
            continue
        if not isinstance(r.get('success'), bool):
            continue
        p, m = r.get('provider'), r.get('model')
        if not isinstance(p, str) or not isinstance(m, str):
            continue
        lanes.setdefault((p, m), []).append(r)
    return lanes


def _streak(observed: Any) -> tuple:
    """(failure_count, failing_rows, cleared_by) over the lane's observed tick
    rows, scanning NEWEST first: the streak counts consecutive observed
    failures and stops at the first observed success. cleared_by is that
    success row when the lane's LATEST observed verdict is a success."""
    fails = []
    cleared_by = None
    for r in reversed(observed or []):
        if r.get('success') is True:
            cleared_by = r
            break
        fails.append(r)
    fails.reverse()                       # back to chronological order
    return len(fails), fails, cleared_by


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc).isoformat(timespec='seconds') \
        .replace('+00:00', 'Z')


def _base_record(event: Any, provider: Any, model: Any, th: Any,
                 now_s: Any) -> dict:
    ts = float(now_s) if now_s is not None else time.time()
    return {'ts': ts, 'ts_iso': _iso(ts), 'event': event,
            'provider': provider, 'model': model,
            'lane': f'{provider}/{model}', 'threshold': th, 'task': 'TR-357'}


def _demote_record(provider: Any, model: Any, fails: Any, th: Any,
                   now_s: Any = None) -> dict:
    reasons = sorted({str(r.get('success_reason')) for r in fails})
    verdicts = [str(r.get('session_id')) for r in fails if r.get('session_id')
                is not None][-_VERDICT_CAP:]
    rec = _base_record('demote', provider, model, th, now_s)
    rec.update({
        'consecutive_failure_count': len(fails),
        'verdict_reasons': reasons,
        'verdicts': verdicts,
        'reason': (f'{len(fails)} consecutive observed task failures '
                   f'(success=false tick verdicts: {", ".join(reasons)}) '
                   f'reached the {THRESHOLD_ENV}={th} threshold — lane '
                   f'demoted (may not hold hop 0) until an observed success '
                   f'clears it (TR-357)'),
    })
    return rec


def _clear_record(provider: Any, model: Any, success_row: Any, th: Any,
                  now_s: Any = None) -> dict:
    sid = success_row.get('session_id')
    sid = str(sid) if sid is not None else 'unknown'
    sr = str(success_row.get('success_reason'))
    rec = _base_record('clear', provider, model, th, now_s)
    rec.update({
        'consecutive_failure_count': 0,
        'verdict_reasons': [sr],
        'verdicts': [sid],
        'reason': (f'observed success cleared the demotion (success=true tick '
                   f'verdict: {sr}, session {sid}) — lane head-eligible '
                   f'again (TR-357)'),
    })
    return rec


def _append_record(row: dict, path: Any) -> None:
    """Append ONE record line (caller holds the flock; a single small line
    append is the same write discipline as append_rows/record_unservable)."""
    target = path or demotions_path()
    d = os.path.dirname(target)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(target, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        fh.flush()


def evaluate(store_path: Any = None, demotions_path_override: Any = None,
             now_s: Any = None) -> dict:
    """One demotion pass over the outcome store. Returns a summary dict,
    NEVER raises (fail-open: a broken pass must never block an import).

    Reads the M1-stamped tick verdicts, computes each lane's trailing
    consecutive-failure streak, and appends EXACTLY the transitions the log
    does not yet carry: not-demoted -> streak >= threshold appends one
    'demote'; demoted -> latest observed verdict is a success appends one
    'clear'. State is read (and re-read) under the log's flock, so a
    concurrent evaluator can never double-append a transition. An already-
    demoted lane with a still-growing streak is a counted no-op ('already').
    """
    summary: dict = {'demoted': [], 'cleared': [], 'already': 0,
                     'lanes_observed': 0, 'records_appended': 0}
    try:
        th = threshold()
        summary['threshold'] = th
        if th <= 0:
            summary['reason'] = f'disabled ({THRESHOLD_ENV}=0)'
            return summary
        import router_outcomes as _ro
        store = store_path or _ro.outcomes_path()
        target = demotions_path_override or demotions_path()
        summary['store'] = store
        summary['state_path'] = target
        lanes = _observed_tick_rows(_ro.load_outcome_rows(store))
        summary['lanes_observed'] = len(lanes)
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        lock_path = target + '.lock'
        with open(lock_path, 'a+') as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                # state is read UNDER the lock: the transitions are computed
                # against what the log actually carries right now
                active = active_demotions(target)
                for pair in sorted(lanes):
                    provider, model = pair
                    count, streak_rows, cleared_by = _streak(lanes[pair])
                    if cleared_by is not None:
                        if pair not in active:
                            continue   # success, never demoted: no event
                        rec = _clear_record(provider, model, cleared_by,
                                            th, now_s)
                        _append_record(rec, target)
                        summary['records_appended'] += 1
                        summary['cleared'].append(rec['lane'])
                        continue
                    if count >= th:
                        if pair in active:
                            # transition-only: the demotion is already
                            # recorded — never append a duplicate
                            summary['already'] += 1
                            continue
                        rec = _demote_record(provider, model, streak_rows,
                                             th, now_s)
                        _append_record(rec, target)
                        summary['records_appended'] += 1
                        summary['demoted'].append(rec['lane'])
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        return summary
    except Exception as exc:  # noqa: BLE001 — fail-open: never raise
        summary['error'] = f'{type(exc).__name__}: {exc}'
        return summary


def show(limit: int = 20) -> dict:
    """The health view: active demotions + the most recent log records."""
    recs = load_demotions()
    active = active_demotions()
    return {
        'state_path': demotions_path(),
        'threshold_env': THRESHOLD_ENV,
        'active': [
            {'lane': rec.get('lane'),
             'consecutive_failure_count': rec.get('consecutive_failure_count'),
             'demoted_at': rec.get('ts_iso'),
             'verdict_reasons': rec.get('verdict_reasons')}
            for rec in active.values()],
        'recent_records': recs[-max(1, int(limit)):],
        'total_records': len(recs),
    }


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(
        description='TR-357 M3 lane demotion: consecutive observed task '
                    'failures demote a lane; an observed success clears it. '
                    'Every transition is recorded, never silent.')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('evaluate', help='one demotion pass over the outcome store')
    p_show = sub.add_parser('show', help='active demotions + recent records')
    p_show.add_argument('--limit', type=int, default=20)
    args = ap.parse_args(argv)
    cmd = args.cmd or 'evaluate'
    if cmd == 'evaluate':
        print(json.dumps(evaluate(), ensure_ascii=False))
    elif cmd == 'show':
        print(json.dumps(show(limit=getattr(args, 'limit', 20)),
                         ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
