#!/usr/bin/env python3
"""verified_outcomes.py — TR-299: verified outcome feedback leg.

Turns a completed dispatched task into an outcome row the ranking loop can
trust, under the lane that ACTUALLY served it. Three facts this module owns
that the existing Hermes importer (router_outcomes.import_hermes) deliberately
does not:

1. ACTUAL provider/model, not the billed/requested label. The gateway's
   billing_provider/model columns are re-stamped to config defaults at every
   restart; the immutable truth is `billing_base_url`. Provider identity is
   derived from that host (same table as the Hermes driver).

2. THE TASK'S OWN LANE, not the session's side-purposes. A foreman session
   makes calls the task never asked for — title generation, approval checks,
   background review — on different models. Only the usage rows whose `task`
   column is the MAIN LANE ('' — the gateway leaves it empty for the calls the
   session exists for) belong to the task outcome. Side-purpose rows are
   excluded BY NAME (the known side-purpose set below); a NON-empty task value
   outside that set is unclassified: it is billed to NEITHER the main lane nor
   a side lane, and the row says so (a NULL carries a reason, never a guess).

3. PASS/FAIL from the task's acceptance checks, not the worker's claim. The
   verdict reads the board row (tasks.jsonl) the session's identity points at:
   its status closure, worker_status, per-criterion results, or evidence. What
   it deliberately does NOT read: worker prose in the title/detail saying
   "success"/"complete"/"done" — a self-report is exactly the input TR-299
   forbids. Unverifiable rows carry success=None WITH the reason.

Cost is the session's own meter (tokens in / cache-read / out, API call count,
wall time from first_seen..last_seen, gateway estimate with the driver's
plan-effective replacement when the meter reads zero). The band comes from the
board row's `required_categories` via the SAME band_key() the resolve side
uses (docs/complexity-model.md R3.1).

Writes are an ATOMIC PROJECTION into a separate verified store (not the main
outcome store): the whole derivation is rebuilt each run and replaces the
store via tmp+rename, so re-running the same session produces the SAME row
and a byte-identical store — never a duplicate, never an additive double-count
(the accumulate path in router_outcomes would sum cost/tokens again on every
re-run, which is right for live steps landing one at a time and wrong for a
full re-derivation). Identity stays (source_system, session_id, model), the
store contract's own key. When outcomes_averages folds this store in via
--extra-input, verified rows SUPERSEDE main-store rows with the same key (the
billed-lane row for a session the verified derivation also carries), so no
session is ever counted twice.

Fail-open everywhere: an unreadable DB or board produces reasoned rows or an
empty result, never a crash — nothing here may block a resolve (the store is
consumed by outcomes_averages, which the sort path reads).
"""
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import router_outcomes as ro  # noqa: E402  (the store + banding source of truth)

DEFAULT_DB = os.environ.get('HERMES_STATE_DB', '~/.hermes/state.db')
DEFAULT_BOARD = os.path.join(REPO, '.coding-hermes', 'board', 'tasks.jsonl')


def verified_outcomes_path():
    """The verified store path (TR-299). ROUTING_OUTCOMES_VERIFIED_FILE wins;
    the repo-relative default is gitignored runtime state like the main store."""
    return (os.environ.get('ROUTING_OUTCOMES_VERIFIED_FILE')
            or os.path.join(REPO, 'data', 'state', 'outcomes-verified.jsonl'))


# ---------------------------------------------------------------------------
# The task column's vocabulary (measured on the live gateway 2026-10-06:
# 150k title_generation, 20k approval, 11k background_review, 429 compression,
# 255 vision, 2 goal_judge — every one a side-purpose; the MAIN lane always
# carries task='').
# ---------------------------------------------------------------------------
SIDE_PURPOSE_TASKS = frozenset((
    'title_generation', 'approval', 'background_review',
    'compression', 'vision', 'goal_judge',
))

#: same host->provider table as the Hermes driver (one identity per base_url
#: host; the stamped billing_provider is the FALLBACK, never the truth)
HOST2PROVIDER = {
    'api.xkiro.com': 'xkiro', 'api.cline.bot': 'clinepass',
    'openrouter.ai': 'openrouter', 'api.deepseek.com': 'deepseek',
    'api.minimax.io': 'minimax', 'api.stepfun.com': 'stepfun',
    'api.synthetic.new': 'synthetic', 'api.groq.com': 'groq',
    'api.fireworks.ai': 'fireworks-ai', 'ollama.com': 'ollama-cloud',
}

_FOREMAN_KEY_RE = re.compile(r'^task-router-foreman-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$')


def _txt(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', errors='replace')
    return v


def _provider_of(base_url, stamped):
    """Provider id from the IMMUTABLE billing_base_url host; the re-stamped
    column only names hosts the table has never seen (LAW, memory 09-16)."""
    for host, name in HOST2PROVIDER.items():
        if host in (base_url or ''):
            return name
    stamped = _txt(stamped)
    return stamped or 'unknown'


def _is_numericish(v):
    """Column-drift era rows (provider/model hold numerics/epoch floats/hex
    ints) are not lanes — same guard as the Hermes driver."""
    if v is None:
        return True
    s = str(v).strip().lower()
    try:
        float(s)
        return True
    except ValueError:
        pass
    try:
        int(s, 0)
        return True
    except ValueError:
        return False


def task_key_from_session(display_name=None, session_key=None, task=None):
    """The stable task id a session belongs to (the board-row join key).

    Precedence: the usage row's own task label (fine-grained, when the
    gateway named the call), then the session's identity with timestamped
    foreman keys (task-router-foreman-<ts>) collapsed to their stable prefix
    so every foreman run of the project joins to ONE board row, then the raw
    display_name, then the session_key. None when the session carries no task
    identity at all — never a synthesized placeholder."""
    task = _txt(task)
    if task:
        return task
    for candidate in (display_name, session_key):
        candidate = _txt(candidate)
        if not candidate:
            continue
        if _FOREMAN_KEY_RE.match(candidate):
            return candidate.rsplit('-', 6)[0]
        return candidate
    return None


# ---------------------------------------------------------------------------
# The board side: what a task row says about its own acceptance
# ---------------------------------------------------------------------------

def board_row_context(board_path=None):
    """Read-once index of the JSONL board: (by_id, by_title). Tolerant of torn
    lines (every JSONL consumer in this repo is); missing file -> empty index
    (callers reason about it, never crash)."""
    path = board_path or DEFAULT_BOARD
    by_id, by_title = {}, {}
    try:
        if not os.path.exists(path):
            return by_id, by_title
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                tid = _txt(row.get('id'))
                if tid:
                    by_id[tid] = row
                title = _txt(row.get('title'))
                if title:
                    by_title.setdefault(title, row)
    except OSError:
        pass
    return by_id, by_title


#: worker-claimed words are deliberately ABSENT from the pass signals: a
#: worker saying "success"/"complete" in prose is the self-report TR-299
#: forbids. Pass/fail comes from closure state, worker_status, per-criterion
#: results, or evidence — or it is None with a reason.
_FAILURE_SIGNALS = ('fail', 'rejected', 'reverted', 'regression', 'not done')


def _criteria_verdict(trow):
    """(verdict, source) from explicit per-criterion results, when the row
    carries them. Unmet criteria fail the task; all-met criteria pass it;
    anything unmmarked is not evidence."""
    for field in ('criteria_results', 'criteria', 'acceptance_criteria'):
        val = trow.get(field)
        if isinstance(val, list) and val and all(isinstance(c, dict) for c in val):
            marks = [c.get('met') if isinstance(c.get('met'), bool) else c.get('passed')
                     for c in val]
            marks = [m for m in marks if isinstance(m, bool)]
            if marks and len(marks) == len(val):
                return (all(marks), f'{field}:per-criterion')
    return None, None


def acceptance_verdict(trow):
    """(success: bool|None, source: str) — the INDEPENDENT pass/fail for a
    board task row. Never reads worker self-report prose. None + reason when
    the row carries no acceptance evidence."""
    if not isinstance(trow, dict):
        return None, 'no board row'
    status = (_txt(trow.get('status')) or '').strip().lower()
    worker_status = (_txt(trow.get('worker_status')) or '').strip().lower()
    # explicit failure verdicts first (a row can be closed AND failed)
    if worker_status in ('failed', 'rejected', 'reverted'):
        return False, f'worker_status={worker_status}'
    verdict, source = _criteria_verdict(trow)
    if verdict is not None:
        return verdict, source
    text = f'{_txt(trow.get("title")) or ""} {_txt(trow.get("detail")) or ""}'.lower()
    if status in ('done', 'closed', 'completed'):
        for sig in _FAILURE_SIGNALS:
            if sig in text:
                return False, f'closed with failure signal ({sig!r} in title/detail)'
        evidence = trow.get('evidence')
        if isinstance(evidence, list) and evidence:
            return True, 'status=closed + evidence'
        return True, f'status={status}'
    if worker_status in ('done', 'passed', 'verified'):
        return True, f'worker_status={worker_status}'
    if isinstance(trow.get('evidence'), list) and trow.get('evidence'):
        return True, 'evidence present'
    return None, ('no acceptance evidence on the board row '
                  '(not closed, no worker_status verdict, no per-criterion '
                  'result, no evidence)')


def normalize_levels(obj):
    """{category: level} from the shapes board rows carry ({cat: lvl} dict,
    [{'category','level'},...], ['cat=lvl',...]). None when nothing usable —
    never invented (the banding input stays honest)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None
            out[str(k)] = int(v)
        return out or None
    if isinstance(obj, (list, tuple)):
        out = {}
        for item in obj:
            if isinstance(item, dict) and 'category' in item:
                v = item.get('level')
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return None
                out[str(item['category'])] = int(v)
            elif isinstance(item, str) and '=' in item:
                c, _, lvl = item.partition('=')
                try:
                    out[c.strip()] = int(lvl)
                except ValueError:
                    return None
            else:
                return None
        return out or None
    return None


# ---------------------------------------------------------------------------
# Derivation: the gateway DB's main-lane usage -> candidate outcome rows
# ---------------------------------------------------------------------------

def derive_actual_lanes(db_path=None):
    """One candidate row per (session x main-lane usage row) under the lane
    that ACTUALLY served it, with the session's exact meter.

    Returns (rows, problems): rows carry success=None (the acceptance verdict
    is joined by verified_task_outcomes), problems carry session-level
    skip reasons. Rows whose provider/model cannot be identified or whose
    session has no task identity are still RETURNED (with the reason on the
    row) — a NULL carries a reason, and the averages bucket counts them
    honestly via their own failure to name a lane."""
    db_file = os.path.expanduser(db_path or DEFAULT_DB)
    problems = []
    if not os.path.exists(db_file):
        return [], [f'gateway DB not found: {db_file}']
    try:
        db = sqlite3.connect(f'file:{db_file}?mode=ro', uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        return [], [f'gateway DB unreadable: {exc}']
    try:
        sess = db.execute(
            'SELECT id, session_key, COALESCE(display_name, \'\') FROM sessions')
        identities = {r[0]: (r[1], r[2]) for r in sess.fetchall()}
        usage = db.execute("""
            SELECT session_id, model, billing_provider, billing_base_url, task,
                   api_call_count, input_tokens, cache_read_tokens, output_tokens,
                   reasoning_tokens, estimated_cost_usd, first_seen, last_seen
            FROM session_model_usage
        """).fetchall()
    except sqlite3.Error as exc:
        db.close()
        return [], [f'gateway DB query failed: {exc}']
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    out = []
    for (sid, model, prov, base, task, calls, tin, cread, tout, treason,
         cost, t0, t1) in usage:
        (sid, model, prov, base, task) = map(_txt, (sid, model, prov, base, task))
        if (task or '') in SIDE_PURPOSE_TASKS:
            continue                       # a side-purpose, never the task's lane
        if (task or '') != '':
            # a NAMED purpose outside the known set: unclassified — billed to
            # neither the main lane nor a side lane, and the row says so
            out.append({'session_id': sid, 'provider': None, 'model': None,
                        'task_key': None, 'unclassified_task': task,
                        'success': None,
                        'reason': f'usage row task {task!r} is not the main lane '
                                  'nor a known side purpose; not classified'})
            continue
        ident, problems = _identity_for(sid, identities, problems)
        provider = _provider_of(base, prov)
        if _is_numericish(model) or _is_numericish(provider):
            problems.append(f'session {sid}: column-drift row '
                            f'(model={model!r} provider={provider!r}) is not a lane')
            continue
        if provider in ('', 'unknown') or not model:
            out.append({'session_id': sid, 'provider': provider or None,
                        'model': model or None, 'task_key': ident,
                        'success': None,
                        'reason': 'no actual lane identified (provider/model '
                                  'unresolvable from billing_base_url)'})
            continue
        cost = float(cost) if cost is not None else None
        basis = None
        if cost is not None and cost > 1000:
            cost = None                    # corrupted estimate: NULL, never fabricate
            basis = 'rejected: estimate above the $1000 corruption bound'
        elif not cost:
            est, basis = ro.plan_effective_cost(provider, model,
                                                (tin or 0) + (cread or 0), tout or 0)
            cost, basis = est, (basis if est is not None else
                                (basis or 'no declared price; meter read 0'))
        try:
            wall = round(float(t1) - float(t0), 6) if (t0 is not None and t1 is not None) else None
        except (TypeError, ValueError):
            wall = None
        ts = float(t1) if t1 is not None else None
        out.append({'session_id': sid, 'provider': provider, 'model': model,
                    'task_key': ident, 'task_label': ident,
                    'turns': int(calls) if calls is not None else None,
                    'tokens_in': (tin or 0) + (cread or 0),
                    'cache_read_tokens': cread or 0,
                    'tokens_out': tout or 0,
                    'tokens_reasoning': treason or 0,
                    'cost_usd': cost, 'price_basis': basis,
                    'wall_time_s': wall, 'ts': ts,
                    'success': None,            # joined by the board verdict
                    'reason': ('session has no task identity '
                               '(display_name/session_key empty)')
                    if ident is None else None})
    return out, problems


def _identity_for(sid, identities, problems):
    key, display = identities.get(sid, (None, ''))
    ident = task_key_from_session(display_name=display, session_key=key)
    if ident is None:
        problems.append(f'session {sid}: no task identity (skipped from the '
                        'task join; still metered under its session id)')
    return ident, problems


# ---------------------------------------------------------------------------
# The joined outcome: actual lane + exact cost + independent verdict + band
# ---------------------------------------------------------------------------

def verified_task_outcomes(db_path=None, board_path=None):
    """The TR-299 feedback rows: one per (session x actual main-lane).

    Every row carries: the ACTUAL provider/model (derived, never the label),
    the exact token/call/wall meter, the independent acceptance verdict (or
    None with the reason), the band from the board row's required_categories
    via the resolve side's own band_key, and per-row skip reasons. Nothing
    here raises for a missing/unreadable source — fail-open by contract."""
    rows, problems = derive_actual_lanes(db_path)
    ctx = {'board': None}                  # the board is read lazily, once

    def board():
        if ctx['board'] is None:
            ctx['board'] = board_row_context(board_path)
        return ctx['board']

    out = []
    for cand in rows:
        row = {'source_system': 'hermes',
               'session_id': cand.get('session_id'),
               'provider': cand.get('provider'), 'model': cand.get('model'),
               'task_key': cand.get('task_key'),
               'task_label': cand.get('task_label'),
               'turns': cand.get('turns'),
               'tokens_in': cand.get('tokens_in'),
               'cache_read_tokens': cand.get('cache_read_tokens'),
               'tokens_out': cand.get('tokens_out'),
               'tokens_reasoning': cand.get('tokens_reasoning'),
               'cost_usd': cand.get('cost_usd'),
               'price_basis': cand.get('price_basis'),
               'wall_time_s': cand.get('wall_time_s'),
               'ts': cand.get('ts'),
               'complexity': None, 'profile_id': None,
               'required_categories': None, 'complexity_sig': None,
               'complexity_source': None,
               'success': cand.get('success'),
               'acceptance_status': None, 'acceptance_source': None,
               'unranked_reason': cand.get('reason')}
        warnings = []
        if cand.get('unclassified_task'):
            row['warnings'] = [f"unclassified usage task {cand['unclassified_task']!r}"]
        ident = cand.get('task_key')
        if ident:
            by_id, by_title = board()
            trow = by_id.get(ident) or by_title.get(ident)
            if trow is None:
                row['success'] = None
                row['acceptance_status'] = 'unverified'
                row['acceptance_source'] = None
                row['unranked_reason'] = (f'task {ident!r} not found on the '
                                          'board; pass/fail unverifiable')
                warnings.append('task-not-on-board')
            else:
                verdict, source = acceptance_verdict(trow)
                row['success'] = verdict
                row['acceptance_status'] = ('pass' if verdict is True else
                                            'fail' if verdict is False else 'unverified')
                row['acceptance_source'] = source
                if verdict is None:
                    # a NULL verdict carries its reason on the same row —
                    # never a bare unverified (Bane's null-with-reason law)
                    row['unranked_reason'] = source
                levels = normalize_levels(trow.get('required_categories'))
                if levels:
                    row['required_categories'] = levels
                    row['complexity'] = levels
                    row['complexity_sig'] = ro.band_key(levels)
                    row['complexity_source'] = 'board-row'
                    row['band'] = row['complexity_sig']
                else:
                    warnings.append('board row carries no usable '
                                    'required_categories; band stays null')
        if warnings:
            row['warnings'] = warnings
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# CLI: collect + atomic idempotent projection into the verified store
# ---------------------------------------------------------------------------

def write_store(path, rows):
    """Replace the store with the fresh projection (tmp + os.replace, the
    outcomes_averages.write_rows pattern). Idempotent BY CONSTRUCTION: the
    same (DB, board) state yields a byte-identical file, so a re-run of the
    same session adds no row and changes no byte. Returns the row count."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, path)
    return len(rows)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog='verified_outcomes.py',
        description='TR-299: verified outcome rows under the ACTUAL served '
                    'provider/model, with independent acceptance pass/fail.')
    ap.add_argument('--db', default=None, help='gateway state.db path')
    ap.add_argument('--board', default=None, help='board tasks.jsonl path')
    ap.add_argument('--out', default=None,
                    help='verified store JSONL (default $ROUTING_OUTCOMES_VERIFIED_FILE)')
    ap.add_argument('--dry-run', action='store_true',
                    help='collect and print the summary; write nothing')
    args = ap.parse_args(argv)

    rows = verified_task_outcomes(args.db, args.board)
    passed = sum(1 for r in rows if r['success'] is True)
    failed = sum(1 for r in rows if r['success'] is False)
    unverified = sum(1 for r in rows if r['success'] is None)
    summary = {'rows': len(rows), 'verified_pass': passed,
               'verified_fail': failed, 'unverified': unverified,
               'store': args.out or verified_outcomes_path(),
               'dry_run': bool(args.dry_run)}
    if not args.dry_run:
        store = args.out or verified_outcomes_path()
        try:
            n = write_store(store, rows)
            summary['written'] = n
        except OSError as exc:
            summary['error'] = f'write failed: {exc}'
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
