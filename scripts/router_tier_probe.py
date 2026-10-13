#!/usr/bin/env python3
"""router_tier_probe.py — TR-221 ranking backfill probe harness.

THE GAP
-------
Unranked lanes read tier -1 in every category and are silently unroutable
(the muse class). 316 of 768 active/priced lanes carry no tier rows at all
and the blocking categories are thin exactly where the classifier asks
(mechanical 36, review 107, guard 146 lanes tiered). This harness is the
repeatable loop the board row asks for: SELECT who needs measuring, PROBE
via the existing battery, INGEST through the single-category-key path, and
STAMP a reason on every lane measurement cannot reach — so no tier gap on a
live lane is ever unexplained again (Bane: a NULL must carry a reason).

PIPELINE (all sanctioned channels, nothing hand-edited)
-------------------------------------------------------
  select   active + priced lanes from models.jsonl missing any --categories
           tier row (default: the 12 wave categories), minus probe_excludes
           and probe_gaps dead ids, minus providers that cannot be probed
           correctly (router_probe_run.SKIP_REASON: transport/OAuth skips).
  probe    shells out to scripts/router_probe_run.py (the existing battery:
           agentic agent_tick/delegation/schema/long_doc/long_horizon/test
           always run; --extended2 guard/review; --extended3 mechanical/
           multilingual/spec_docs/mock). Dead ids 404 and are filed by the
           ingest as typed not_served gaps — evidence, not silence.
  ingest   shells out to scripts/router_probe_ingest.py --commit. Every key
           is SINGLE-CATEGORY (the rank-fidelity rule from 67d553c: the
           BENCH_OVERLAY mapping is derived from the battery spec, so a
           score lands only where earned; never a multi-category key on a
           multi-task battery).
  reseed   NOT done here. benchmarks.jsonl is an input; the derived tables
           rebuild via scripts/router_seed.py (run it after this script —
           printed as the next step, never attempted in-process).
  stamp    lanes still unmeasured after the wave get a row in
           data/null_reasons.jsonl (the TR-197 sidecar, keyed
           (provider, model, field), FINITE vocabulary) via --stamp-reasons:
             unreachable-provider -> the provider has no resolvable
               credential/transport or the lane is a proven dead id
             no-sample-yet       -> reachable, not yet in a probe wave
           Idempotent: an existing (provider, model, field) row is skipped.

COST PER MODEL (documented, per AC)
-----------------------------------
  The default wave (--batteries agentic,extended2,extended3) is 12
  chat/completions calls per lane (6 agentic + 2 v2 + 4 v3), each
  max_tokens=3000, so the worst case is ~36K output tokens/lane; measured
  lanes typically emit ~10x less (deterministic short answers). Worst case
  ~= $0.11/lane at a $3/M output sticker, typical ~= $0.01-0.04/lane;
  :free lanes $0. The 404 class costs one ~0.1s call per dead lane.
  --dry-run costs nothing.

COVERAGE TABLE
--------------
  Every run (dry or real) prints the per-category BEFORE census (tier rows
  over active lanes) and, when --baseline-census is passed, diffs against a
  saved JSON census so a docs/ before/after table is one command:

    router_tier_probe.py --dry-run --save-census /tmp/census-before.json
    ... probe + ingest + reseed ...
    router_tier_probe.py --dry-run --baseline-census /tmp/census-before.json

CLI
  router_tier_probe.py [--dry-run] [--commit] [--providers p1,p2]
                       [--models id,id] [--categories c,c ...]
                       [--batteries agentic,extended2,extended3]
                       [--workers N] [--date YYYY-MM-DD] [--limit N]
                       [--tag S] [--stamp-reasons] [--reasons-only]
                       [--save-census P] [--baseline-census P]
  --dry-run and --commit are mutually exclusive; with neither the script
  behaves as a dry run and refuses to run batteries.
Exit 0 on success; exit 2 on usage errors. Never edits data/tables/*.jsonl
directly — only router_probe_ingest.py (benchmarks/probe_gaps appends) and
the null_reasons sidecar are written, both sanctioned channels.
"""
import argparse
import collections
import datetime
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPTS = os.path.join(REPO, 'scripts')
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(REPO, 'data', 'tables'))
sys.path.insert(0, SCRIPTS)
import router_probe_run as _run                     # noqa: E402  (battery + skip classes)

#: the 12 wave categories the default batteries measure — the blocking set
#: from the TR-221 row (mechanical, guard, agent_tick) plus the rest of the
#: agentic/extended probes shipped with them. (terminal is NOT wave-measured
#: by design: a text-only chat probe cannot measure a terminal session, so
#: terminal evidence arrives via Terminal-Bench rows in benchmarks.jsonl and
#: is overlaid by the seed — BENCH_OVERLAY 'Terminal-Bench'.)
WAVE_CATEGORIES = (
    'agent_tick', 'delegation', 'schema', 'long_doc', 'long_horizon', 'test',
    'guard', 'review',
    'mechanical', 'multilingual', 'spec_docs', 'mock',
)

#: battery name -> the router_probe_run.py flag that turns it on.
#: 'agentic' categories ride the base run (no flag, always measured), so it
#: has no flag here and its results file is written on every real run.
BATTERY_FLAGS = {'extended': '--extended', 'extended2': '--extended2',
                 'extended3': '--extended3'}

#: TR-197 null_reasons vocabulary — reasons this harness may stamp.
REASON_UNREACHABLE = 'unreachable-provider'
REASON_NO_SAMPLE = 'no-sample-yet'

NULL_REASONS_FILE = 'null_reasons.jsonl'
#: TABLE names (no extension) for load(); the .jsonl suffix is appended there.
PROBE_EXCLUDES_TABLE = 'probe_excludes'
PROBE_GAPS_TABLE = 'probe_gaps'
PROBE_PROVIDERS_TABLE = 'probe_providers'

#: typed failure classes that PROVE a lane cannot be measured (filed by
#: router_probe_ingest.py). Transient classes (rate_limited, http_520/524,
#: transport) are evidence about the provider's edge, not the lane, and
#: never exclude a lane.
GAP_EVIDENCE_CLASSES = ('not_served', 'not_in_plan', 'endpoint_unsupported',
                        'wrong_shape')


def load(name) -> list:
    """data/tables/<name>.jsonl -> [row]. Missing file -> []."""
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as fh:
        return [json.loads(l) for l in fh if l.strip()]


def lane_state(m) -> str:
    """The rank-audit lifecycle state of a models.jsonl row."""
    if m.get('archive'):
        return 'archived'
    if m.get('valid_to'):
        return 'retired'
    if m.get('disabled'):
        return 'disabled'
    if m.get('normalized_price') is None:
        return 'unpriced'
    return 'active'


def gap_dead_ids(gaps=None):
    """{(provider, model)} with a typed EVIDENCE gap: probed and PROVEN
    unmeasurable."""
    evidence = set(GAP_EVIDENCE_CLASSES)
    out = set()
    for g in (gaps if gaps is not None else load(PROBE_GAPS_TABLE)):
        text = str(g.get('error') or '')
        cls = text.split(']')[0].lstrip('[') if text.startswith('[') else ''
        if cls in evidence:
            out.add((g.get('provider'), g.get('model')))
    return out


def _resolvable_providers(prov_cfg=None, env=None, probe_data=None):
    """{provider: True} when a credential resolves (the battery's own
    resolve() order: cache-keyed subs -> config.yaml -> probe_providers).
    A provider that resolves to no base_url cannot be probed, so its lanes
    are excluded with a named reason instead of failing 401 mid-wave."""
    prov_cfg = prov_cfg if prov_cfg is not None else _run.load_providers()
    env = env if env is not None else _run.load_env()
    probe_data = probe_data if probe_data is not None else _run.load_probe_providers()
    out = {}
    provs = ({p for p, _ in (models_provider_pairs())}
             | set(prov_cfg) | {r.get('id') for r in load(PROBE_PROVIDERS_TABLE)})
    for prov in provs:
        if not prov:
            continue
        base, _key, extra = _run.resolve(prov, prov_cfg, env, probe_data)
        out[prov] = bool(base)
    return out


def models_provider_pairs() -> set:
    """Distinct (provider, model) pairs in models.jsonl (for provider enumeration)."""
    return {(m.get('provider'), m.get('model')) for m in load('models')}


def select_targets(categories, only_providers=None, only_models=None,
                   models=None, excludes=None, tiers=None, dead=None) -> tuple:
    """Lanes that NEED measurement, with a reason for every lane that does
    not qualify. Returns (targets, excluded) where targets is a sorted list
    of (provider, model) and excluded is {reason_class: [(provider, model)]}.

    A lane is a target when it is ACTIVE + PRICED and missing at least one
    --categories tier row. Exclusions are named, never silent: not-active
    lifecycle states, unpriced, providers the harness cannot probe correctly
    (SKIP_REASON transport class), already-excluded lanes
    (probe_excludes.jsonl) and typed dead ids (probe_gaps.jsonl).
    """
    models = models if models is not None else load('models')
    excludes = excludes if excludes is not None else {
        (r.get('provider'), r.get('model')) for r in load(PROBE_EXCLUDES_TABLE)}
    dead = dead if dead is not None else gap_dead_ids()
    if tiers is None:
        tiers = collections.defaultdict(set)
        for r in load('model_tier'):
            tiers[r['model']].add(r['category'])

    # Providers with no resolvable credential (config.yaml / .env /
    # probe_providers.jsonl / subscription cache) cannot be probed — named
    # as no-credentials so their lanes stamp unreachable-provider, not
    # no-sample-yet. Resolved once per provider, never per lane.
    provcred = _resolvable_providers()

    want = set(categories)
    only_models = set(only_models or [])
    targets, excluded = [], collections.defaultdict(list)

    def exclude(reason, prov, model) -> None:
        excluded[reason].append((prov, model))

    seen = set()
    for m in models:
        prov, model = m.get('provider'), m.get('model')
        if (prov, model) in seen:
            continue
        seen.add((prov, model))
        if only_providers and prov not in only_providers:
            continue
        if only_models and model not in only_models:
            continue
        state = lane_state(m)
        if state != 'active':
            exclude(state, prov, model)
            continue
        if prov in _run.SKIP_REASON:
            exclude('unprobeable-provider', prov, model)
            continue
        if not provcred.get(prov):
            exclude('no-credentials', prov, model)
            continue
        if (prov, model) in excludes:
            exclude('probe-excluded', prov, model)
            continue
        if (prov, model) in dead:
            exclude('dead-id', prov, model)
            continue
        missing = want - tiers.get(model, set())
        if not missing:
            exclude('fully-tiered', prov, model)
            continue
        targets.append((prov, model))
    return sorted(targets), excluded


def category_census(categories=None, models=None, tier_rows=None) -> dict:
    """{category: n_ACTIVE_LANES tiered} — the coverage table unit."""
    models = models if models is not None else load('models')
    active_models = {m.get('model') for m in models if lane_state(m) == 'active'}
    tiered = collections.defaultdict(set)
    for r in (tier_rows if tier_rows is not None else load('model_tier')):
        tiered[r['category']].add(r['model'])
    cats = categories or sorted(tiered)
    return {c: len(tiered.get(c, set()) & active_models) for c in cats}


def estimate_costs(models_rows, targets, calls_per_lane: int=12, max_tokens: int=3000) -> dict:
    """{provider: worst_case_usd} — calls x max_tokens at each lane's own
    published output sticker (public_out_per_m); $0 where none is published
    (:free / sticker-NULL lanes)."""
    price = {}
    for m in models_rows:
        out = m.get('public_out_per_m')
        if out is not None and m.get('model') not in price:
            price[m['model']] = float(out)
    costs = collections.defaultdict(float)
    for prov, model in targets:
        costs[prov] += calls_per_lane * max_tokens / 1e6 * price.get(model, 0.0)
    return {p: round(v, 4) for p, v in costs.items()}


def stamp_reasons(targets, excluded, commit: bool=False, data_dir=None, today=None):
    """Append data/null_reasons.jsonl rows for live lanes measurement cannot
    reach. Only ACTIVE lanes are stamped — archived/retired/disabled/unpriced
    rows are not live, so their tier null needs no reason (TR-197 scopes
    reasons to LIVE rows). Returns the rows (written or previewed).

    Classification, first match wins:
      unprobeable-provider / no-credentials / dead-id / probe-excluded ->
          unreachable-provider (structural: a probe cannot run)
      otherwise -> no-sample-yet (reachable, not yet in a wave)
    """
    data_dir = data_dir or DATA_DIR
    today = today or datetime.date.today().isoformat()
    dead_or_unprobeable = (set(excluded.get('unprobeable-provider', []))
                           | set(excluded.get('no-credentials', []))
                           | set(excluded.get('dead-id', []))
                           | set(excluded.get('probe-excluded', [])))
    rows = []
    for (prov, model) in targets:
        reason = (REASON_UNREACHABLE if (prov, model) in dead_or_unprobeable
                  else REASON_NO_SAMPLE)
        rows.append({'provider': prov, 'model': model, 'field': 'model_tier',
                     'reason': reason, 'ts': today,
                     'source': 'router_tier_probe TR-221 backfill wave'})

    path = os.path.join(data_dir, NULL_REASONS_FILE)
    have = set()
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            have.add((r.get('provider'), r.get('model'), r.get('field')))
    rows = [r for r in rows
            if (r['provider'], r['model'], r['field']) not in have]
    if commit and rows:
        with open(path, 'a', encoding='utf-8') as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    return rows


def run_batteries(args, targets):
    """Shell the existing battery (router_probe_run.py) for the selection,
    then ingest each results file through the single-category ingest path.
    Never reseeds — that is scripts/router_seed.py, run separately."""
    date = args.date
    tag = f'_{args.tag}' if args.tag else ''
    out_dir = os.path.expanduser(args.out_dir)
    env = dict(os.environ)
    env['ROUTING_DATA_DIR'] = DATA_DIR

    batteries = [b.strip() for b in args.batteries.split(',') if b.strip()]
    known = tuple(BATTERY_FLAGS) + ('agentic', 'v5')
    for b in batteries:
        if b not in known:
            raise SystemExit(f'unknown battery {b!r} (known: {", ".join(known)})')

    provs = args.providers or ','.join(sorted({p for p, _ in targets}))
    if not provs:
        print('nothing selected — nothing to probe')
        return 0
    cmd = [sys.executable, os.path.join(SCRIPTS, 'router_probe_run.py'),
           '--providers', provs,
           '--date', date, '--out-dir', out_dir, '--workers', str(args.workers)]
    for b in batteries:
        flag = BATTERY_FLAGS.get(b)
        if flag:
            cmd.append(flag)
    if args.limit:
        cmd += ['--limit', str(args.limit)]
    if args.models:
        cmd += ['--models', args.models]
    if tag:
        cmd += ['--tag', args.tag]
    print('PROBE:', ' '.join(cmd), flush=True)
    rc = subprocess.call(cmd, env=env, cwd=REPO)
    if rc != 0:
        return rc

    for b in batteries:
        results = os.path.join(out_dir, f'results_lanes_{date}{tag}_{b}.json')
        if not os.path.exists(results):
            print(f'ingest SKIP {b}: no results file {results}')
            continue
        icmd = [sys.executable, os.path.join(SCRIPTS, 'router_probe_ingest.py'),
                '--results', results, '--battery', b, '--date', date]
        if args.commit:
            icmd.append('--commit')
        print('INGEST:', ' '.join(icmd), flush=True)
        rc = subprocess.call(icmd, env=env, cwd=REPO)
        if rc != 0:
            return rc
    print('\nnext step: reseed the derived tables — '
          '~/.hermes/venvs/board/bin/python3 scripts/router_seed.py')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='TR-221 ranking backfill probe harness')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true',
                      help='print selection + coverage projection; write nothing')
    mode.add_argument('--commit', action='store_true',
                      help='run the battery + ingest for real (costs API calls)')
    ap.add_argument('--providers', default='',
                    help='comma list restricting the wave (default: all selectable)')
    ap.add_argument('--models', default='',
                    help='comma list of model ids to probe even if tiered elsewhere')
    ap.add_argument('--categories', default=','.join(WAVE_CATEGORIES),
                    help='categories a lane must be missing to be a target')
    ap.add_argument('--batteries', default='agentic,extended2,extended3',
                    help='battery set to run (default covers all 12 wave categories)')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--date', default=datetime.date.today().isoformat())
    ap.add_argument('--out-dir', default='~/model_bench')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--tag', default='')
    ap.add_argument('--stamp-reasons', action='store_true',
                    help='append data/null_reasons.jsonl rows for unmeasured live lanes')
    ap.add_argument('--reasons-only', action='store_true',
                    help='only report/stamp reasons; never run the battery')
    ap.add_argument('--save-census', default='',
                    help='write the before-census JSON to this path (dry-run safe)')
    ap.add_argument('--baseline-census', default='',
                    help='diff the current census against a saved one (after table)')
    args = ap.parse_args(argv)

    categories = [c.strip() for c in args.categories.split(',') if c.strip()]
    only_providers = {p.strip() for p in args.providers.split(',') if p.strip()} or None
    only_models = {m.strip() for m in args.models.split(',') if m.strip()} or None

    targets, excluded = select_targets(categories, only_providers, only_models)
    by_prov = collections.Counter(p for p, _ in targets)

    print(f'selection: {len(targets)} lanes need measurement')
    for prov, n in by_prov.most_common():
        print(f'   {prov:16s} {n}')
    for reason, pairs in sorted(excluded.items()):
        if pairs:
            print(f'   excluded {reason:22s} {len(pairs)}')

    print('\ncoverage BEFORE (active lanes tiered per category):')
    before = category_census(categories)
    for cat, n in before.items():
        print(f'   {cat:14s} {n}')

    if args.baseline_census:
        with open(os.path.expanduser(args.baseline_census), encoding='utf-8') as fh:
            base = json.load(fh)
        print(f'\ncoverage AFTER vs baseline ({os.path.basename(args.baseline_census)}):')
        print(f'   {"category":14s}{"before":>8}{"after":>8}{"delta":>8}')
        total_before = total_after = 0
        for cat in sorted(before):
            b = base.get('census', base).get(cat, 0)
            a = before[cat]
            total_before += b
            total_after += a
            print(f'   {cat:14s}{b:>8}{a:>8}{a - b:>+8}')
        print(f'   {"TOTAL":14s}{total_before:>8}{total_after:>8}'
              f'{total_after - total_before:>+8}')

    if args.save_census:
        path = os.path.expanduser(args.save_census)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'date': args.date, 'categories': categories,
                       'census': before}, fh, indent=1)
        print(f'census saved -> {path}')

    models_rows = load('models')
    est = estimate_costs(models_rows, targets)
    if est:
        print('\nestimated worst-case battery cost per provider (USD):')
        for prov, c in sorted(est.items()):
            print(f'   {prov:16s} ${c:.4f}')

    if args.stamp_reasons or args.reasons_only:
        rows = stamp_reasons(targets, excluded, commit=bool(args.stamp_reasons))
        print(f'\nreason stamps {"WRITTEN" if args.stamp_reasons else "preview"}: '
              f'{len(rows)}')
        for r in rows[:10]:
            print(f'   {r["provider"]:16s} {r["model"][:40]:40s} {r["reason"]}')
        if len(rows) > 10:
            print(f'   ... {len(rows) - 10} more')

    if args.reasons_only:
        return 0
    if args.commit:
        return run_batteries(args, targets)
    print('\nDRY RUN — nothing probed, nothing written.')
    print('next step for a real wave: rerun with --commit, then reseed via '
          'scripts/router_seed.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
