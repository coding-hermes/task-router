#!/usr/bin/env python3
"""router_probe_ingest.py — battery results -> benchmarks rows (the rank inputs).

The missing middle of the pipeline: a battery produces scores, the registry
consumes benchmark rows, and nothing connected the two by hand without risking
the defects already paid for once:

  - ONE source key per measured category. BENCH_OVERLAY applies a source's whole
    category list to every row carrying it, so a multi-task battery under a
    multi-category key credits each category with its best task (muse code_gen
    read 0.85 from a 2/3 code task until the keys were split — commit 67d553c).
  - small-n probes are damped: score = 0.85 * checks_passed / checks_total,
    max_score 1.0, n=1, never family-filled (the 09-16/09-25 onboarding shape).
  - idempotent: a row already present for (model, category, source key) is
    skipped, so re-running a battery after a partial run cannot double-stamp.

Usage:
  ~/.hermes/venvs/board/bin/python3 scripts/router_probe_ingest.py \
      --results ~/model_bench/results_muse_agentic.json --battery agentic \
      [--date 2026-09-27] [--dry-run|--commit]
"""
import argparse
import collections
import datetime
import json
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(REPO, 'data', 'tables'))

# battery name -> {result field: (source key, category, max points or None)}
# max points None => the results file already carries a damped score.
BATTERIES = {
    'v5': {
        'T1-TOOL': ('T1-TOOL', 'tool_use', 3),
        'T2-CODE': ('T2-CODE', 'code_gen', 3),
        'T3-REASON': ('T3-REASON', 'reasoning', 3),
        'T5-DEBUG': ('T5-DEBUG', 'debug', 3),
        # T4-INSTR deliberately absent: floor/saturation test, excluded elsewhere
        # as battery-T4-INSTR-floor.
    },
    'agentic': {
        'agent_tick': ('AGENT-TICK', 'agent_tick', None),
        'delegation': ('DELEGATION', 'delegation', None),
        'schema': ('SCHEMA', 'schema', None),
        'long_doc': ('LONG-DOC', 'long_doc', None),
        'long_horizon': ('LONG-HORIZON', 'long_horizon', None),
        'test': ('TEST', 'test', None),
    },
    # the six categories that BLOCK the held retirements; e2e_vision is absent on
    # purpose — a text-only probe cannot measure vision and must not pretend to.
    'extended': {
        'guard': ('GUARD', 'guard', None),
        'mock': ('MOCK', 'mock', None),
        'review': ('REVIEW', 'review', None),
        'spec_docs': ('SPEC-DOCS', 'spec_docs', None),
        'mechanical': ('MECHANICAL', 'mechanical', None),
        'multilingual': ('MULTILINGUAL', 'multilingual', None),
    },
}


def load(name):
    with open(os.path.join(DATA_DIR, f'{name}.jsonl'), encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--results', required=True)
    ap.add_argument('--battery', required=True, choices=sorted(BATTERIES))
    ap.add_argument('--date', default=datetime.date.today().isoformat())
    ap.add_argument('--commit', action='store_true')
    ap.add_argument('--note', default='n=1 small-probe, NOT large-bench')
    args = ap.parse_args()

    spec = BATTERIES[args.battery]
    results = json.load(open(os.path.expanduser(args.results), encoding='utf-8'))
    bench_path = os.path.join(DATA_DIR, 'benchmarks.jsonl')
    existing = load('benchmarks')

    def key_of(src):
        """Semantic identity of a benchmark row: the source KEY before the colon.
        The descriptive tail after ':' is provenance prose and must not defeat
        idempotency — deduping on the full string let a reworded suffix re-stamp
        the same evidence twice (caught in dry run before it reached the table)."""
        return str(src or '').split(':')[0].strip()

    have = {(r['model'], r['category'], key_of(r.get('source'))) for r in existing}

    new_rows, skipped, errors = [], 0, []
    for row in results:
        model = row.get('model')
        if not model:
            continue
        if row.get('error'):
            errors.append({'model': model, 'error': row['error']})
            continue
        for field, (key, category, maxpts) in spec.items():
            v = row.get(field)
            if v is None:
                continue
            if isinstance(v, dict):                      # agentic shape
                passed, total = v.get('passed'), v.get('total')
                damped = round(0.85 * passed / total, 3) if total else None
                detail = f'{passed}/{total}'
            else:                                        # v5 shape
                damped = round(0.85 * v / maxpts, 3)
                detail = f'{v}/{maxpts}'
            if damped is None:
                continue
            source = (f'live-probe-{args.date}/{key}: {args.battery} deterministic battery '
                      f'({field} {detail}, {args.note})')
            if (model, category, key_of(source)) in have:
                skipped += 1
                continue
            have.add((model, category, key_of(source)))
            new_rows.append({'model': model, 'category': category, 'score': damped,
                             'max_score': 1.0, 'source': source, 'valid_from': args.date})

    print(f'battery={args.battery}  results={args.results}')
    print(f'rows to add: {len(new_rows)}   already present (skipped): {skipped}')
    for r in new_rows:
        print(f"   {r['model']:18s} {r['category']:12s} {r['score']}")
    for e in errors:
        print(f"   ERROR {e['model']}: {e['error'][:70]}")
    if new_rows:
        print('NOTE: BENCH_OVERLAY must contain each key or the rows are INERT.'
              f' Keys used: {sorted({key_of(r["source"]) for r in new_rows})}')
    if not args.commit:
        print('\nDRY RUN — nothing written.')
        return 0
    if not new_rows:
        print('nothing to write')
        return 0

    shutil.copy(bench_path, f'/tmp/benchmarks.jsonl.bak-{time.strftime("%Y%m%d-%H%M%S")}')
    with open(bench_path, 'a', encoding='utf-8') as fh:
        for r in new_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    total = sum(1 for l in open(bench_path, encoding='utf-8') if l.strip())
    print(f'\nwrote {len(new_rows)} rows; benchmarks.jsonl now {total} rows. Re-seed next.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
