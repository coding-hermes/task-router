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
"""
import argparse
import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
REGISTRY = os.environ.get('ROUTING_REGISTRY', os.path.join(_REPO, 'registry.json'))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
STATE_DIR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))

REGISTRY_VERSION = 3
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
        table_files = sorted(glob.glob(os.path.join(DATA_DIR, '*.jsonl')))
        if not table_files:
            add('freshness', False, f'no data tables found under {DATA_DIR}')
        else:
            reg_m = os.path.getmtime(REGISTRY)
            newest_f = max(table_files, key=os.path.getmtime)
            newest_m = os.path.getmtime(newest_f)
            if reg_m < newest_m:
                add('freshness', False,
                    f'stale registry (warning-level): {os.path.basename(newest_f)} is '
                    f'{newest_m - reg_m:.0f}s newer than registry.json — re-run '
                    f'scripts/router_seed.py')
            else:
                add('freshness', True,
                    f'registry.json is at least as new as all {len(table_files)} data tables')

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


def main():
    ap = argparse.ArgumentParser(
        description='router validate — registry/state/profile integrity checks (stdlib only)')
    ap.add_argument('--json', action='store_true',
                    help='emit pure machine-parseable JSON on stdout')
    args = ap.parse_args()

    checks, issues = run_checks()
    valid = not issues
    report = {'valid': valid, 'checks': checks, 'issues': issues}

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
