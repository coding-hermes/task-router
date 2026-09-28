#!/usr/bin/env python3
"""router_probe_ingest.py — battery results -> benchmarks rows (the rank inputs).

The missing middle of the pipeline: a battery produces scores, the registry
consumes benchmark rows, and nothing connected the two by hand without risking
the defects already paid for once:

  - ONE source key per measured category. BENCH_OVERLAY applies a source's whole
    category list to every row carrying it, so a multi-task battery under a
    multi-category key credits each category with its best task (muse code_gen
    read 0.85 from a 2/3 code task until the keys were split — commit 67d553c).
  - probes are stored UNDAMPED: score = checks_passed / checks_total,
    max_score 1.0, n=1, never family-filled (the 09-16/09-25 onboarding shape).
    TR-232: the previous score = 0.85 * ratio cap (small-n humility) collided
    with the rank scale — third-party and estimate rows are stored as their raw
    fraction and can reach 1.0, so the seed's per-category quantiles bucketed a
    MIXED pool where a model that aced our probe 4/4 (0.85) could never outrank
    a 0.9 third-party benchmark row. The units have to agree at the source;
    humility about n=1 lives in the source string ('n=1 small-probe, NOT
    large-bench') and in the rank audit's provenance view, not in a unit shift.
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
# max points None => the results file already carries an undamped score.
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
    # v2 of the same six, built to DISCRIMINATE rather than to pass. MEASURED across a
    # strong/mid/weak spread (2026-09-27, seven lanes from gpt-6-sol down to
    # cohere/command-r7b): guard separated the field 0/4..4/4 and review 2/4..4/4, so
    # those two keys exist. mechanical, multilingual, spec_docs and mock STILL scored
    # 4/4 on every lane, so their keys are deliberately ABSENT — an unregistered key is
    # inert by construction, which is the only honest way to hold a probe that has not
    # shown it can tell two models apart. Note the design lesson: v1's guard tested one
    # direction (refuse), so every lane passed; v2 requires refusing two harmful asks
    # AND still answering two benign ones, and the weak lanes fail by OVER-refusing.
    'extended2': {
        'guard': ('GUARD-V2', 'guard', None),
        'review': ('REVIEW-V2', 'review', None),
    },
    # v3: the four that STILL saturated in v2. MEASURED 2026-09-27 over the same
    # seven-lane spread — mechanical spread 0.50, multilingual 0.33, spec_docs 0.33,
    # mock 0.33, every lane landing between 3/6 and 6/6 — so the fix was the task shape
    # (six precision-weighted checks, partial credit) rather than the wording. All four
    # keys exist now; with v2's guard/review that is all six previously-blocked categories.
    'extended3': {
        'mechanical': ('MECHANICAL-V3', 'mechanical', None),
        'multilingual': ('MULTILINGUAL-V3', 'multilingual', None),
        'spec_docs': ('SPEC-DOCS-V3', 'spec_docs', None),
        'mock': ('MOCK-V3', 'mock', None),
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

    # A typed failure is EVIDENCE only for some classes. `not_served` (the row names an id
    # the provider does not serve), `not_in_plan` (the model exists but our plan excludes
    # it) and `endpoint_unsupported` (that id is not served on any endpoint shape here) are
    # facts that belong in probe_gaps, where the rank audit reads them to tell "unranked
    # because nobody measured it" from "unranked because we CANNOT". Transient classes
    # (rate_limited, http_520/524, transport) are not filed — they say nothing about the
    # lane, and filing them would bury the real evidence.
    EVIDENCE_CLASSES = {'not_served', 'not_in_plan', 'endpoint_unsupported'}

    new_rows, skipped, errors, empty = [], 0, [], []
    for row in results:
        model = row.get('model')
        if not model:
            continue
        if row.get('error'):
            errors.append({'provider': row.get('provider'), 'model': model,
                           'error': row['error'], 'error_class': row.get('error_class')})
            continue
        for field, (key, category, maxpts) in spec.items():
            v = row.get(field)
            if v is None:
                continue
            if isinstance(v, dict):                      # agentic shape
                if v.get('empty'):
                    # Nothing came back: file it as a GAP, never as a 0.0 score.
                    # A zero would drop the lane to the tier floor, which is how
                    # ten paid neuralwatt lanes ended up ranked as if they had
                    # failed everything they were asked.
                    empty.append({'model': model, 'category': category, 'field': field})
                    continue
                passed, total = v.get('passed'), v.get('total')
                score = round(passed / total, 3) if total else None
                detail = f'{passed}/{total}'
            else:                                        # v5 shape
                if row.get(field + '_empty'):
                    empty.append({'model': model, 'category': category, 'field': field})
                    continue
                score = round(v / maxpts, 3)
                detail = f'{v}/{maxpts}'
            if score is None:
                continue
            source = (f'live-probe-{args.date}/{key}: {args.battery} deterministic battery '
                      f'({field} {detail}, {args.note})')
            if (model, category, key_of(source)) in have:
                skipped += 1
                continue
            have.add((model, category, key_of(source)))
            new_rows.append({'model': model, 'category': category, 'score': score,
                             'max_score': 1.0, 'source': source, 'valid_from': args.date})

    print(f'battery={args.battery}  results={args.results}')
    print(f'rows to add: {len(new_rows)}   already present (skipped): {skipped}')
    for r in new_rows:
        print(f"   {r['model']:18s} {r['category']:12s} {r['score']}")
    for e in errors:
        print(f"   ERROR {e['model']}: {e['error'][:70]}")
    if empty:
        print(f'   EMPTY RESPONSES filed as gaps (NOT scored 0.0): {len(empty)}')
        for e in empty[:8]:
            print(f"      {e['model'][:26]:26s} {e['category']}")
    if new_rows:
        print('NOTE: BENCH_OVERLAY must contain each key or the rows are INERT.'
              f' Keys used: {sorted({key_of(r["source"]) for r in new_rows})}')

    # File the evidence-class failures as gaps (deduped), so the rank audit can annotate
    # the blocked families with a REASON instead of calling them ranking work.
    gap_path = os.path.join(DATA_DIR, 'probe_gaps.jsonl')
    seen_gaps = {(g.get('provider'), g.get('model'), key_of(g.get('error'))) for g in load('probe_gaps')}
    gap_rows = []
    for e in errors:
        if e.get('error_class') not in EVIDENCE_CLASSES:
            continue
        text = f"[{e['error_class']}] {e['error']}"
        ident = (e.get('provider'), e['model'], key_of(text))
        if ident in seen_gaps:
            continue
        seen_gaps.add(ident)
        gap_rows.append({'provider': e.get('provider'), 'model': e['model'], 'error': text})
    if gap_rows:
        print(f'   typed failures filed as EVIDENCE gaps: {len(gap_rows)}')
        for g in gap_rows[:8]:
            print(f"      {g['provider']}/{g['model']}: {g['error'][:70]}")
    transient = [e for e in errors if e.get('error_class') not in EVIDENCE_CLASSES]
    if transient:
        print(f'   transient failures NOT filed (they say nothing about the lane): '
              f'{sorted({e.get("error_class") for e in transient})}')

    if not args.commit:
        print('\nDRY RUN — nothing written.')
        return 0
    if not new_rows and not gap_rows:
        print('nothing to write')
        return 0

    shutil.copy(bench_path, f'/tmp/benchmarks.jsonl.bak-{time.strftime("%Y%m%d-%H%M%S")}')
    with open(bench_path, 'a', encoding='utf-8') as fh:
        for r in new_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    total = sum(1 for l in open(bench_path, encoding='utf-8') if l.strip())
    print(f'\nwrote {len(new_rows)} rows; benchmarks.jsonl now {total} rows. Re-seed next.')
    if gap_rows:
        with open(gap_path, 'a', encoding='utf-8') as fh:
            for g in gap_rows:
                fh.write(json.dumps(g, ensure_ascii=False) + '\n')
        gaps_total = sum(1 for l in open(gap_path, encoding='utf-8') if l.strip())
        print(f'wrote {len(gap_rows)} typed gaps; probe_gaps.jsonl now {gaps_total} rows.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
