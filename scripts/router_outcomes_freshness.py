#!/usr/bin/env python3
"""router_outcomes_freshness.py — is the outcome store current with its SOURCES?

Called with no arguments by `~/.hermes/scripts/router-outcomes-import.sh` before and after the
driver importers. The shell caller decides its exit code by grepping the AFTER probe's output for a
line that begins `OK: every present source`, so that exact prefix is the contract.

WHAT "FRESH" MEANS HERE, and why it is not "recently modified"
The store is fed only by driver importers, so a stale store is normally a frozen import. But a
newest row that is days old is ALSO what a faithful import of an idle source looks like: measured
2026-09-26, `pi`'s newest store row was 157.07 h old while its newest session file was 157.08 h old —
lag 0.01 h, i.e. correct. A probe that compared the store against the wall clock would call that a
breach forever and the alarm would be worthless. So each source is measured against ITSELF: the
newest meter row the source can offer, versus the newest store row that source has produced. Lag
over the budget is a breach; an idle source is not.

A source that is not installed on this box (e.g. openclaw) is reported as ABSENT and never counts
as a breach — absent data is not stale data.

Output:
  OK: every present source ...            (healthy)
  BREACH: N of M present sources ...      (then one `  - <source>: ...` line each)
Exit: 0 healthy, 1 breach, 2 the store/probe itself could not be read.
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: Default lag budget in hours. Override with OUTCOMES_FRESHNESS_MAX_AGE_H.
DEFAULT_BUDGET_H = 6.0

HERMES_DB = '~/.hermes/state.db'
PI_DIR = '~/.pi/agent/sessions'
OPENCODE_DB = '~/.local/share/opencode/opencode.db'
OPENCLAW_DIR = '~/.openclaw'


def _epoch(v):
    """Normalize a source timestamp to seconds (opencode stores milliseconds)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f / 1000.0 if f > 1e12 else f


def _newest_hermes():
    path = os.path.expanduser(HERMES_DB)
    if not os.path.exists(path):
        return None, 'absent'
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        row = db.execute(
            'SELECT MAX(COALESCE(last_seen, first_seen)) FROM session_model_usage '
            'WHERE billing_base_url IS NOT NULL').fetchone()
    finally:
        db.close()
    ts = _epoch(row[0] if row else None)
    return (ts, 'ok') if ts else (None, 'no meter rows')


def _newest_pi():
    base = os.path.expanduser(PI_DIR)
    if not os.path.isdir(base):
        return None, 'absent'
    newest = None
    for root, _dirs, files in os.walk(base):
        for f in files:
            if not f.endswith('.jsonl'):
                continue
            try:
                m = os.path.getmtime(os.path.join(root, f))
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return (newest, 'ok') if newest else (None, 'no session files')


def _newest_opencode():
    path = os.path.expanduser(OPENCODE_DB)
    if not os.path.exists(path):
        return None, 'absent'
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        row = db.execute('SELECT MAX(time_updated) FROM message').fetchone()
    finally:
        db.close()
    ts = _epoch(row[0] if row else None)
    return (ts, 'ok') if ts else (None, 'no messages')


def _newest_openclaw():
    base = os.path.expanduser(OPENCLAW_DIR)
    if not os.path.isdir(base):
        return None, 'absent'
    newest = None
    for root, _dirs, files in os.walk(base):
        for f in files:
            try:
                m = os.path.getmtime(os.path.join(root, f))
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return (newest, 'ok') if newest else (None, 'no files')


SOURCES = {
    'hermes': _newest_hermes,
    'pi': _newest_pi,
    'opencode': _newest_opencode,
    'openclaw': _newest_openclaw,
}


def store_newest_by_source(store_path):
    """Newest `ts` per source_system in the store. Never crashes on a bad line."""
    newest = {}
    with open(store_path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            s = d.get('source_system')
            t = _epoch(d.get('ts'))
            if not s or t is None:
                continue
            if s not in newest or t > newest[s]:
                newest[s] = t
    return newest


def check(store_path=None, budget_h=None, now_s=None, sources=None):
    """Returns a dict: verdict, present, absent, breaches, per-source detail."""
    import router_outcomes as ro
    store_path = store_path or ro.outcomes_path()
    budget_h = DEFAULT_BUDGET_H if budget_h is None else budget_h
    now_s = now_s or time.time()
    sources = sources or SOURCES
    try:
        stored = store_newest_by_source(store_path)
    except OSError as e:
        return {'verdict': 'unreadable', 'error': f'{store_path}: {e}', 'detail': []}
    present, absent, breaches, detail = [], [], [], []
    for name, fn in sources.items():
        try:
            src_ts, why = fn()
        except Exception as e:  # noqa: BLE001 — a source probe must not kill the report
            src_ts, why = None, f'probe error: {e}'
        if src_ts is None:
            absent.append((name, why))
            continue
        present.append(name)
        got = stored.get(name)
        lag_h = None if got is None else (src_ts - got) / 3600.0
        if got is None:
            breaches.append((name, 'no rows for this source in the store'))
            detail.append(f'  - {name}: source has meter rows up to {_ago(src_ts, now_s)} '
                          f'but the store holds NO {name} row')
        elif lag_h > budget_h:
            breaches.append((name, f'{lag_h:.2f}h behind'))
            detail.append(f'  - {name}: store is {lag_h:.2f}h behind its source '
                          f'(source newest {_ago(src_ts, now_s)}, store newest {_ago(got, now_s)}, '
                          f'budget {budget_h:g}h)')
        else:
            detail.append(f'    {name}: current ({lag_h:.2f}h behind, '
                          f'store newest {_ago(got, now_s)})')
    return {'verdict': 'breach' if breaches else 'ok', 'present': present, 'absent': absent,
            'breaches': breaches, 'detail': detail, 'budget_h': budget_h,
            'store': store_path}


def _ago(ts, now_s):
    h = (now_s - ts) / 3600.0
    return f'{h:.2f}h ago'


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    budget = os.environ.get('OUTCOMES_FRESHNESS_MAX_AGE_H')
    try:
        budget_h = float(budget) if budget not in (None, '') else DEFAULT_BUDGET_H
    except ValueError:
        budget_h = DEFAULT_BUDGET_H
    r = check(budget_h=budget_h)
    if r['verdict'] == 'unreadable':
        print(f'BREACH: the outcome store could not be read — {r["error"]}')
        return 2
    absent_note = ('; absent (not a breach): ' +
                   ', '.join(f'{n} ({w})' for n, w in r['absent'])) if r['absent'] else ''
    if r['verdict'] == 'ok':
        print(f'OK: every present source is represented in the store within the {budget_h:g}h '
              f'budget ({len(r["present"])} present: {", ".join(r["present"])}){absent_note}')
    else:
        print(f'BREACH: {len(r["breaches"])} of {len(r["present"])} present source(s) behind their '
              f'own newest meter row (budget {budget_h:g}h){absent_note}')
    for line in r['detail']:
        print(line)
    return 0 if r['verdict'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
