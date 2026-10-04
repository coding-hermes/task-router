#!/usr/bin/env python3
"""TR-295: measured cost per PASSED task — an auditable offline-first A/B harness.

Ranks provider/model lanes by (attempt cost from real metered tokens x that
lane's verified price) / (independent acceptance-check success rate). The
owner's target, not per-token price: cheap-but-failing lanes cannot win.

Design constraints (from the board row):
- DEFAULT IS DRY-RUN: a deterministic frozen fixture, a fake executor, a
  verifier that runs each task's explicit acceptance command. No model/provider
  API call, no production ledger write.
- `--execute` is the ONLY way a real run happens; it demands >= 4 explicit
  --lane pairs and refuses until a meter-reading executor is configured. It is
  NOT run by the worker; it waits for foreman review.
- Every attempt persists: stable task id, attempt id, requested pair, actual
  served pair (from the executor/meter, never the request), billing base URL,
  raw category levels, exact complexity_sig, versioned complexity_band
  (band_key() from router_outcomes — TR-289's key, imported not reimplemented),
  tokens in / cache-read / out, API calls, wall time, provider price source,
  calculated attempt cost, acceptance check command/result, pass/fail.
- Pass/fail comes ONLY from executing the task's acceptance-check command (or
  an injected verifier in tests). Self-reported success is never accepted.
- A requested-vs-served model mismatch (the measured trap: a request for
  openai/gpt-6-luna @ xkiro silently served z-ai/glm-5.3-flash) is FLAGGED and
  the sample is EXCLUDED from the requested lane's bucket — a GLM run is never
  counted as Luna.
- Missing cost is never 0: a lane missing tokens or a verified price is
  unmeasured with the reason.
- Sample floor governs the ranking; below the floor a lane is unmeasured with
  the reason (same rule as the resolve path: unknown is not cheap).
- The GLM cost anomaly (z-ai/glm-5.3-flash at $1,344.97/task derived) is
  excluded unless a price file supplies a verified basis naming its source;
  the exclusion is recorded with evidence, never silent.
- Output goes ONLY to explicitly chosen scratch paths. Production outcome
  paths are REFUSED. Attempt rows are written via router_outcomes.append_rows
  (dedupe key source_system/session_id/model) with provider/model/band
  identity retained so TR-299 can ingest them. This runner does NOT update
  the live rolling averages — that is the TR-299/TR-289 integration.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from router_outcomes import band_key, append_rows, complexity_sig  # noqa: E402

BAND_VERSION = 'b1'
SOURCE_SYSTEM = 'ab_harness'
DEFAULT_SAMPLE_FLOOR = 3

#: the anomaly from the board row: this lane's derived per-task cost contradicts
#: its sibling spelling by ~10^6. Excluded unless the price file supplies a
#: verified basis naming its source.
KNOWN_PRICE_ANOMALIES = {
    'z-ai/glm-5.3-flash':
        'board-derived $1344.97/task contradicts sibling glm-5.3-flash $0.0013/task '
        '(unit or pricing defect suspected)',
}

#: production paths the harness must never touch (refused as outputs).
FORBIDDEN_OUTPUT = (
    os.path.expanduser('~/task-router/data/state/outcomes.jsonl'),
    os.path.expanduser('~/task-router/data/state'),
)


def attempt_id(task_id, provider, model, run_id, seq):
    """Stable, replay-deterministic attempt identity."""
    h = hashlib.sha1(
        f'{task_id}|{provider}|{model}|{run_id}|{seq}'.encode()).hexdigest()[:16]
    return f'{task_id}-{provider}-{model}-{h}'


# --------------------------------------------------------------------------- #
# Frozen task set
# --------------------------------------------------------------------------- #

def frozen_tasks():
    """The SAME task set every lane executes. Each task carries an explicit
    acceptance check (a shell command reading the artifact the attempt left
    behind) and the raw category levels that drive the band key."""
    return [
        {'task_id': 'ab-fix-null-reason',
         'acceptance_cmd': 'grep -q "reason" {artifact}',
         'levels': {'mechanical': 1, 'code_gen': 1}},
        {'task_id': 'ab-regex-allowlist',
         'acceptance_cmd': 'python3 {artifact}',
         'levels': {'code_gen': 2, 'security': 1}},
        {'task_id': 'ab-tz-window-fix',
         'acceptance_cmd': 'grep -q "TZ" {artifact}',
         'levels': {'debug': 2, 'schema': 1}},
        {'task_id': 'ab-band-pooling-note',
         'acceptance_cmd': 'grep -q "pool" {artifact}',
         'levels': {'spec_docs': 1}},
    ]


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #

def fake_executor(provider, model, task, scratch_dir):
    """Deterministic offline stand-in for a real model call. Serves per-lane
    metrics AND the requested/served mismatch trap: the xkiro Luna lane serves
    GLM for a Luna request, so the exclusion path is exercisable offline.
    Artifact content satisfies the acceptance check iff the lane is in
    FIXTURE_PASS_LANES — the point is the verifier deciding, not the model."""
    lane = f'{provider}/{model}'
    served = {'provider': provider, 'model': model}
    if (provider, model) == ('xkiro', 'openai/gpt-6-luna'):
        served = {'provider': 'xkiro', 'model': 'z-ai/glm-5.3-flash'}
    artifact = os.path.join(scratch_dir, task['task_id'] + '.' +
                            hashlib.sha1(lane.encode()).hexdigest()[:8] + '.out')
    passes = lane in FIXTURE_PASS_LANES
    with open(artifact, 'w') as f:
        if passes:
            f.write(f'reason: fixture artifact for {task["task_id"]}\n'
                    f'TZ-independent window noted\n'
                    f'pool: fixture note\n')
            body = 'x = 1\n'   # exits 0 under python3
        if not passes:
            # a deliberately-failing artifact: no 'reason', no 'TZ', no 'pool'
            # tokens and a non-zero exit under python3, so every acceptance
            # command in the frozen set genuinely refuses it.
            f.write('INCOMPLETE attempt for task <redacted>\nstatus: NOT finished\n')
            body = 'raise SystemExit(1)\n'
    if task['acceptance_cmd'].startswith('python3'):
        with open(artifact, 'w') as f:
            f.write(body)
    return {
        'served_provider': served['provider'],
        'served_model': served['model'],
        'billing_base_url': 'https://fixture.invalid/v1',
        'tokens_in': 20000 + (len(task['task_id']) % 900),
        'tokens_cache_read': 1400000 * (len(task['task_id']) % 5),
        'tokens_out': 9000 + (len(task['task_id']) % 700),
        'api_calls': 1 + (len(task['task_id']) % 3),
        'wall_time_s': round(0.1 * (len(task['task_id']) % 11) + 0.2, 3),
        'self_reported_success': True,   # deliberately NOT trusted
        'artifact': artifact,
    }


#: deterministic fixture outcome per lane: these pass their acceptance checks.
FIXTURE_PASS_LANES = {'fixture/model-a', 'fixture/model-b'}


def live_executor(provider, model, task, scratch_dir):
    """Real-lane executor used only under --execute. Deliberately refuses until
    a meter-reading wrapper is configured: the requested --model flag is not
    evidence — served identity and tokens must come from state.db
    session_model_usage (billing_provider + billing_base_url + model)."""
    raise SystemExit(
        'live executor not configured: supply --executor-script (a wrapper that '
        'runs the task and reads served model + tokens from state.db '
        'session_model_usage, per the xkiro silent-reroute trap). No real A/B '
        'calls happen until the foreman-approved measured run.')


def run_acceptance_cmd(cmd_template, artifact):
    """The independent check. Exit code decides — the model's own words never do."""
    cmd = cmd_template.format(artifact=artifact)
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        detail = (f'rc={p.returncode} out={p.stdout.strip()[:120]} '
                  f'err={p.stderr.strip()[:120]}')
        return p.returncode == 0, detail
    except subprocess.TimeoutExpired:
        return False, 'acceptance cmd timeout 60s'


def default_verifier(task, ex, attempt_row):
    """Runs the task's acceptance command against the attempt's artifact."""
    return run_acceptance_cmd(task['acceptance_cmd'], ex.get('artifact') or '/nonexistent')


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #

def load_price_source(path):
    """{lane: {in_per_m, cache_read_per_m, out_per_m, source, basis}} from JSONL."""
    if not path:
        return {}
    out = {}
    with open(path) as f:
        for l in f:
            l = l.strip()
            if not l:
                continue
            r = json.loads(l)
            out[r['lane']] = r
    return out


def price_for_pair(provider, model, price_source):
    lane = f'{provider}/{model}'
    if lane in KNOWN_PRICE_ANOMALIES:
        # excluded unless the price file supplies a VERIFIED basis naming its source
        row = price_source.get(lane)
        if row and row.get('basis') and row.get('source'):
            return row
        return None
    row = price_source.get(lane)
    if row and 'in_per_m' in row and 'out_per_m' in row:
        return row
    return None


def attempt_cost(tokens, price):
    """(cost_usd or None, basis/reason). Missing tokens or price -> None with
    a reason; never 0."""
    if price is None:
        return None, 'no verified price source for lane'
    missing = [k for k in ('tokens_in', 'tokens_out')
               if not isinstance(tokens.get(k), int)]
    if missing:
        return None, 'missing token meter: ' + ','.join(missing)
    cost = (tokens['tokens_in'] * price['in_per_m']
            + tokens['tokens_out'] * price['out_per_m']) / 1e6
    cache = price.get('cache_read_per_m')
    if cache is not None and isinstance(tokens.get('tokens_cache_read'), int):
        cost += tokens['tokens_cache_read'] * cache / 1e6
    return round(cost, 8), f"price:{price.get('source')}"


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

def run_tasks(tasks, pairs, executor, price_source, run_id, scratch_dir,
              sample_floor=DEFAULT_SAMPLE_FLOOR, verifier=None):
    """Execute the frozen task set on every pair. Returns (attempts, lanes).

    attempts = the full persisted row set; lanes = per-lane aggregates with
    the ranking decision. The verifier is injectable for tests."""
    verifier = verifier or default_verifier
    attempts = []
    counts = {}
    for seq, task in enumerate(tasks, 1):
        for provider, model in pairs:
            aid = attempt_id(task['task_id'], provider, model, run_id, seq)
            t0 = time.monotonic()
            ex = executor(provider, model, task, scratch_dir)
            accepted, detail = verifier(task, ex, None)
            wall = round(time.monotonic() - t0 + ex.get('wall_time_s', 0.0), 3)
            levels = task['levels']
            row = {
                'source_system': SOURCE_SYSTEM,
                'session_id': aid,
                'task_id': task['task_id'],
                'attempt_id': aid,
                'run_id': run_id,
                'requested_provider': provider,
                'requested_model': model,
                'actual_provider': ex.get('served_provider'),
                'actual_model': ex.get('served_model'),
                'billing_base_url': ex.get('billing_base_url'),
                'complexity_levels': levels,
                'complexity_sig': complexity_sig(levels),
                'complexity_band': band_key(levels),
                'band_version': BAND_VERSION,
                'tokens_in': ex.get('tokens_in'),
                'tokens_cache_read': ex.get('tokens_cache_read'),
                'tokens_out': ex.get('tokens_out'),
                'api_calls': ex.get('api_calls'),
                'wall_time_s': wall,
                'acceptance_cmd': task['acceptance_cmd'],
                'acceptance_result': detail,
                'passed': bool(accepted),
            }
            mismatch = ((row['actual_provider'], row['actual_model'])
                        != (provider, model))
            row['requested_served_mismatch'] = mismatch
            lane = f'{provider}/{model}'
            c = counts.setdefault(lane, {
                'attempts': 0, 'passed': 0, 'excluded': 0,
                'cost_sum': 0.0, 'cost_attempts': 0,
                'missing_cost_reasons': [], 'mismatch_count': 0})
            c['attempts'] += 1
            if mismatch:
                c['excluded'] += 1
                c['mismatch_count'] += 1
                row['excluded'] = True
                row['exclusion_reason'] = (
                    f'requested {provider}/{model} but served '
                    f'{row["actual_provider"]}/{row["actual_model"]}; sample '
                    'excluded from requested lane bucket')
                row['cost_usd'] = None
                row['cost_basis'] = 'excluded: requested/served mismatch'
            else:
                row['excluded'] = False
                if accepted:
                    c['passed'] += 1
                price = price_for_pair(provider, model, price_source)
                cost, basis = attempt_cost(row, price)
                row['price_source'] = (price or {}).get('source')
                row['cost_usd'] = cost
                row['cost_basis'] = basis
                if cost is None:
                    c['missing_cost_reasons'].append(basis)
                else:
                    c['cost_sum'] += cost
                    c['cost_attempts'] += 1
            attempts.append(row)
    lanes = rank_lanes(counts, sample_floor)
    return attempts, lanes


def rank_lanes(counts, sample_floor):
    """$ per PASSED task = total measured cost / independently passed tasks.
    Both rates are named separately so a cheap-but-failing lane cannot look
    best. Lanes with zero passes, sub-floor usable samples, a missing cost
    basis or a price anomaly are unmeasured with the reason."""
    out = []
    for lane, c in sorted(counts.items()):
        usable = c['attempts'] - c['excluded']
        entry = {
            'lane': lane,
            'attempts': c['attempts'],
            'excluded_mismatch': c['excluded'],
            'usable_attempts': usable,
            'passed': c['passed'],
            'success_rate': round(c['passed'] / usable, 4) if usable else None,
            'cost_per_attempt': (round(c['cost_sum'] / c['cost_attempts'], 6)
                                 if c['cost_attempts'] else None),
            'cost_per_passed_task': (round(c['cost_sum'] / c['passed'], 6)
                                     if c['passed'] and c['cost_attempts'] else None),
            'sample_floor': sample_floor,
        }
        reasons = []
        if usable < sample_floor:
            reasons.append(f'below sample floor ({usable} usable < {sample_floor})')
        if c['passed'] == 0:
            reasons.append('zero independently passed tasks')
        if c['missing_cost_reasons']:
            reasons.append('missing cost basis: '
                           + '; '.join(sorted(set(c['missing_cost_reasons']))))
        if c['mismatch_count']:
            reasons.append(f'requested/served mismatch on {c["mismatch_count"]} '
                           'attempt(s); excluded from this lane')
        anomaly = KNOWN_PRICE_ANOMALIES.get(lane)
        if anomaly and not c['missing_cost_reasons'] and c['passed']:
            # measured only if the price file supplied a verified basis
            entry['known_price_anomaly'] = anomaly
        if reasons:
            entry['measured'] = False
            entry['unmeasured_reason'] = ' | '.join(reasons)
        else:
            entry['measured'] = True
        out.append(entry)
    out.sort(key=lambda e: (not e['measured'],
                            e['cost_per_passed_task'] if e['measured'] else 0.0))
    return out


# --------------------------------------------------------------------------- #
# Output guards
# --------------------------------------------------------------------------- #

def guard_output(path):
    p = os.path.abspath(os.path.expanduser(path or ''))
    forbidden = {os.path.abspath(os.path.expanduser(f)) for f in FORBIDDEN_OUTPUT}
    for f in forbidden:
        if p == f or p.startswith(f + os.sep):
            raise SystemExit(
                f'REFUSED: {path} is production state; the A/B harness writes '
                'only explicitly chosen scratch paths')
    return p


def write_report(path, lanes, run_id, sample_floor, mode):
    report = {
        'run_id': run_id,
        'harness': 'scripts/router_ab.py (TR-295)',
        'mode': mode,
        'band_version': BAND_VERSION,
        'sample_floor': sample_floor,
        'ranking_metric': ('cost_per_passed_task = total measured attempt cost / '
                           'independently passed tasks; cost_per_attempt and '
                           'success_rate reported separately'),
        'lanes': lanes,
        'note': ('attempt rows are ingestible by TR-299; this runner does NOT '
                 'update the live rolling averages (TR-299/TR-289 integration)'),
    }
    with open(path, 'w') as f:
        json.dump(report, f, indent=2)
        f.write('\n')
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='TR-295 A/B harness: rank lanes by measured $ per PASSED task')
    ap.add_argument('--execute', action='store_true',
                    help='run against REAL lanes (consumes quota); requires '
                         '>=4 --lane pairs AND --executor-script')
    ap.add_argument('--lane', action='append', default=[],
                    help='provider/model pair, repeatable (>=4 for --execute)')
    ap.add_argument('--executor-script',
                    help='path to the meter-reading live executor (required with --execute)')
    ap.add_argument('--price-source',
                    help='JSONL of per-lane verified prices '
                         '{lane,in_per_m,cache_read_per_m,out_per_m,source[,basis]}')
    ap.add_argument('--out', required=True,
                    help='explicit scratch JSONL path for attempt rows')
    ap.add_argument('--report', required=True,
                    help='explicit scratch JSON path for the ranking report')
    ap.add_argument('--run-id', default=time.strftime('ab-%Y%m%dT%H%M%S'))
    ap.add_argument('--sample-floor', type=int, default=DEFAULT_SAMPLE_FLOOR)
    ap.add_argument('--scratch-dir', default=None,
                    help='task artifact scratch dir (default: sibling of --out)')
    args = ap.parse_args(argv)

    pairs = []
    for l in args.lane:
        prov, model = l.split('/', 1)
        pairs.append((prov, model))
    if args.execute:
        if len(pairs) < 4:
            ap.error('--execute requires >= 4 explicit --lane provider/model pairs')
        if not args.executor_script:
            ap.error('--execute requires --executor-script (meter-reading wrapper)')

    out_path = guard_output(args.out)
    report_path = guard_output(args.report)
    scratch = args.scratch_dir or os.path.join(
        os.path.dirname(out_path) or '.', 'ab-artifacts')
    os.makedirs(scratch, exist_ok=True)
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    os.makedirs(os.path.dirname(report_path) or '.', exist_ok=True)

    if args.execute:
        executor = _load_live_executor(args.executor_script)
        mode = 'live (execute; metered)'
    else:
        executor = fake_executor
        mode = 'dry-run fixture (no model/provider API call)'

    price_source = load_price_source(args.price_source)
    tasks = frozen_tasks()
    if not pairs:
        pairs = [('fixture', 'model-a'), ('fixture', 'model-b'),
                 ('fixture', 'model-c'), ('xkiro', 'openai/gpt-6-luna')]

    attempts, lanes = run_tasks(tasks, pairs, executor, price_source,
                                args.run_id, scratch, args.sample_floor)

    n = append_rows(out_path, attempts)
    write_report(report_path, lanes, args.run_id, args.sample_floor, mode)
    print(json.dumps({'appended_rows': n, 'rows': out_path, 'report': report_path,
                      'mode': mode}, indent=2))
    return 0


def _load_live_executor(path):
    """Load a user-supplied executor module exposing run(provider, model, task,
    scratch_dir) with the same contract as fake_executor."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('ab_live_executor', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, 'run', None)
    if fn is None:
        raise SystemExit('executor script must define run(provider, model, task, scratch_dir)')
    return fn


if __name__ == '__main__':
    sys.exit(main())
