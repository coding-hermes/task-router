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
import json
import math
import os
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTCOMES = os.path.join(REPO, 'data', 'state', 'outcomes.jsonl')
AVERAGES = os.path.join(REPO, 'data', 'state', 'outcomes-averages.jsonl')
DEFAULT_SCALES_H = [24, 72, 168]  # 1d / 3d / 7d


# ---------- core (unit-tested) ----------

def decay_weight(age_s, scale_h):
    """Half-life decay: a sample exactly scale_h old weighs 0.5."""
    return 0.5 ** (age_s / (scale_h * 3600.0))


def bucket_avg(rows, scale_h, now_s=None):
    """Weighted mean of cost_usd over rows with a cost, by decay weight.
    Returns None when no row carries a cost (never fabricate)."""
    now_s = now_s or time.time()
    wsum = xsum = 0.0
    for r in rows:
        if r.get('cost_usd') is None:
            continue
        ts = r.get('ts') or now_s
        w = decay_weight(max(0.0, now_s - ts), scale_h)
        wsum += w
        xsum += w * r['cost_usd']
    return None if wsum == 0 else xsum / wsum


def compute_averages(rows, scales_h=DEFAULT_SCALES_H, merge_backends=False, now_s=None):
    """Bucket = (source_system, provider, model, complexity) — or
    (provider, model, complexity) when merging across backends.
    Averages computed per bucket per scale; cost-per-task is the bucket's
    weighted mean session cost (the task unit here = one session)."""
    now_s = now_s or time.time()
    buckets = {}
    for r in rows:
        key = ((r['provider'], r['model'], r.get('complexity')) if merge_backends
               else (r.get('source_system'), r['provider'], r['model'], r.get('complexity')))
        buckets.setdefault(key, []).append(r)
    out = []
    for key, brows in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        if merge_backends:
            prov, model, complexity = key
            entry = {'provider': prov, 'model': model, 'complexity': complexity}
        else:
            src, prov, model, complexity = key
            entry = {'source_system': src, 'provider': prov, 'model': model,
                     'complexity': complexity}
        for s in scales_h:
            entry[f'avg_cost_task_{s}h'] = bucket_avg(brows, s, now_s=now_s)
        entry['n_samples'] = len(brows)
        entry['n_completed'] = sum(1 for r in brows if r.get('success'))
        out.append(entry)
    return out


def profile_signature(profile_id, registry_path=None):
    """The complexity reference: a task profile IS its declared per-category
    levels (Bane: "complexity part is the categories of the task so that it
    has a reference"). Returns {category: required_level} from the seeded
    registry, or None when the profile is unknown — never invent levels."""
    registry_path = registry_path or os.path.join(REPO, 'registry.json')
    if not os.path.exists(registry_path):
        return None
    d = json.load(open(registry_path))
    tables = d.get('tables', {})
    prof = next((p for p in tables.get('task_profiles', [])
                 if p.get('id') == profile_id), None)
    if prof is None:
        return None
    # requirements live in task_profile_requirements: one row per
    # (profile_id, category, min_level) — the declared task categories
    reqs = {}
    for r in tables.get('task_profile_requirements') or []:
        if r.get('task_id') == profile_id:
            reqs[str(r['category'])] = int(r.get('level', 0))
    return reqs


# ---------- IO ----------

def append_rows(path, new_rows):
    """Idempotent append: skip rows whose (source_system, session_id, model)
    is already present. Returns appended count."""
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
        out.append({'source_system': 'hermes', 'session_id': sid, 'task_label': task,
                    'complexity': None, 'profile_id': None,
                    'required_categories': None,
                    'provider': provider_of(base, prov), 'model': model,
                    'turns': calls, 'tokens_in': tin, 'tokens_out': tout,
                    'tokens_reasoning': treason, 'cost_usd': cost,
                    'wall_time_s': wall, 'success': None, 'ts': ts})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('import-hermes')
    p_avg = sub.add_parser('averages')
    p_avg.add_argument('--merge-backends', action='store_true')
    p_q = sub.add_parser('query')
    p_q.add_argument('--provider'); p_q.add_argument('--model')
    p_q.add_argument('--merge-backends', action='store_true')
    args = ap.parse_args()

    if args.cmd == 'import-hermes':
        n = append_rows(OUTCOMES, import_hermes())
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
