#!/usr/bin/env python3
"""router_validate.py — `router validate`: pre-flight integrity checks (TR-031).

Stdlib-only validation of the task-router's data + state plane, meant to run
before a calibration loop or after a reseed. Checks:

  a. registry  — registry.json exists (ROUTING_REGISTRY env override, repo
     default otherwise — same convention as router_spawn.py), parses as JSON,
     carries version=3 + a tables dict, and every tables.models row carries the
     schema fields router_spawn.py reads (provider, model, normalized_price,
     plan_tier, token_factor, data_class, disabled, archive).
  b. freshness — registry.json mtime vs data/tables/*.jsonl mtimes. A registry
     OLDER than any table is stale (re-run scripts/router_seed.py) — reported
     as an issue (warning-level detail, still counted).
  c. state     — circuit-state.json / health-state.json / quota-state.json and
     ledger.jsonl under ROUTER_STATE_DIR (default ~/.hermes/model-router) parse
     if present; corrupt = issue. Absent = ok (fresh installs have none).
  d. profiles  — data/tables/task_profiles.jsonl: required fields (id, title)
     present, no duplicate ids. task_profile_requirements.jsonl: task_id
     references a known profile, category non-empty, level an int in -5..+5.

Usage:
  router_validate.py [--json]

Output with --json is PURE machine-parseable JSON on stdout:
  {"valid": bool, "checks": [{"name", "ok", "detail"}, ...], "issues": [...]}
Exit 0 when valid, exit 1 when any issue is found. A missing registry.json is
reported as an issue (exit 1) with a detail pointing at router_seed.py — the
validator never fabricates or repairs state.

TR-108 self-heal (opt-in): `--heal` or ROUTER_VALIDATE_HEAL=1 makes the run
re-seed via scripts/router_seed.py BEFORE the verdict when the registry is
missing or genuinely stale (the two breaks the 2026-09-22 refresh-cron death
produced). The re-graded verdict then describes the post-heal tree and an
explicit `heal` check records what ran. Default OFF — without the flag the
run is read-only (the health plane runs these checks in-process and must not
spawn seed subprocesses).
"""
import argparse
import glob
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)

# Repo-relative default (worker 2026-09-21): with ROUTING_REGISTRY unset the
# validator must look at the SAME registry.json `router seed` writes and
# `router spawn` reads — <repo>/registry.json (identical defaults in
# router_seed.py:21 and router_spawn.py:117). The TR-057 data-home default
# via task_router.paths.registry_path() diverged from both: a bare
# `python3 scripts/router_validate.py` on a healthy repo checkout exited 1
# with "registry.exists: missing ~/.local/share/task-router/registry.json".
#
# 2026-09-19 (CI red, 5 consecutive runs from 625439e): the TR-057 data-home
# version imported task_router.paths at module level, which broke every FRESH
# CHECKOUT — the import only resolved where the package is installed (board
# venv editable install), so `python3 -m pytest` in CI died with
# ModuleNotFoundError: No module named 'task_router' and took 9 validate
# fixture tests with it. This default needs NO package import at all
# (stdlib-only, works from any cwd, installed or not).
#
# Installed-CLI use is unchanged: task_router.cli exports ROUTING_REGISTRY
# (data-home derived) before dispatching to this script, and the env override
# remains authoritative — bare run == repo checkout convention, CLI run ==
# data-home convention via env, exactly like seed/spawn.

REGISTRY = os.environ.get('ROUTING_REGISTRY', os.path.join(_REPO, 'registry.json'))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
STATE_DIR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))

REGISTRY_VERSION = 3
# TR-082: seed writes the tables and registry.json within the same second, so a
# strict `registry < newest_table` comparison made a correct first run report
# itself as stale ("0s newer") and exit 1. Treat a lag up to this many seconds
# as fresh; anything beyond it is a genuinely stale registry.
FRESHNESS_SLACK_S = 1.0
# TR-108: self-heal seed budget — sized like the suite's SEED_TIMEOUT (11s
# idle measured; this box saturates routinely). The heal is armed opt-in, so
# a hung seed surfaces as this timeout instead of blocking forever.
HEAL_TIMEOUT_S = 600
# Schema fields router_spawn.py's registry loader reads off every model row.
MODEL_SCHEMA_FIELDS = ('provider', 'model', 'normalized_price', 'plan_tier',
                       'token_factor', 'data_class', 'disabled', 'archive')
PROFILE_REQUIRED_FIELDS = ('id', 'title')
LEVEL_MIN, LEVEL_MAX = -5, 5
STATE_JSON_FILES = ('circuit-state.json', 'health-state.json', 'quota-state.json')
STATE_JSONL_FILES = ('ledger.jsonl',)


def _read_jsonl(path):
    """-> (rows, error). Missing file = ([], 'missing'); unparsable line = error."""
    if not os.path.exists(path):
        return [], 'missing'
    rows = []
    try:
        with open(path) as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception as e:
                    return rows, f'line {i}: {e}'
        return rows, None
    except Exception as e:
        return rows, str(e)


def _tables_content_equal(reg_tables, data_dir):
    """True when EVERY registry table matches its data/tables/*.jsonl mirror
    row-for-row. Direction matters: data/tables also holds seed-INPUT files
    the registry never exports (probe_*, plan_terms, quality_estimates, ...);
    only registry-owned tables decide staleness. Used ONLY as the
    big-mtime-lag tiebreak: content, not timestamps, is what 'stale' means.
    """
    if not isinstance(reg_tables, dict) or not reg_tables:
        return False
    for name, reg_rows in reg_tables.items():
        if not isinstance(reg_rows, list):
            return False
        path = os.path.join(data_dir, f'{name}.jsonl')
        if not os.path.exists(path):
            return False
        rows = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception:
            return False
        if rows != reg_rows:
            return False
    return True


def freshness_check(registry_path=None, data_dir=None):
    """The freshness verdict + the numbers behind it (one source of truth).

    Returns a dict; `ok` is the gate verdict and `detail` is the exact string
    the `freshness` check publishes. Callers that need the numbers (the health
    plane's `registry_age` block) must not re-derive the predicate from
    `lag_s` alone: the content tiebreak below is part of the definition, and a
    re-derivation that skips it reports `stale: true` on a checkout the gate
    calls valid (measured: 37855s mtime lag, byte-identical tables).

    Keys: ok, detail, registry, data_dir, tables, newest_table, lag_s,
    content_match, stale.
    """
    registry_path = registry_path or REGISTRY
    data_dir = data_dir or DATA_DIR
    table_files = sorted(glob.glob(os.path.join(data_dir, '*.jsonl')))
    out = {'ok': False, 'detail': '', 'registry': registry_path,
           'data_dir': data_dir, 'tables': len(table_files),
           'newest_table': None, 'lag_s': None, 'content_match': None,
           'stale': None}
    if not table_files:
        out['detail'] = f'no data tables found under {data_dir}'
        return out
    reg_m = os.path.getmtime(registry_path)
    newest_f = max(table_files, key=os.path.getmtime)
    newest_m = os.path.getmtime(newest_f)
    # TR-082: seed rewrites the tables and registry.json inside the SAME
    # second, so a raw float comparison let sub-second write ordering
    # decide — the fresh-install path reported its own successful write
    # as "stale (0s newer)" and exited 1. Compare with a slack window:
    # only a registry older than the newest table by MORE than
    # FRESHNESS_SLACK_S is genuinely stale.
    lag = newest_m - reg_m
    out['newest_table'] = os.path.basename(newest_f)
    out['lag_s'] = lag
    if lag > FRESHNESS_SLACK_S:
        # TR-082 follow-up (worker 2026-09-21): the seed writes
        # registry.json BEFORE syncing data/tables, so under load the
        # tables can land many seconds "newer" while carrying EXACTLY
        # the registry's content — mtime alone then reds a healthy
        # checkout (measured: 5s lag on the current tree). Before
        # flagging stale, compare CONTENT: identical row-for-row
        # tables = seed write-ordering artifact, not staleness.
        reg_doc = None
        try:
            with open(registry_path) as f:
                reg_doc = json.load(f)
        except Exception:
            reg_doc = None
        reg_tables = reg_doc.get('tables') if isinstance(reg_doc, dict) else None
        out['content_match'] = bool(
            isinstance(reg_tables, dict)
            and _tables_content_equal(reg_tables, data_dir))
        if out['content_match']:
            out.update(ok=True, stale=False,
                       detail=f'registry.json content matches all {len(table_files)} data '
                              f'tables ({out["newest_table"]} mtime is {lag:.0f}s '
                              f'newer — seed write ordering, not staleness)')
        else:
            out.update(ok=False, stale=True,
                       detail=f'stale registry (warning-level): {out["newest_table"]} is '
                              f'{lag:.0f}s newer than registry.json — re-run '
                              f'scripts/router_seed.py')
    else:
        out.update(ok=True, stale=False,
                   detail=f'registry.json is at least as new as all {len(table_files)} data tables '
                          f'(tolerance {FRESHNESS_SLACK_S:.0f}s)')
    return out


# TR-108 (worker 2026-09-24): the freshness gate had no self-heal. The daily
# refresh cron died mid-run on 2026-09-22 and left probe_gaps.jsonl newer than
# registry.json, so every later `router validate` exited 1 until a human re-ran
# the seed. This heal is strictly OPT-IN: ROUTER_VALIDATE_HEAL=1 (or --heal).
# Default OFF on purpose — the health plane (router_health.py) runs these same
# checks IN-PROCESS on every /health request, and a read-only monitor must
# never spawn seed subprocesses (writes data/tables, takes seconds, duckdb).
HEAL_ENV = 'ROUTER_VALIDATE_HEAL'
HEAL_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                           'router_seed.py')


def heal_is_armed():
    """The heal runs only when the operator (or the cron RESUME block) asks."""
    return bool(os.environ.get(HEAL_ENV)) or '--heal' in sys.argv


def heal_registry(registry_path=None, data_dir=None):
    """Self-heal the data plane: re-seed registry.json from data/tables.

    Called AFTER run_checks() graded the tree and BEFORE the verdict is
    printed, so the report describes the tree the caller actually gets. Seed
    is deterministic and idempotent (full rebuild from the committed tables —
    same reason the refresh-resume plan calls re-running it safe), so re-running
    it converges: a fresh clone gets its gitignored registry.json written, a
    stale-registry tree gets tables+registry rewritten in one second. Any
    failure is fail-open: the heal detail is reported and the ORIGINAL issue
    list stands untouched — a broken heal must never mask the diagnosis.
    """
    registry_path = registry_path or REGISTRY
    data_dir = data_dir or DATA_DIR
    pre_ok = os.path.exists(registry_path)
    proc = subprocess.run(
        [sys.executable, HEAL_SCRIPT], capture_output=True, text=True,
        timeout=HEAL_TIMEOUT_S,
        env=dict(os.environ, ROUTING_REGISTRY=registry_path,
                 ROUTING_DATA_DIR=data_dir))
    if proc.returncode != 0:
        return {'ok': False,
                'detail': f'self-heal seed failed (rc={proc.returncode}): '
                          f'{(proc.stderr or proc.stdout or "")[-300:]}'}
    ok = os.path.exists(registry_path)
    return {'ok': ok,
            'detail': ('seed completed; registry now present'
                       if ok else
                       'seed exited 0 but registry.json still absent'),
            'seed_ran': True, 'registry_preexisting': pre_ok}


def _heal_needed(report):
    """True when the graded report describes a healable data-plane break.

    Exactly the two conditions the refresh-cron incident produced: the
    gitignored registry.json is MISSING (fresh clone / wiped tree), or the
    freshness check went stale (cron died between the table writes and the
    registry write). Anything else — corrupt state files, profile problems —
    is not seed-repairable and must not trigger a seed run.
    """
    if any('registry.exists' in i for i in report.get('issues') or []):
        return True
    fresh = next((c for c in report.get('checks') or []
                  if c.get('name') == 'freshness'), None)
    return bool(fresh and not fresh.get('ok'))


def run_heal():
    """heal_registry() with every failure mode folded into the result dict.

    Fail-open: a timeout, a missing seed script, duckdb unavailable on a bare
    interpreter — all come back as {'ok': False, 'detail': ...} instead of an
    exception, so the validate run itself always completes and reports.
    """
    try:
        return heal_registry()
    except subprocess.TimeoutExpired:
        return {'ok': False,
                'detail': f'self-heal seed timed out after {HEAL_TIMEOUT_S}s'}
    except Exception as exc:  # noqa: BLE001 — a broken heal never breaks validate
        return {'ok': False, 'detail': f'self-heal seed could not run: {exc}'}


def run_checks():
    checks, issues = [], []

    def add(name, ok, detail):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            issues.append(f'{name}: {detail}')

    # ---- a. registry: exists / parses / version + schema ---------------------
    reg = None
    if not os.path.exists(REGISTRY):
        add('registry.exists', False,
            f'missing: {REGISTRY} — run scripts/router_seed.py once to generate it')
    else:
        add('registry.exists', True, REGISTRY)
        try:
            with open(REGISTRY) as f:
                reg = json.load(f)
            add('registry.parse', True, 'valid JSON')
        except Exception as e:
            add('registry.parse', False, f'corrupt JSON: {e}')
    if isinstance(reg, dict):
        v = reg.get('version')
        if not isinstance(v, int) or isinstance(v, bool):
            add('registry.version', False, f'version missing or not an int: {v!r}')
        elif v != REGISTRY_VERSION:
            add('registry.version', False,
                f'version={v} — spawn/seed are built against version {REGISTRY_VERSION}')
        else:
            add('registry.version', True, f'version={v}')
        tables = reg.get('tables')
        if not isinstance(tables, dict):
            add('registry.schema', False,
                f'"tables" is {type(tables).__name__}, expected object of table lists')
        else:
            add('registry.schema', True, f'{len(tables)} tables: {", ".join(sorted(tables))}')
            models = tables.get('models')
            if not isinstance(models, list) or not models:
                add('registry.models_schema', False, 'tables.models missing or empty')
            else:
                missing = {}
                for i, row in enumerate(models):
                    if not isinstance(row, dict):
                        missing.setdefault('<row not an object>', []).append(i)
                        continue
                    for field in MODEL_SCHEMA_FIELDS:
                        if field not in row:
                            missing.setdefault(field, []).append(i)
                if missing:
                    det = '; '.join(f"'{k}' absent on {len(v)} row(s) (first idx {v[0]})"
                                    for k, v in sorted(missing.items()))
                    add('registry.models_schema', False, det)
                else:
                    add('registry.models_schema', True,
                        f'{len(models)} model rows carry the spawn schema fields '
                        f'({", ".join(MODEL_SCHEMA_FIELDS)})')

    # ---- b. freshness: registry vs data/tables -------------------------------
    if os.path.exists(REGISTRY):
        fresh = freshness_check(REGISTRY, DATA_DIR)
        if fresh['lag_s'] is None:
            add('freshness', False, fresh['detail'])
        else:
            add('freshness', fresh['ok'], fresh['detail'])
            # TR-082: name BOTH resolved paths so a future reader can see exactly
            # what was compared instead of guessing at the layout.
            add('freshness.paths', True,
                f'compared registry={REGISTRY} against tables={DATA_DIR}')

    # ---- c. state files: parse-if-present ------------------------------------
    for fname in STATE_JSON_FILES:
        path = os.path.join(STATE_DIR, fname)
        if not os.path.exists(path):
            add(f'state.{fname}', True, 'not present (ok — fresh state dir)')
            continue
        try:
            with open(path) as f:
                json.load(f)
            add(f'state.{fname}', True, 'parses as JSON')
        except Exception as e:
            add(f'state.{fname}', False, f'corrupt: {path}: {e}')
    for fname in STATE_JSONL_FILES:
        path = os.path.join(STATE_DIR, fname)
        if not os.path.exists(path):
            add(f'state.{fname}', True, 'not present (ok — fresh state dir)')
            continue
        rows, err = _read_jsonl(path)
        if err and err != 'missing':
            add(f'state.{fname}', False, f'corrupt: {path}: {err}')
        else:
            add(f'state.{fname}', True, f'{len(rows)} rows parse as JSONL')

    # ---- d. profile integrity -------------------------------------------------
    profiles, perr = _read_jsonl(os.path.join(DATA_DIR, 'task_profiles.jsonl'))
    if perr:
        add('profiles.table', False,
            f'task_profiles.jsonl unreadable: {perr}' if perr != 'missing'
            else f'task_profiles.jsonl missing under {DATA_DIR}')
        profile_ids = set()
    else:
        problems = []
        seen = set()
        for i, row in enumerate(profiles):
            if not isinstance(row, dict):
                problems.append(f'row {i} is not an object')
                continue
            for field in PROFILE_REQUIRED_FIELDS:
                if not row.get(field):
                    problems.append(f'row {i} missing required field {field!r}')
            pid = row.get('id')
            if pid:
                if pid in seen:
                    problems.append(f'duplicate profile id {pid!r}')
                seen.add(pid)
        profile_ids = seen
        if problems:
            add('profiles.table', False, '; '.join(problems[:5]) +
                (f' (+{len(problems) - 5} more)' if len(problems) > 5 else ''))
        else:
            add('profiles.table', True,
                f'{len(profiles)} profiles, ids unique, required fields present')

    reqs, rerr = _read_jsonl(os.path.join(DATA_DIR, 'task_profile_requirements.jsonl'))
    if rerr:
        add('profiles.requirements', False,
            f'task_profile_requirements.jsonl unreadable: {rerr}' if rerr != 'missing'
            else f'task_profile_requirements.jsonl missing under {DATA_DIR}')
    else:
        problems = []
        for i, row in enumerate(reqs):
            if not isinstance(row, dict):
                problems.append(f'row {i} is not an object')
                continue
            tid = row.get('task_id')
            if not tid:
                problems.append(f'row {i} missing task_id')
            elif profile_ids and tid not in profile_ids:
                problems.append(f'row {i} references unknown profile {tid!r}')
            if not row.get('category'):
                problems.append(f'row {i} missing category')
            lvl = row.get('level')
            if not isinstance(lvl, int) or isinstance(lvl, bool):
                problems.append(f'row {i} ({tid}) level not an int: {lvl!r}')
            elif not (LEVEL_MIN <= lvl <= LEVEL_MAX):
                problems.append(f'row {i} ({tid}) level {lvl} outside '
                                f'{LEVEL_MIN}..+{LEVEL_MAX}')
        if problems:
            add('profiles.requirements', False, '; '.join(problems[:5]) +
                (f' (+{len(problems) - 5} more)' if len(problems) > 5 else ''))
        else:
            add('profiles.requirements', True,
                f'{len(reqs)} requirement rows: levels within {LEVEL_MIN}..+{LEVEL_MAX}, '
                f'all task_ids resolve')

    return checks, issues


def run_checks_dict():
    """The check envelope as one dict: {'valid', 'checks', 'issues'}.

    Single source of truth for the report shape. `main()` prints this, and
    consumers that cannot run the CLI as a subprocess (the HTTP health plane in
    router_health.py) read the SAME dict — so a check added here is a check
    everywhere, and the gate verdict on /health can never drift from the
    `router validate` verdict an operator sees.
    """
    checks, issues = run_checks()
    return {'valid': not issues, 'checks': checks, 'issues': issues}


def main():
    ap = argparse.ArgumentParser(
        description='router validate — registry/state/profile integrity checks (stdlib only)')
    ap.add_argument('--json', action='store_true',
                    help='emit pure machine-parseable JSON on stdout')
    ap.add_argument('--heal', action='store_true',
                    help='TR-108 self-heal: when the registry is missing or stale, '
                         're-run scripts/router_seed.py before the verdict (also '
                         f'armed by {HEAL_ENV}=1). Default OFF: the check run stays '
                         'read-only.')
    args = ap.parse_args()

    report = run_checks_dict()
    checks, issues, valid = report['checks'], report['issues'], report['valid']

    heal_result = None
    if heal_is_armed() and _heal_needed(report):
        heal_result = run_heal()
        if heal_result.get('ok'):
            # Grade the tree the caller actually gets: a healed tree must exit
            # 0, a failed heal must surface the ORIGINAL issues untouched.
            report = run_checks_dict()
            checks, issues, valid = (report['checks'], report['issues'],
                                     report['valid'])
        checks.append({'name': 'heal', 'ok': bool(heal_result.get('ok')),
                       'detail': heal_result.get('detail', '')})
        if not heal_result.get('ok'):
            issues.append(f"heal: {heal_result.get('detail', '')}")

    if args.json:
        print(json.dumps(report))
    else:
        for c in checks:
            mark = 'ok  ' if c['ok'] else 'FAIL'
            print(f"[{mark}] {c['name']}: {c['detail']}")
        print('')
        if valid:
            print(f'valid: all {len(checks)} checks passed')
        else:
            print(f'INVALID — {len(issues)} issue(s):')
            for i in issues:
                print(f'  - {i}')
    return 0 if valid else 1


if __name__ == '__main__':
    sys.exit(main())
