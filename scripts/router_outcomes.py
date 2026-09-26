#!/usr/bin/env python3
"""router_outcomes.py — cost-per-task engine, phase 1 (TR-049).

Outcome store: append-only JSONL rows, UNCOMMITTED (gitignored — users rebuild
from THEIR gateway DBs; publishing ours is a separate future decision).

Row shape (roadmap 2026-09-16, Bane directives):
  {source_system, session_id, complexity, provider, model, turns,
   tokens_in, tokens_out, tokens_reasoning, cost_usd, wall_time_s, success}

Rolling averages work like Linux load averages: exponential decay with
half-life = scale (default 1d/3d/7d, user-extensible), recomputed per
(source_system, provider, model, complexity) bucket. Per-backend isolation is
the default; merge only with --merge-backends (never a global hardcode).

Subcommands:
  import-hermes   pull rows from the hermes gateway DB (~/.hermes/state.db)
  averages        recompute outcomes-averages.jsonl from the outcome store
  query           print the average for a lane (optionally merged)
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_OUTCOMES = os.path.join(REPO, 'data', 'state', 'outcomes.jsonl')
_DEFAULT_AVERAGES = os.path.join(REPO, 'data', 'state', 'outcomes-averages.jsonl')


def outcomes_path():
    """The outcome store path (TR-049).

    ROUTING_OUTCOMES_FILE wins so a reporter can point at its OWN store (the
    per-user data doctrine: every user rebuilds the averages from their data);
    the repo-relative default is gitignored runtime state. Resolved at CALL
    time so a long-lived process (the API server) follows an env change and
    tests stay hermetic.
    """
    return os.environ.get('ROUTING_OUTCOMES_FILE') or _DEFAULT_OUTCOMES


def averages_path():
    """The rolling-averages path (ROUTING_AVERAGES_FILE override, TR-049)."""
    return os.environ.get('ROUTING_AVERAGES_FILE') or _DEFAULT_AVERAGES


# Back-compat module constants (historical import-time values). New code calls
# the resolvers above; these exist so existing importers keep working.
OUTCOMES = outcomes_path()
AVERAGES = averages_path()
DEFAULT_SCALES_H = [24, 72, 168]  # 1d / 3d / 7d


# ---------- core (unit-tested) ----------

def decay_weight(age_s, scale_h):
    """Half-life decay: a sample exactly scale_h old weighs 0.5."""
    return 0.5 ** (age_s / (scale_h * 3600.0))


def bucket_weighted(rows, field, scale_h, now_s=None):
    """Decay-weighted mean of `field` over rows that carry it.

    Returns None when no row carries the field — never fabricate (a missing
    fact stays missing; the resolve-time sort treats unknown as unknown).
    """
    now_s = now_s or time.time()
    wsum = xsum = 0.0
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        ts = r.get('ts') or now_s
        w = decay_weight(max(0.0, now_s - ts), scale_h)
        wsum += w
        xsum += w * v
    return None if wsum == 0 else xsum / wsum


def bucket_avg(rows, scale_h, now_s=None):
    """Weighted mean of cost_usd over rows with a cost, by decay weight.
    Returns None when no row carries a cost (never fabricate)."""
    return bucket_weighted(rows, 'cost_usd', scale_h, now_s=now_s)


def canonical_complexity(obj):
    """Normalize a complexity declaration to a canonical {category: int} dict.

    Accepts: {cat: level} | [{"category": c, "level": l}, ...] | {"c=3","d=-2"}.
    Returns None when nothing usable is present (never invent categories).
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == 'category' and 'level' in obj and len(obj) <= 3:
                continue  # single-row {category, level} shape
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                return None
        return out or None
    if isinstance(obj, (list, tuple)):
        out = {}
        for item in obj:
            if isinstance(item, dict) and 'category' in item:
                out[str(item['category'])] = int(item.get('level', 0))
            elif isinstance(item, str) and '=' in item:
                c, _, lvl = item.partition('=')
                out[c.strip()] = int(lvl)
            else:
                return None
        return out or None
    return None


def complexity_sig(requirements):
    """The complexity REFERENCE (TR-065 R1): sha1 over the canonical JSON of
    {category: min_level}. Dict-order independent; level-sensitive — a task
    requiring code_gen>=2 is a different bucket from code_gen>=3 on the same
    model. Returns None when the declaration is unusable (never guess)."""
    canon = canonical_complexity(requirements)
    if not canon:
        return None
    payload = json.dumps({k: int(canon[k]) for k in sorted(canon)},
                         separators=(',', ':'), sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()


def row_complexity_sig(row):
    """The bucket key for an outcome row: an explicit complexity_sig wins, then
    a canonical dict (complexity / required_categories), then a profile id
    (keyed by name when the registry is unavailable) — else None (honest:
    imported gateway rows carry no declared set)."""
    sig = row.get('complexity_sig')
    if isinstance(sig, str) and sig:
        return sig
    for field in ('required_categories', 'complexity'):
        v = row.get(field)
        if isinstance(v, (dict, list)):
            s = complexity_sig(v)
            if s:
                return s
    pid = row.get('profile_id') or (row.get('complexity')
                                    if isinstance(row.get('complexity'), str) else None)
    if isinstance(pid, str) and pid.strip():
        return f'profile:{pid.strip()}'
    return None


def compute_averages(rows, scales_h=DEFAULT_SCALES_H, merge_backends=False, now_s=None):
    """Bucket = (source_system, provider, model, complexity_sig) — the signature
    of the declared requirement SET (TR-065 R1/R3), not a single scalar. Rows
    that declare nothing share the None bucket, so a per-model average is still
    available to callers who never declare complexity.

    Per scale each bucket carries every metric the sort rules consume:
    avg_cost_task_<s>h, avg_tokens_in_<s>h, avg_tokens_out_<s>h,
    avg_tokens_total_<s>h, avg_turns_<s>h, avg_wall_time_<s>h — (the token/turn/
    wall keys carry no `_task` infix; only cost does. Verified against the code,
    docs/outcomes-schema.md and router_spawn's consumers 2026-09-23.)
    None whenever no sample carries that metric (never fabricate).
    """
    now_s = now_s or time.time()
    buckets, sig_dicts = {}, {}
    for r in rows:
        sig = row_complexity_sig(r)
        key = ((r['provider'], r['model'], sig) if merge_backends
               else (r.get('source_system'), r['provider'], r['model'], sig))
        buckets.setdefault(key, []).append(r)
        if sig:
            canon = canonical_complexity(r.get('required_categories')) or \
                canonical_complexity(r.get('complexity'))
            if canon:
                sig_dicts[sig] = canon
    out = []
    for key, brows in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        if merge_backends:
            prov, model, sig = key
            entry = {'provider': prov, 'model': model}
        else:
            src, prov, model, sig = key
            entry = {'source_system': src, 'provider': prov, 'model': model}
        entry['complexity_sig'] = sig
        entry['required_categories'] = sig_dicts.get(sig) if sig else None
        for s in scales_h:
            entry[f'avg_cost_task_{s}h'] = bucket_weighted(brows, 'cost_usd', s, now_s=now_s)
            entry[f'avg_wall_time_{s}h'] = bucket_weighted(brows, 'wall_time_s', s, now_s=now_s)
            entry[f'avg_turns_{s}h'] = bucket_weighted(brows, 'turns', s, now_s=now_s)
            entry[f'avg_tokens_in_{s}h'] = bucket_weighted(brows, 'tokens_in', s, now_s=now_s)
            entry[f'avg_tokens_out_{s}h'] = bucket_weighted(brows, 'tokens_out', s, now_s=now_s)
            tot = [{'ts': r.get('ts'), 'tokens_total': (r.get('tokens_in') or 0) + (r.get('tokens_out') or 0)}
                   for r in brows
                   if r.get('tokens_in') is not None or r.get('tokens_out') is not None]
            entry[f'avg_tokens_total_{s}h'] = bucket_weighted(tot, 'tokens_total', s, now_s=now_s) if tot else None
        entry['n_samples'] = len(brows)
        entry['n_completed'] = sum(1 for r in brows if r.get('success'))
        known = sum(1 for r in brows if r.get('success') is not None)
        entry['n_success_known'] = known
        entry['success_rate'] = (entry['n_completed'] / known) if known else None
        out.append(entry)
    return out


def merge_average_rows(rows):
    """Collapse per-backend average rows into one row per (provider, model,
    complexity) — sample-count weighted, so a 100k-sample backend outweighs a
    2-sample one instead of being averaged as an equal.

    This is the MERGE half of TR-049 component 4: resolve-time merge stats.
    Rows without an n_samples count contribute weight 0 to the merge but still
    count toward `n_samples` presence; every weighted metric is None when no
    contributing row carries it.
    """
    groups = {}
    for r in rows:
        key = (r.get('provider'), r.get('model'), r.get('complexity_sig'))
        groups.setdefault(key, []).append(r)
    metric_fields = sorted({k for r in rows for k in r
                            if k.startswith('avg_')})
    out = []
    for key, grows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        prov, model, sig = key
        entry = {'provider': prov, 'model': model, 'complexity_sig': sig,
                 'required_categories': next((r.get('required_categories') for r in grows
                                              if r.get('required_categories')), None)}
        for f in metric_fields:
            wsum = xsum = 0.0
            for r in grows:
                v, w = r.get(f), (r.get('n_samples') or 0)
                if v is None or w <= 0:
                    continue
                wsum += w
                xsum += w * v
            entry[f] = (xsum / wsum) if wsum else None
        entry['n_samples'] = sum((r.get('n_samples') or 0) for r in grows)
        entry['n_completed'] = sum((r.get('n_completed') or 0) for r in grows)
        known = sum((r.get('n_success_known') or 0) for r in grows)
        entry['n_success_known'] = known
        entry['success_rate'] = (entry['n_completed'] / known) if known else None
        entry['backends'] = sorted({r.get('source_system') for r in grows
                                    if r.get('source_system')})
        out.append(entry)
    return out


def required_levels(profile_id=None, matrix=None, registry_path=None):
    """THE authority for "what levels must a lane clear for this task" (TR-142).

    Until now this derivation existed in several places — profile_signature here,
    and three separate reads of `task_profile_requirements` in router_spawn — so
    the levels a task must clear could drift between the path that picks a model
    and the path that reports why. One function, one answer.

    `profile_id` resolves the levels a named profile declares; `matrix` returns a
    supplied set normalized to {category: int level}. Both may be given (the
    matrix wins for whatever it names). Unknown profile / unusable input -> None,
    never a guessed level set.
    """
    levels = {}
    if matrix is not None:
        levels.update(_normalize_levels(matrix))
    if profile_id:
        registry_path = registry_path or os.path.join(REPO, 'registry.json')
        if os.path.exists(registry_path):
            try:
                tables = (json.load(open(registry_path)) or {}).get('tables', {})
            except (ValueError, OSError):
                tables = {}
            known = {p.get('id') for p in tables.get('task_profiles') or []}
            if profile_id in known:
                for r in tables.get('task_profile_requirements') or []:
                    if r.get('task_id') == profile_id and r.get('category') is not None:
                        # The MATRIX wins: it is the live classification of the
                        # actual prompt, so the profile may only fill in the
                        # categories the matrix does not name. Overwriting here is
                        # the drift this function exists to prevent (caught by the
                        # test that supplies both).
                        if str(r['category']) in levels:
                            continue
                        try:
                            levels[str(r['category'])] = int(r.get('level', 0))
                        except (TypeError, ValueError):
                            continue
            elif not levels:
                return None      # unknown profile AND no matrix: refuse to guess
    return levels or None


def _normalize_levels(matrix):
    """Accept the shapes the fleet actually emits: {cat: level},
    [{category, level}], or {'cat=3', 'other=-2'}."""
    out = {}
    if isinstance(matrix, dict):
        for k, v in matrix.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[str(k)] = int(v)
            elif isinstance(v, str) and '=' in str(k):
                cat, _, val = str(k).partition('=')
                try:
                    out[cat] = int(val)
                except ValueError:
                    continue
    elif isinstance(matrix, (list, tuple, set, frozenset)):   # the fleet emits sets too: {"c=3","d=-2"}
        for item in matrix:
            if isinstance(item, dict) and item.get('category') is not None:
                try:
                    out[str(item['category'])] = int(item.get('level', 0))
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, str) and '=' in item:
                cat, _, val = item.partition('=')
                try:
                    out[cat] = int(val)
                except ValueError:
                    continue
    return out


def profile_signature(profile_id, registry_path=None):
    """The complexity reference: a task profile IS its declared per-category
    levels (Bane: "complexity part is the categories of the task so that it
    has a reference"). Returns {category: required_level} from the seeded
    registry, or None when the profile is unknown — never invent levels."""
    # TR-142: delegate to required_levels() — this function and the spawn path
    # used to derive the same levels independently, which is how a reference can
    # disagree with the thing it describes.
    return required_levels(profile_id=profile_id, registry_path=registry_path)


# ---------- IO ----------

def append_rows(path, new_rows):
    """Idempotent append: skip rows whose (source_system, session_id, model)
    is already present. Returns appended count.

    Bulk path (CLI import): scans the WHOLE store once, then appends. Not used
    by the HTTP ingest — see append_row_fast for why.
    """
    seen = set()
    if os.path.exists(path):
        for l in open(path):
            if l.strip():
                r = json.loads(l)
                seen.add((r.get('source_system'), r.get('session_id'), r.get('model')))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = 0
    with open(path, 'a') as f:
        for r in new_rows:
            k = (r.get('source_system'), r.get('session_id'), r.get('model'))
            if k in seen:
                continue
            seen.add(k)
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
            n += 1
    return n


def tail_rows(path, max_lines=2000, max_bytes=512 * 1024):
    """The last `max_lines` complete JSONL rows (bounded read).

    Reads at most `max_bytes` from the end of the file so the duplicate guard
    on a live store stays O(tail), not O(store) — the live store is ~90 MB.
    """
    if not os.path.exists(path):
        return []
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        start = max(0, size - max_bytes)
        f.seek(start)
        chunk = f.read()
    if start > 0:
        # drop the partial first line
        chunk = chunk.split(b'\n', 1)[1] if b'\n' in chunk else b''
    lines = [l for l in chunk.decode('utf-8', errors='replace').splitlines() if l.strip()]
    rows = []
    for l in lines[-max_lines:]:
        try:
            rows.append(json.loads(l))
        except ValueError:
            continue
    return rows


def _merge_task_rows(prev, incoming):
    """Merge one step of a task into its accumulated task row.

    Sums what a task spends (tokens, cost, wall time), counts `turns` as the
    number of steps merged, keeps `success` sticky (a task that ever succeeded is
    a success) and lets the NEWEST descriptive fields win without letting a blank
    incoming value erase a known one. Additive by construction: a lane that needs
    three steps costs three steps, which is exactly what the cost-per-task
    averages must see."""
    out = dict(prev)
    for field in ('tokens_in', 'tokens_out', 'tokens_reasoning', 'cost_usd', 'wall_time_s'):
        a, b = prev.get(field), incoming.get(field)
        if a is None and b is None:
            out[field] = None                      # still unmeasured, not zero
        else:
            out[field] = (a or 0) + (b or 0)
    prev_turns, inc_turns = prev.get('turns'), incoming.get('turns')
    out['turns'] = (prev_turns or 0) + (inc_turns if isinstance(inc_turns, int) and inc_turns > 0 else 1)
    out['steps'] = (prev.get('steps') or 1) + 1
    out['success'] = bool(prev.get('success')) or bool(incoming.get('success'))
    for field in ('ts', 'task_label', 'complexity', 'profile_id', 'required_categories',
                  'complexity_sig', 'provider', 'model', 'session_id', 'source_system',
                  'hermes_session'):
        value = incoming.get(field)
        if value not in (None, '', {}, []):
            out[field] = value
    return out


def accumulate_row(path, row, tail_lines=2000, tail_bytes=512 * 1024):
    """Append-or-accumulate ONE normalized row for a TASK (cost-per-task engine).

    The store's unit is one row per finished task/session (docs/outcomes-schema.md)
    and its identity is (source_system, session_id, model). A session makes MANY
    calls — one per step — so appending each step as its own row loses the
    association (and the plain dedupe silently DROPS steps 2..N), while inventing a
    unique id per step destroys the join to the session. This does neither: the
    first step appends, later steps of the same (source_system, session_id, model)
    MERGE into that row, so tokens/cost/steps accumulate on the row that
    represents the task.

    Bounded work: only the last `tail_bytes` of the store are read and only the
    matched line is rewritten — a live ~90 MB store costs O(tail), not O(store).
    Returns (mode, reason) with mode in ('accumulated', 'appended', 'failed');
    never raises for an I/O problem (callers are live request paths)."""
    if not isinstance(row, dict):
        raise ValueError('row must be an object')
    row = dict(row)
    row.setdefault('steps', 1)          # the first step is still a step count
    if row.get('turns') is None:
        # For a proxied request one model call IS one turn; the counter then
        # tracks the session's steps on the accumulated task row.
        row['turns'] = 1
    key = (row.get('source_system'), row.get('session_id'), row.get('model'))
    payload = json.dumps(row, ensure_ascii=False).encode() + b'\n'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            open(path, 'ab').close()
        # NOT 'a+b': append mode redirects every write to EOF regardless of seek,
        # which turned the in-place merge into a second appended row (caught by
        # tests/test_outcome_accumulation.py).
        with open(path, 'r+b') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                size = f.seek(0, os.SEEK_END)
                start = max(0, size - tail_bytes)
                f.seek(start)
                chunk = f.read()
                body_off = 0
                if start > 0:
                    cut = chunk.find(b'\n')
                    if cut == -1:                 # window holds no complete line
                        f.seek(0, os.SEEK_END)
                        f.write(payload)
                        f.flush()
                        os.fsync(f.fileno())
                        return 'appended', 'no complete row in the tail window'
                    body_off = start + cut + 1
                    chunk = chunk[cut + 1:]
                lines = chunk.split(b'\n')
                offset = body_off
                found = None
                for raw in lines:
                    if raw.strip():
                        try:
                            prev = json.loads(raw)
                        except ValueError:
                            prev = None
                        if prev is not None and (prev.get('source_system'), prev.get('session_id'),
                                                 prev.get('model')) == key:
                            found = (offset, prev, len(raw) + 1)
                    offset += len(raw) + 1
                if found is None:
                    f.seek(0, os.SEEK_END)
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                    return 'appended', 'first row for this task'
                line_off, prev, line_len = found
                merged = _merge_task_rows(prev, row)
                rest = chunk[(line_off - body_off) + line_len:]
                f.seek(line_off)
                f.write(json.dumps(merged, ensure_ascii=False).encode() + b'\n' + rest)
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
                return 'accumulated', f'merged step {merged.get("steps")} into the task row'
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as exc:
        return 'failed', f'write failed: {exc}'


def append_row_fast(path, row, tail_lines=2000):
    """Append ONE normalized row: flock-serialized write + bounded-tail dedupe.

    Returns (appended: bool, reason: str). Never raises for an I/O problem the
    caller cannot fix — the HTTP ingest is fail-open (a store hiccup must not
    turn into a 500). Raises only on a programming error (non-dict row).

    Dedupe semantics: the same (source_system, session_id, model) inside the
    recent tail is treated as a re-POST and skipped; older duplicates are
    accepted. The average is decay-weighted, so a stale duplicate cannot
    dominate a bucket, while the O(tail) guard keeps ingest ~ms.
    """
    if not isinstance(row, dict):
        raise ValueError('row must be an object')
    key = (row.get('source_system'), row.get('session_id'), row.get('model'))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # flock the store itself (no sidecar lock file to leak on crash).
        with open(path, 'a+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                if key[1] is not None:
                    for prev in tail_rows(path, max_lines=tail_lines):
                        if (prev.get('source_system'), prev.get('session_id'),
                                prev.get('model')) == key:
                            return False, 'duplicate (same source_system/session_id/model in tail)'
                f.seek(0, os.SEEK_END)
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as exc:
        return False, f'write failed: {exc}'
    return True, 'appended'


# ---------- ingest (TR-049 component 1) ----------

_REQUIRED_INGEST = ('source_system', 'session_id', 'provider', 'model')
# Field order of a store row = the order normalize_row emits + the docs table.
STORE_FIELDS = ('source_system', 'session_id', 'task_label', 'complexity',
                'profile_id', 'required_categories', 'provider', 'model',
                'turns', 'tokens_in', 'tokens_out', 'tokens_reasoning',
                'cost_usd', 'wall_time_s', 'success', 'ts')


def _required_str(body, name, problems):
    v = body.get(name)
    if not isinstance(v, str) or not v.strip():
        problems.append(f'{name} must be a non-empty string')
        return None
    return v.strip()


def _optional_number(body, name, problems, integer=False):
    v = body.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        problems.append(f'{name} must be a number or null')
        return None
    if integer and float(v) != int(v):
        problems.append(f'{name} must be an integer')
        return None
    return int(v) if integer else float(v)


def normalize_row(body, now_s=None):
    """Validate + normalize an ingest payload into a store row.

    Accepts the wire names from the TR-049 contract (cost, wall_time) plus the
    store's own names (cost_usd, wall_time_s) so a re-posted row round-trips.
    Raises ValueError listing EVERY problem (never partially accepts).
    """
    if not isinstance(body, dict):
        raise ValueError('payload must be a JSON object')
    problems = []
    row = {}
    for name in ('source_system', 'session_id', 'provider', 'model'):
        row[name] = _required_str(body, name, problems)
    # task_label: optional free text ('' allowed, None when absent)
    tl = body.get('task_label')
    if tl is not None and not isinstance(tl, str):
        problems.append('task_label must be a string or null')
        tl = None
    row['task_label'] = tl
    # complexity: per-category levels (object) or a profile id (string) — the
    # reference Bane asked for; never invented, so anything else is rejected.
    cx = body.get('complexity')
    if cx is not None and not isinstance(cx, (dict, str)):
        problems.append('complexity must be an object of category levels, '
                        'a profile id string, or null')
        cx = None
    if isinstance(cx, str) and not cx.strip():
        cx = None
    row['complexity'] = cx
    pid = body.get('profile_id')
    if pid is not None and not isinstance(pid, str):
        problems.append('profile_id must be a string or null')
        pid = None
    row['profile_id'] = pid
    req_cats = body.get('required_categories')
    if req_cats is not None and not isinstance(req_cats, (dict, list)):
        problems.append('required_categories must be an object/list or null')
        req_cats = None
    row['required_categories'] = req_cats
    row['turns'] = _optional_number(body, 'turns', problems, integer=True)
    for name in ('tokens_in', 'tokens_out', 'tokens_reasoning'):
        row[name] = _optional_number(body, name, problems, integer=True)
    cost = body.get('cost_usd', body.get('cost'))
    if 'cost_usd' in body and 'cost' in body and body['cost'] != body['cost_usd']:
        problems.append('cost and cost_usd disagree — send one')
    if cost is None:
        row['cost_usd'] = None
    elif isinstance(cost, bool) or not isinstance(cost, (int, float)):
        problems.append('cost must be a number or null')
        row['cost_usd'] = None
    else:
        row['cost_usd'] = float(cost)
    wall = body.get('wall_time_s', body.get('wall_time'))
    if 'wall_time_s' in body and 'wall_time' in body and body['wall_time'] != body['wall_time_s']:
        problems.append('wall_time and wall_time_s disagree — send one')
    if wall is None:
        row['wall_time_s'] = None
    elif isinstance(wall, bool) or not isinstance(wall, (int, float)):
        problems.append('wall_time must be a number or null')
        row['wall_time_s'] = None
    else:
        row['wall_time_s'] = float(wall)
    success = body.get('success')
    if success is not None and not isinstance(success, bool):
        problems.append('success must be true, false, or null')
        success = None
    row['success'] = success
    ts = body.get('ts')
    if ts is None:
        ts = now_s if now_s is not None else time.time()
    elif isinstance(ts, bool) or not isinstance(ts, (int, float)):
        problems.append('ts must be an epoch number or null')
        ts = now_s if now_s is not None else time.time()
    row['ts'] = float(ts)
    if problems:
        raise ValueError('; '.join(problems))
    return row


def ingest(payload, path=None, now_s=None):
    """End-to-end ingest for one payload: normalize → locked append.

    Fail-open by contract (TR-049): validation problems raise ValueError (the
    caller reports 400 — a malformed payload is a caller bug), a WRITE problem
    returns {'appended': False, 'error': ...} so the caller still answers 200.
    """
    row = normalize_row(payload, now_s=now_s)
    store = path or outcomes_path()
    appended, reason = append_row_fast(store, row)
    out = {'appended': bool(appended), 'reason': reason, 'store': store,
           'row': row}
    if not appended:
        out['error'] = reason
    return out


_PRICE_MAP = None


_PRICE_MAP = None


def _registry_prices():
    """provider/model -> (public_in_per_m, public_out_per_m, normalized_price, public_price).

    TR-070 divides the labour: a DRIVER reports what its own accounting knows (for a
    subscription lane that is 0.0 by config), and PRICING A LANE IS THE ROUTER'S JOB. This is the
    router's half, read once per import. The PLAN OFFSET is not a multiplier column - it is already
    baked into `normalized_price` (xkiro luna: public_price 0.116 -> normalized 0.003867, exactly
    /30; opencode-go mimo: 0.1456 -> 0.012757, its per-request bucket rate), so the lane's own
    ratio is what scales a token split.
    """
    global _PRICE_MAP
    if _PRICE_MAP is not None:
        return _PRICE_MAP
    tables = {'models': []}
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'data', 'tables', 'models.jsonl')
        tables = {'models': [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]}
    except Exception:  # noqa: BLE001
        pass
    m = {}
    for d in (tables.get('models') or []):
        pin, pout = d.get('public_in_per_m'), d.get('public_out_per_m')
        norm, pub = d.get('normalized_price'), d.get('public_price')
        if (pin is None and pout is None) and (norm is None):
            continue
        m[(d.get('provider'), d.get('model'))] = (
            float(pin) if pin is not None else None,
            float(pout) if pout is not None else None,
            float(norm) if norm is not None else None,
            float(pub) if pub is not None else None)
    _PRICE_MAP = m
    return m


def plan_effective_cost(provider, model, tokens_in, tokens_out):
    """(cost, basis) for a lane the driver priced at ZERO.

    A driver's 0.0 is not a price: these providers are paid plans, and a plan lane must never
    report free (Bane's pricing law - list on public_price, the plan offset on normalized_price).
    When the lane declares a price and the session has usage, the router supplies the
    plan-effective figure it is responsible for, scaled by the lane's own plan ratio
    (normalized_price / public_price). Returns (None, reason) when no price is declared - the
    honest NULL, never a made-up number - and a lane with no declared price keeps whatever the
    driver said (a genuine free promo zero must not be overwritten).
    """
    got = _registry_prices().get((provider, model))
    if got is None:
        return None, 'no declared price for this lane; driver reported 0'
    pin, pout, norm, pub = got
    if not tokens_in and not tokens_out:
        return None, 'no usage on the row; price not applied'
    if pin is None and pout is None:
        if norm is None:
            return None, 'no declared price for this lane; driver reported 0'
        # a blend-only lane: apply the all-in figure to the tokens actually used
        total = (tokens_in or 0) + (tokens_out or 0)
        return round(total / 1e6 * norm, 8), 'plan-effective blend (driver reported $0)'
    list_cost = (tokens_in or 0) / 1e6 * (pin or 0.0) + (tokens_out or 0) / 1e6 * (pout or 0.0)
    if norm is None:
        return round(list_cost, 8), 'public split (driver reported $0)'
    if not pub:
        return round(list_cost, 8), 'public split (driver reported $0)'
    ratio = norm / pub
    basis = f'plan-effective: public split x plan ratio {ratio:.6g} (normalized/public; driver reported $0)'
    if ratio == 1:
        basis = 'public split (driver reported $0)'
    return round(list_cost * ratio, 8), basis


def import_hermes(db_path='~/.hermes/state.db'):
    """Hermes driver: session_model_usage -> outcome rows.
    complexity stays NULL (no declared profile at the gateway yet — TR-050's
    classifier will supply it later; DATA > CODE, missing = NULL).
    success: NULL (the gateway does not report completion; do not infer)."""
    db = sqlite3.connect(os.path.expanduser(db_path))
    rows = db.execute("""
        SELECT session_id, model, billing_provider, billing_base_url, task,
               api_call_count, COALESCE(input_tokens,0), COALESCE(output_tokens,0),
               COALESCE(reasoning_tokens,0), estimated_cost_usd,
               first_seen, last_seen
        FROM session_model_usage
        WHERE billing_base_url IS NOT NULL
    """).fetchall()

    def _txt(v):
        if isinstance(v, bytes):
            return v.decode('utf-8', errors='replace')
        return v

    # LAW (memory 09-16): billing_provider+model get re-stamped to gateway
    # defaults at gateway restarts; billing_base_url is immutable truth.
    # Derive provider identity from base_url host; fall back to the stamped
    # column only for hosts we have not mapped.
    HOST2PROVIDER = {
        'api.xkiro.com': 'xkiro', 'api.cline.bot': 'clinepass',
        'openrouter.ai': 'openrouter', 'api.deepseek.com': 'deepseek',
        'api.minimax.io': 'minimax', 'api.stepfun.com': 'stepfun',
        'api.synthetic.new': 'synthetic', 'api.groq.com': 'groq',
        'api.fireworks.ai': 'fireworks-ai', 'ollama.com': 'ollama-cloud',
    }

    def provider_of(base_url, stamped):
        for host, name in HOST2PROVIDER.items():
            if host in (base_url or ''):
                return name
        return stamped or 'unknown'

    out = []
    for (sid, model, prov, base, task, calls, tin, tout, treason, cost, t0, t1) in rows:
        sid, model, prov, base, task = map(_txt, (sid, model, prov, base, task))
        # column-drift era rows (provider/model hold numerics/epoch floats/
        # hex ints) are not lanes — a provider or model must not be purely
        # numeric in any base
        def _is_numericish(v):
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
        if _is_numericish(model) or _is_numericish(prov):
            continue
        # corrupted estimates (timestamp leaked into price column, >$1000 for
        # one session) are not costs — NULL, never fabricate
        if cost is not None and cost > 1000:
            cost = None
        ts = float(t1) if t1 is not None else None
        wall = (float(t1) - float(t0)) if (t0 is not None and t1 is not None) else None
        # TR-070: pricing a lane is the router's job, so a driver's zero is replaced by the
        # plan-effective figure when the lane declares a price. Measured before this:
        # 95 of 110 hermes cost buckets read exactly 0.0 (custom 131k samples, ollama-cloud 81k,
        # opencode-go 11k, synthetic 7k ...) while the registry prices those very lanes, so the
        # rolling cost-per-task the chain sort feeds on was zero across the whole fleet.
        basis = None
        if not cost:
            _est, basis = plan_effective_cost(provider_of(base, prov), model,
                                              tin or 0, tout or 0)
            cost = _est
        out.append({'source_system': 'hermes', 'session_id': sid, 'task_label': task,
                    'complexity': None, 'profile_id': None,
                    'required_categories': None,
                    'provider': provider_of(base, prov), 'model': model,
                    'turns': calls, 'tokens_in': tin, 'tokens_out': tout,
                    'tokens_reasoning': treason, 'cost_usd': cost,
                    'price_basis': basis,
                    'wall_time_s': wall, 'success': None, 'ts': ts})
    return out


def import_pi(sessions_dir='~/.pi/agent/sessions'):
    """pi driver (TR-074): pi session JSONL -> outcome rows.

    pi writes one JSONL per session under
    `~/.pi/agent/sessions/<cwd-slug>/<timestamp>_<uuid>.jsonl`. The first line is
    a `session` header (id, timestamp, cwd); assistant messages carry a `usage`
    object (input/output/cacheRead/cacheWrite/totalTokens and a cost block) and a
    `stopReason`. Verified against a real session file, not assumed from docs.

    Unlike hermes (whose gateway does not report completion, so success stays
    NULL), pi DOES report `stopReason`, so success is genuinely known here —
    inferred only when an assistant message exists, never fabricated.

    cost_usd is the session's own summed `cost.total`. pi computes it from the
    cost table in the provider config, and the router's config declares zeros
    (lane pricing is the router's job, TR-070), so a driver-shaped session
    reports 0.0 rather than a fabricated number.
    """
    import glob
    base = os.path.expanduser(sessions_dir)
    out = []
    # pi's layout is <sessions>/<cwd-slug>/<file>.jsonl, but a flat directory of
    # session files is also valid input (and is how tests construct fixtures), so
    # glob recursively rather than encoding the depth.
    for path in sorted(glob.glob(os.path.join(base, '**', '*.jsonl'),
                                 recursive=True)):
        sid = None
        first_ts = last_ts = None
        cwd = None
        provider = model = None
        turns = 0
        tin = tout = tcread = tcwrite = 0
        cost_total = 0.0
        saw_usage = False
        stop_reasons = []
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    t = d.get('type')
                    if t == 'session':
                        sid = d.get('id') or sid
                        cwd = d.get('cwd') or cwd
                        ts = _iso_to_epoch(d.get('timestamp'))
                        first_ts = first_ts or ts
                        last_ts = ts or last_ts
                        continue
                    if t != 'message':
                        continue
                    m = d.get('message') or {}
                    ts = _iso_to_epoch(d.get('timestamp'))
                    if ts:
                        first_ts = first_ts if first_ts is not None else ts
                        last_ts = ts
                    if m.get('role') != 'assistant':
                        continue
                    turns += 1
                    provider = m.get('provider') or provider
                    model = m.get('model') or model
                    if isinstance(m.get('stopReason'), str):
                        stop_reasons.append(m['stopReason'])
                    u = m.get('usage')
                    if isinstance(u, dict):
                        saw_usage = True
                        tin += int(u.get('input') or 0)
                        tout += int(u.get('output') or 0)
                        tcread += int(u.get('cacheRead') or 0)
                        tcwrite += int(u.get('cacheWrite') or 0)
                        c = u.get('cost')
                        if isinstance(c, dict):
                            cost_total += float(c.get('total') or 0)
        except OSError:
            continue

        if not sid:
            continue
        if not saw_usage and not turns:
            continue          # nothing to report; never fabricate a row
        if model is None and provider is None:
            continue
        success = None
        if stop_reasons:
            success = not any(r == 'error' for r in stop_reasons)
        wall = (last_ts - first_ts) if (first_ts is not None and last_ts is not None) else None
        out.append({'source_system': 'pi', 'session_id': sid,
                    'task_label': cwd, 'complexity': None, 'profile_id': None,
                    'required_categories': None,
                    'provider': provider, 'model': model,
                    'turns': turns, 'tokens_in': tin or None,
                    'tokens_out': tout or None,
                    'tokens_cache_read': tcread or None,
                    'tokens_cache_write': tcwrite or None,
                    'tokens_reasoning': None,
                    'cost_usd': (cost_total if saw_usage else None),
                    'wall_time_s': wall, 'success': success,
                    'ts': last_ts})
    return out


def import_opencode(db_path='~/.local/share/opencode/opencode.db'):
    """opencode driver (TR-073): opencode's SQLite store -> outcome rows.

    The T surface is NOT a directory of session JSON files (unlike pi): opencode
    keeps everything in one SQLite DB. Shape verified against real data on this
    box (137 sessions / 3,870 messages), not assumed:

      * `message.data` is JSON; an assistant message carries
        `tokens` = {total, input, output, reasoning, cache:{read, write}},
        a real `cost`, the lane at `providerID`/`modelID`, and
        `time.{created, completed}` in epoch MILLISECONDS.
      * cache accounting is NESTED under `tokens.cache`, so `cache.read` must be
        summed separately — folding it into `input` would misreport every cached
        request (94,146,162 cache reads across the 3,711 real messages).

    success is reported only when it is genuinely knowable. opencode's `finish`
    is a terminal reason; `unknown` means the turn ended without one, which is
    NOT evidence of failure, so it maps to None rather than a guessed boolean.

    The DB is opened strictly READ-ONLY (and immutably as a fallback): opencode
    may be mid-write, and a reader must never disturb the host's store.
    """
    path = os.path.expanduser(db_path)
    if not os.path.exists(path):
        return []
    out = []
    sessions = {}
    conn = None
    for uri in (f'file:{path}?mode=ro', f'file:{path}?mode=ro&immutable=1'):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            break
        except sqlite3.Error:
            conn = None
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute('SELECT id, title, directory, time_created,'
                                  ' time_updated FROM session'):
                sessions[r['id']] = {'title': r['title'],
                                     'dir': r['directory'],
                                     'created': r['time_created'],
                                     'updated': r['time_updated']}
        except sqlite3.Error:
            pass

        agg = {}
        try:
            rows = conn.execute('SELECT session_id, time_created, data'
                                ' FROM message')
        except sqlite3.Error:
            return []
        for r in rows:
            try:
                d = json.loads(r['data'])
            except (ValueError, TypeError):
                continue
            if d.get('role') != 'assistant':
                continue
            sid = r['session_id']
            a = agg.setdefault(sid, {'turns': 0, 'tin': 0, 'tout': 0,
                                     'treason': 0, 'tcread': 0, 'tcwrite': 0,
                                     'cost': 0.0, 'saw_cost': False,
                                     'provider': None, 'model': None,
                                     'finishes': [], 'first': None,
                                     'last': None})
            a['turns'] += 1
            tk = d.get('tokens')
            if isinstance(tk, dict):
                a['tin'] += int(tk.get('input') or 0)
                a['tout'] += int(tk.get('output') or 0)
                a['treason'] += int(tk.get('reasoning') or 0)
                cache = tk.get('cache')
                if isinstance(cache, dict):
                    a['tcread'] += int(cache.get('read') or 0)
                    a['tcwrite'] += int(cache.get('write') or 0)
            c = d.get('cost')
            if isinstance(c, (int, float)):
                a['saw_cost'] = True
                a['cost'] += float(c)
            a['provider'] = d.get('providerID') or a['provider']
            a['model'] = d.get('modelID') or a['model']
            if isinstance(d.get('finish'), str):
                a['finishes'].append(d['finish'])
            t = d.get('time') or {}
            start = t.get('created')
            end = t.get('completed') or t.get('created')
            ms = r['time_created']
            if isinstance(start, (int, float)):
                start = start / 1000.0
            elif isinstance(ms, (int, float)):
                start = ms / 1000.0
            else:
                start = None
            if isinstance(end, (int, float)):
                end = end / 1000.0
            elif isinstance(ms, (int, float)):
                end = ms / 1000.0
            else:
                end = None
            if start is not None:
                a['first'] = start if a['first'] is None else min(a['first'], start)
            if end is not None:
                a['last'] = end if a['last'] is None else max(a['last'], end)
    finally:
        conn.close()

    for sid, a in agg.items():
        if not a['turns']:
            continue
        meta = sessions.get(sid) or {}
        if a['model'] is None and a['provider'] is None:
            continue          # nothing to attribute; never fabricate a row
        # `unknown` is NOT a failure signal — only an explicit error is.
        if any(f == 'error' for f in a['finishes']):
            success = False
        elif any(f in ('stop', 'tool-calls') for f in a['finishes']):
            success = True
        else:
            success = None
        created = meta.get('created')
        updated = meta.get('updated')
        if isinstance(created, (int, float)):
            created = created / 1000.0
        else:
            created = a['first']
        if isinstance(updated, (int, float)):
            updated = updated / 1000.0
        else:
            updated = a['last']
        wall = (updated - created
                if isinstance(created, (int, float))
                and isinstance(updated, (int, float)) else None)
        out.append({'source_system': 'opencode', 'session_id': sid,
                    'task_label': meta.get('title') or meta.get('dir'),
                    'complexity': None, 'profile_id': None,
                    'required_categories': None,
                    'provider': a['provider'], 'model': a['model'],
                    'turns': a['turns'],
                    'tokens_in': a['tin'] or None,
                    'tokens_out': a['tout'] or None,
                    'tokens_reasoning': a['treason'] or None,
                    'tokens_cache_read': a['tcread'] or None,
                    'tokens_cache_write': a['tcwrite'] or None,
                    'cost_usd': (a['cost'] if a['saw_cost'] else None),
                    'wall_time_s': wall, 'success': success,
                    'ts': a['last'] or updated})
    return sorted(out, key=lambda r: (r['ts'] or 0, r['session_id']))


def import_openclaw(state_dir='~/.openclaw'):
    """openclaw driver (TR-072): openclaw's agent store -> outcome rows.

    The T surface is a per-agent SQLite DB, not JSON files and not the same schema
    as opencode: `<state>/agents/<agentId>/agent/openclaw-agent.sqlite`. Shape
    derived from a REAL run on this box (18 transcript events / 18 trajectory
    events from one live session), not assumed:

      * `transcript_events.event_json` holds one canonical transcript event per
        row; an assistant turn is `{type:'message', message:{role:'assistant',
        api, provider, model, usage:{input, output, cacheRead, cacheWrite,
        totalTokens, cost:{total}}, stopReason, timestamp(ms)}}`.
      * the lane is `message.provider` / `message.model` (the CONFIGURED route),
        not `responseModel` — the latter is whatever the upstream answered with.
      * `stopReason` is a real terminal reason, so success is genuinely knowable
        here; there is no observed 'error' value on this box, so only the clear
        failure vocabulary is treated as failure and anything else stays None
        rather than being guessed.
      * cache is flat (`usage.cacheRead`), unlike opencode's nested
        `tokens.cache.read` — the two hosts must not share a reader.

    Multiple agents are scanned: a state dir can hold more than one agent DB, and
    silently reading only the first would under-report.

    Every DB is opened strictly READ-ONLY (immutable fallback): openclaw may be
    mid-write and a reader must never disturb the host's store.
    """
    base = os.path.expanduser(state_dir)
    if not os.path.isdir(base):
        return []
    import glob
    out = []
    for db in sorted(glob.glob(os.path.join(base, 'agents', '*', 'agent',
                                            '*.sqlite'))):
        out.extend(_openclaw_db_rows(db))
    return sorted(out, key=lambda r: (r['ts'] or 0, r['session_id']))


#: stop reasons that genuinely mean the turn failed (never guessed beyond these)
_OPENCLAW_FAILURE_REASONS = frozenset(
    {'error', 'aborted', 'abort', 'timeout', 'timedOut', 'refusal'})


def _openclaw_db_rows(path):
    """Rows from one openclaw agent DB. Malformed/unreadable -> no rows."""
    conn = None
    for uri in (f'file:{path}?mode=ro', f'file:{path}?mode=ro&immutable=1'):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            break
        except sqlite3.Error:
            conn = None
    if conn is None:
        return []
    rows = []
    try:
        conn.row_factory = sqlite3.Row
        agg = {}
        sessions = {}
        try:
            cur = conn.execute('SELECT session_id, event_json'
                               ' FROM transcript_events ORDER BY seq')
        except sqlite3.Error:
            return []
        for r in cur:
            try:
                d = json.loads(r['event_json'])
            except (ValueError, TypeError):
                continue
            sid = r['session_id']
            t = d.get('type')
            if t == 'session':
                sessions[sid] = d
                continue
            if t != 'message':
                continue
            m = d.get('message') or {}
            if m.get('role') != 'assistant':
                continue
            a = agg.setdefault(sid, {'turns': 0, 'tin': 0, 'tout': 0,
                                     'tcread': 0, 'tcwrite': 0, 'cost': 0.0,
                                     'saw_cost': False, 'provider': None,
                                     'model': None, 'reasons': [],
                                     'first': None, 'last': None})
            a['turns'] += 1
            u = m.get('usage')
            if isinstance(u, dict):
                a['tin'] += int(u.get('input') or 0)
                a['tout'] += int(u.get('output') or 0)
                a['tcread'] += int(u.get('cacheRead') or 0)
                a['tcwrite'] += int(u.get('cacheWrite') or 0)
                c = u.get('cost')
                if isinstance(c, dict) and c.get('total') is not None:
                    a['saw_cost'] = True
                    a['cost'] += float(c.get('total') or 0)
            # the CONFIGURED lane (message.provider/model), not responseModel
            a['provider'] = m.get('provider') or a['provider']
            a['model'] = m.get('model') or a['model']
            if isinstance(m.get('stopReason'), str):
                a['reasons'].append(m['stopReason'])
            ts = m.get('timestamp')
            if isinstance(ts, (int, float)):
                ts = ts / 1000.0
                a['first'] = ts if a['first'] is None else min(a['first'], ts)
                a['last'] = ts if a['last'] is None else max(a['last'], ts)
        for sid, a in agg.items():
            if not a['turns']:
                continue
            if a['model'] is None and a['provider'] is None:
                continue      # nothing to attribute; never fabricate a row
            if any(x in _OPENCLAW_FAILURE_REASONS for x in a['reasons']):
                success = False
            elif a['reasons']:
                success = True
            else:
                success = None
            s = sessions.get(sid) or {}
            wall = (a['last'] - a['first']
                    if a['first'] is not None and a['last'] is not None else None)
            rows.append({'source_system': 'openclaw', 'session_id': sid,
                         'task_label': s.get('cwd'),
                         'complexity': None, 'profile_id': None,
                         'required_categories': None,
                         'provider': a['provider'], 'model': a['model'],
                         'turns': a['turns'],
                         'tokens_in': a['tin'] or None,
                         'tokens_out': a['tout'] or None,
                         'tokens_reasoning': None,
                         'tokens_cache_read': a['tcread'] or None,
                         'tokens_cache_write': a['tcwrite'] or None,
                         'cost_usd': (a['cost'] if a['saw_cost'] else None),
                         'wall_time_s': wall, 'success': success,
                         'ts': a['last']})
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return rows


def _iso_to_epoch(v):
    """ISO-8601 (with trailing Z) -> epoch seconds. None when unparseable."""
    if not isinstance(v, str) or not v:
        return None
    s = v.strip().replace('Z', '+00:00')
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('import-hermes')
    sub.add_parser('import-pi')
    sub.add_parser('import-opencode')
    sub.add_parser('import-openclaw')
    p_avg = sub.add_parser('averages')
    p_avg.add_argument('--merge-backends', action='store_true')
    p_q = sub.add_parser('query')
    p_q.add_argument('--provider'); p_q.add_argument('--model')
    p_q.add_argument('--merge-backends', action='store_true')
    args = ap.parse_args()

    if args.cmd == 'import-hermes':
        n = append_rows(OUTCOMES, import_hermes())
        print(f'outcomes: +{n} new rows (idempotent)')
    elif args.cmd == 'import-pi':
        n = append_rows(OUTCOMES, import_pi())
        print(f'outcomes: +{n} new rows (idempotent)')
    elif args.cmd == 'import-opencode':
        n = append_rows(OUTCOMES, import_opencode())
        print(f'outcomes: +{n} new rows (idempotent)')
    elif args.cmd == 'import-openclaw':
        n = append_rows(OUTCOMES, import_openclaw())
        print(f'outcomes: +{n} new rows (idempotent)')
    elif args.cmd == 'averages':
        rows = [json.loads(l) for l in open(OUTCOMES) if l.strip()] if os.path.exists(OUTCOMES) else []
        avgs = compute_averages(rows, merge_backends=args.merge_backends)
        os.makedirs(os.path.dirname(AVERAGES), exist_ok=True)
        with open(AVERAGES, 'w') as f:
            for a in avgs:
                f.write(json.dumps(a, ensure_ascii=False) + '\n')
        print(f'averages: {len(avgs)} buckets -> {AVERAGES}')
    elif args.cmd == 'query':
        if not os.path.exists(AVERAGES):
            print('no averages file — run `averages` first')
            return 1
        for a in (json.loads(l) for l in open(AVERAGES) if l.strip()):
            if args.provider and a['provider'] != args.provider:
                continue
            if args.model and a['model'] != args.model:
                continue
            print(json.dumps(a))
    return 0


if __name__ == '__main__':
    sys.exit(main())
