#!/usr/bin/env python3
"""TR-260: derive missing fleet lanes and append them to data/tables/projects.jsonl.

data/tables/*.jsonl are GENERATED (repo rule): this script is the sanctioned
seed-path way to add lane rows. It reads LIVE fleet state (scheduler DB +
fleet.toml) and the current projects table, appends one row per enabled lane
that the registry lacks, and lets scripts/router_seed.py regenerate
registry.json + every export from the result.

Why derived, not hand-copied: the 2026-10-01/02 <project>-foreman rename left
the registry topology behind (66 ROUTER-MISS lanes — TR-260). The next lane
wave will leave a different set behind; re-running this script closes that gap
from live data without editing tables by hand.

Row policy (matches the table's own 590-row conventions, TR-125 rule "the
table wins where a brief map disagrees"):
  profile     by lane suffix — the foreman PRIMARY (-foreman, and any lane
              whose id IS a foreman) gets P0_FORE; satellites follow their
              suffix family (docs/readme P3_DOCS, sync/pm/dogfood P8_SYNC,
              qa/review P9_REVIEW, releng/perf P2_AGENTIC, security
              P4_SECURITY). Unknown suffix -> refused loudly (never guessed).
  board_type  'jsonl' except -sync lanes ('' — the 75-row -sync precedent).
  shape       the 5-column projects schema (id, sensitivity='open',
              board_type, stack='', profile); extra fields would be dropped
              by the seed's tail-sync on the first reseed.

Usage (board venv python):
  python3 scripts/derive_projects_seed.py [--fleet FLEET_TOML] [--db SCHED_DB]
      [--tables DATA_TABLES_DIR] [--dry-run]

Idempotent: a second run appends nothing (every enabled lane already has a
row). Exit 0 when nothing is missing; exit 1 only when --dry-run found gaps
(so a watchdog can use it as a detector) — the real run exits 0 after
appending.
"""
import argparse
import json
import os
import sqlite3
import sys
import tomllib

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
DEFAULT_DB = os.path.join(os.path.expanduser('~'), '.hermes', 'coding-hermes', 'scheduler.db')
DEFAULT_FLEET = os.path.join(os.path.expanduser('~'), '.hermes', 'fleet.toml')

# Suffix -> profile. The FOREMAN primary is a lane whose own id carries the
# -foreman suffix (or IS a foreman lane): P0_FORE per the table's 5 precedents
# + the brief. Satellites ride their suffix family's existing rows.
SUFFIX_PROFILE = {
    'foreman': 'P0_FORE',    # 5 precedents (h3-protocol/-sdk-go/-sdk-python/-sdk-typescript/-shim-foreman)
    'docs': 'P3_DOCS',       # 52 precedents
    'readme': 'P3_DOCS',     # 51 precedents
    'sync': 'P8_SYNC',       # 75 precedents
    'pm': 'P8_SYNC',         # 64 precedents
    'dogfood': 'P8_SYNC',    # 63 precedents
    'qa': 'P9_REVIEW',       # 63 precedents
    'review': 'P9_REVIEW',   # 32 precedents
    'releng': 'P2_AGENTIC',  # 45 precedents
    'perf': 'P2_AGENTIC',    # 49 precedents
    'security': 'P4_SECURITY',  # python-audit-security precedent
}


def classify_lane(lane_id):
    """(profile, board_type) for a lane id, from the table's own conventions.

    Raises ValueError on a suffix with no precedent — an unclassified lane is
    an owner decision (TR-125 precedent: 122 such lanes were left out on
    purpose rather than guessed).
    """
    stem, _, suffix = lane_id.rpartition('-')
    if suffix in SUFFIX_PROFILE and stem:
        profile = SUFFIX_PROFILE[suffix]
    elif lane_id.endswith('foreman') or 'foreman' in lane_id.rsplit('-', 1)[-1]:
        # a bare foreman lane id with no dash-suffix (defensive; all current
        # foreman lanes carry the -foreman suffix)
        profile = SUFFIX_PROFILE['foreman']
    else:
        raise ValueError(
            f'lane {lane_id!r}: no suffix-profile precedent '
            f'(suffixes: {", ".join(sorted(SUFFIX_PROFILE))}) — profile is an '
            f'owner decision, refusing to guess')
    board_type = '' if suffix == 'sync' else 'jsonl'
    return profile, board_type


def enabled_fleet_lanes(db_path):
    """Set of enabled lane ids from the scheduler DB (authoritative per TR-260)."""
    con = sqlite3.connect(db_path)
    try:
        return {n for (n,) in con.execute('SELECT name FROM projects WHERE enabled=1')}
    finally:
        con.close()


def fleet_toml_lanes(fleet_path):
    with open(fleet_path, 'rb') as f:
        fleet = tomllib.load(f)
    return {p['name'] for p in fleet.get('projects') or []}


def load_projects_rows(tables_dir):
    path = os.path.join(tables_dir, 'projects.jsonl')
    rows = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows, path


def profiles_in_registry(tables_dir):
    """Profile ids declared in data/tables/task_profiles.jsonl (plus the
    bootstrap dict in router_seed.py — same ids)."""
    path = os.path.join(tables_dir, 'task_profiles.jsonl')
    out = set()
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    out.add(json.loads(line).get('id'))
    return {p for p in out if p}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--fleet', default=DEFAULT_FLEET)
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--tables', default=os.path.join(_REPO, 'data', 'tables'))
    ap.add_argument('--dry-run', action='store_true',
                    help='report the gap without appending (exit 1 when non-empty)')
    args = ap.parse_args(argv)

    known_profiles = profiles_in_registry(args.tables)
    missing_profiles = set(SUFFIX_PROFILE.values()) - known_profiles
    if missing_profiles:
        print(f'REFUSING: profile(s) {sorted(missing_profiles)} not in '
              f'task_profiles.jsonl — the registry would reject every row '
              f'that references them', file=sys.stderr)
        return 2

    db_lanes = enabled_fleet_lanes(args.db)
    if not db_lanes:
        print(f'REFUSING: scheduler DB {args.db} yielded 0 enabled lanes — '
              f'refusing to treat that as "nothing to do" (fail loud, never '
              f'seed an empty fleet)', file=sys.stderr)
        return 2
    fleet_lanes = fleet_toml_lanes(args.fleet)
    if fleet_lanes and db_lanes != fleet_lanes:
        # both surfaces exist but disagree: the brief names the DB as
        # authoritative, but a divergence is exactly how a half-rename
        # happens — say so instead of silently picking a side.
        only_db = sorted(db_lanes - fleet_lanes)
        only_fleet = sorted(fleet_lanes - db_lanes)
        print(f'NOTE: db-enabled ({len(db_lanes)}) != fleet.toml '
              f'({len(fleet_lanes)}); db-only={only_db[:10]} '
              f'fleet-only={only_fleet[:10]} — seeding the DB set '
              f'(authoritative per TR-260)', file=sys.stderr)

    rows, path = load_projects_rows(args.tables)
    existing = {r.get('id') for r in rows}
    missing = sorted(db_lanes - existing)

    if not missing:
        print(f'0 missing lanes: all {len(db_lanes)} db-enabled lanes already '
              f'have rows in {path} — nothing to append (idempotent)')
        return 0

    new_rows = []
    for lane in missing:
        profile, board_type = classify_lane(lane)
        new_rows.append({'id': lane, 'sensitivity': 'open',
                         'board_type': board_type, 'stack': '',
                         'profile': profile})

    census = {}
    for r in new_rows:
        census[r['profile']] = census.get(r['profile'], 0) + 1
    print(f'{len(missing)} db-enabled lanes missing from {path}:')
    for lane, r in zip(missing, new_rows):
        print(f"  + {r['id']:40s} {r['profile']} board_type={r['board_type']!r}")
    print('profile census:', dict(sorted(census.items())))

    if args.dry_run:
        print('dry-run: nothing appended')
        return 1

    # read-modify-APPEND (never rewrite): JSONL tables are generated, but the
    # append path keeps any concurrent writer's rows intact and preserves the
    # existing byte content.
    with open(path, 'a', encoding='utf-8') as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f'appended {len(new_rows)} rows -> {path}')
    print('next: python3 scripts/router_seed.py  (regenerates registry.json '
          '+ exports from the updated table)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
