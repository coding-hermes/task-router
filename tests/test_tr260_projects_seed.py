"""TR-260 — the registry must carry every enabled fleet lane, derived not hand-copied.

Origin: the 2026-10-01/02 `<project>-foreman` lane rename left the registry
topology behind — 66 enabled fleet lanes (37 `*-foreman`, 29 satellite lanes
across the guard-*/mischief-*/qa-audit-*/monitoring/asce families) had NO row
in data/tables/projects.jsonl, so router_spawn.py answered
`project <lane> not in registry` and every one of their spawns degraded to a
static chain (the ROUTER-MISS class the fleet-policy-test-watchdog suite 2
guards). data/tables/*.jsonl are GENERATED, so the fix went through the seed
path: scripts/derive_projects_seed.py derives the missing set from the live
scheduler DB and appends the rows; scripts/router_seed.py regenerates the
registry from the table.

What this file pins so the NEXT lane wave cannot silently drop lanes again:
  1. the committed table carries the TR-260 rows (an explicit literal map —
     a classify_lane regression cannot weaken it);
  2. derive_projects_seed.py's suffix conventions match the table's own
     precedent counts (P0_FORE foreman primaries, satellite suffix families,
     '' board_type for -sync) and it REFUSES to guess an unknown suffix;
  3. the generator is idempotent, fails loud on an empty fleet DB, refuses an
     unknown profile, and dry-run appends nothing;
  4. the full seed path regenerates appended rows into registry.json;
  5. the LIVE invariant (enabled fleet lanes ⊆ committed table) — skipped
     where the host has no fleet files, red on the host the day a rename
     wave lands without a re-derive.

Every fixture lives in tmp_path; nothing here touches the live registry, the
live fleet mirror, or the scheduler DB (same doctrine as test_seed_ns_guard).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
PY = sys.executable

SEED_ARGS_TIMEOUT = 120
from conftest import SEED_TIMEOUT  # noqa: E402

GEN = os.path.join(SCRIPTS, "derive_projects_seed.py")

#: The TR-260 rename-wave lanes, as committed by the fix: lane -> profile.
#: This literal IS the pin for AC-d: deleting any of these rows from
#: data/tables/projects.jsonl turns test 1 red. Profiles follow the table's
#: own suffix conventions (37 P0_FORE foreman primaries; satellites by
#: suffix family — docs/readme P3_DOCS, sync/pm/dogfood P8_SYNC, qa/review
#: P9_REVIEW, releng/perf P2_AGENTIC).
TR260_LANES = {
    # 37 foreman primaries (33 project foremen + 4 h3 sub-foremen)
    "9router-foreman": "P0_FORE",
    "ai-plays-poke-foreman": "P0_FORE",
    "asce-foreman": "P0_FORE",
    "auger-foreman": "P0_FORE",
    "boardctl-foreman": "P0_FORE",
    "bunker-foreman": "P0_FORE",
    "chimera-v2-foreman": "P0_FORE",
    "coding-hermes-scheduler-foreman": "P0_FORE",
    "coding-hermes-tools-foreman": "P0_FORE",
    "consensus-foreman": "P0_FORE",
    "crier-foreman": "P0_FORE",
    "dexdat-core-foreman": "P0_FORE",
    "dexdat-memory-foreman": "P0_FORE",
    "digest-foreman": "P0_FORE",
    "duckbrain-foreman": "P0_FORE",
    "gitreins-foreman": "P0_FORE",
    "guard-foreman": "P0_FORE",
    "h3-foreman": "P0_FORE",
    "heading-foreman": "P0_FORE",
    "hermes-canopy-foreman": "P0_FORE",
    "hermes-dagger-foreman": "P0_FORE",
    "hivemind-work-foreman": "P0_FORE",
    "logsey-foreman": "P0_FORE",
    "lore-foreman": "P0_FORE",
    "mafia-ai-benchmark-foreman": "P0_FORE",
    "mischief-foreman": "P0_FORE",
    "monitoring-foreman": "P0_FORE",
    "muster-foreman": "P0_FORE",
    "my-project-foreman": "P0_FORE",
    "off-by-one-foreman": "P0_FORE",
    "pulse-foreman": "P0_FORE",
    "qa-audit-foreman": "P0_FORE",
    "task-router-foreman": "P0_FORE",
    "tatara-foreman": "P0_FORE",
    "terminal-jail-foreman": "P0_FORE",
    "trouble-foreman": "P0_FORE",
    "warpfs-foreman": "P0_FORE",
    # guard family satellites (8)
    "guard-docs": "P3_DOCS",
    "guard-readme": "P3_DOCS",
    "guard-sync": "P8_SYNC",
    "guard-pm": "P8_SYNC",
    "guard-dogfood": "P8_SYNC",
    "guard-qa": "P9_REVIEW",
    "guard-review": "P9_REVIEW",
    "guard-releng": "P2_AGENTIC",
    # mischief family satellites (8)
    "mischief-docs": "P3_DOCS",
    "mischief-readme": "P3_DOCS",
    "mischief-sync": "P8_SYNC",
    "mischief-pm": "P8_SYNC",
    "mischief-dogfood": "P8_SYNC",
    "mischief-qa": "P9_REVIEW",
    "mischief-review": "P9_REVIEW",
    "mischief-releng": "P2_AGENTIC",
    # qa-audit family satellites (8: -qa IS the foreman's own QA lane family)
    "qa-audit-docs": "P3_DOCS",
    "qa-audit-readme": "P3_DOCS",
    "qa-audit-sync": "P8_SYNC",
    "qa-audit-pm": "P8_SYNC",
    "qa-audit-dogfood": "P8_SYNC",
    "qa-audit-qa": "P9_REVIEW",
    "qa-audit-review": "P9_REVIEW",
    "qa-audit-releng": "P2_AGENTIC",
    # monitoring (4)
    "monitoring-docs": "P3_DOCS",
    "monitoring-readme": "P3_DOCS",
    "monitoring-review": "P9_REVIEW",
    # asce (2)
    "asce-releng": "P2_AGENTIC",
    "asce-review": "P9_REVIEW",
}


def _committed_projects():
    path = os.path.join(DATA_DIR, "projects.jsonl")
    rows = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows[r["id"]] = r
    return rows


# ---------------------------------------------------------------------------
# 1. the committed table carries the rename-wave rows
# ---------------------------------------------------------------------------

def test_committed_table_has_all_tr260_lanes_with_pinned_profiles():
    rows = _committed_projects()
    missing = sorted(set(TR260_LANES) - set(rows))
    assert not missing, (
        f"{len(missing)} TR-260 lanes missing from data/tables/projects.jsonl "
        f"— their spawns degrade to static chains (ROUTER-MISS); re-derive "
        f"with scripts/derive_projects_seed.py: {missing[:10]}"
    )
    wrong = {lid: (rows[lid].get("profile"), want)
             for lid, want in TR260_LANES.items()
             if rows[lid].get("profile") != want}
    assert not wrong, (
        f"TR-260 lanes committed with the wrong profile (table convention): {wrong}"
    )
    # foreman primaries must be P0_FORE rows shaped like the 5 pre-TR-260
    # precedents (5-column schema, open, board_type jsonl)
    for lid in ("asce-foreman", "h3-foreman", "qa-audit-foreman"):
        r = rows[lid]
        assert set(r) == {"id", "sensitivity", "board_type", "stack", "profile"}, \
            f"{lid}: row shape drifted from the 5-column projects schema: {sorted(r)}"
        assert r["board_type"] == "jsonl" and r["stack"] == ""


# ---------------------------------------------------------------------------
# 2. classify_lane: the table's own suffix conventions
# ---------------------------------------------------------------------------

def test_classify_lane_matches_table_suffix_conventions():
    sys.path.insert(0, SCRIPTS)
    import derive_projects_seed as dps

    cases = {
        # foreman primary (5 precedents -> P0_FORE)
        "newproj-foreman": ("P0_FORE", "jsonl"),
        # satellite families, profile + board_type per the 590-row precedent
        "newproj-docs": ("P3_DOCS", "jsonl"),
        "newproj-readme": ("P3_DOCS", "jsonl"),
        "newproj-sync": ("P8_SYNC", ""),      # '' board_type: the -sync precedent
        "newproj-pm": ("P8_SYNC", "jsonl"),
        "newproj-dogfood": ("P8_SYNC", "jsonl"),
        "newproj-qa": ("P9_REVIEW", "jsonl"),
        "newproj-review": ("P9_REVIEW", "jsonl"),
        "newproj-releng": ("P2_AGENTIC", "jsonl"),
        "newproj-perf": ("P2_AGENTIC", "jsonl"),
        "newproj-security": ("P4_SECURITY", "jsonl"),
    }
    for lane, want in cases.items():
        assert dps.classify_lane(lane) == want, lane


def test_classify_lane_refuses_unknown_suffix(tmp_path):
    sys.path.insert(0, SCRIPTS)
    import derive_projects_seed as dps

    with pytest.raises(ValueError, match="refusing to guess"):
        dps.classify_lane("newproj-banana")


# ---------------------------------------------------------------------------
# fixtures: a tmp fleet world (scheduler DB + fleet.toml + tables dir)
# ---------------------------------------------------------------------------

PROFILES_MIN = ["P0_FORE", "P1_CODING", "P2_AGENTIC", "P3_DOCS",
                "P4_SECURITY", "P5_VISION_E2E", "P6_DEFAULT", "P7_MOCK",
                "P8_SYNC", "P9_REVIEW"]


def _write_db(path, names):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE projects (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)")
    con.executemany("INSERT INTO projects (name, enabled) VALUES (?, 1)",
                    [(n,) for n in names])
    con.commit()
    con.close()


def _write_fleet(path, names):
    with open(path, "w") as f:
        for n in names:
            f.write("[[projects]]\n")
            f.write(f'name = "{n}"\n')


def _write_tables(path, project_rows=None):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "task_profiles.jsonl"), "w") as f:
        for pid in PROFILES_MIN:
            f.write(json.dumps({"id": pid, "title": pid, "created_at": "2026-08-27",
                                "max_consecutive_per_provider": None,
                                "max_total_per_provider": None,
                                "version": 1, "tag": pid, "allow_slow": None}) + "\n")
    with open(os.path.join(path, "projects.jsonl"), "w") as f:
        for r in (project_rows or []):
            f.write(json.dumps(r) + "\n")


def _run_gen(db, fleet, tables, *extra, timeout=SEED_ARGS_TIMEOUT):
    env = dict(os.environ)
    return subprocess.run(
        [PY, GEN, "--db", db, "--fleet", fleet, "--tables", tables, *extra],
        capture_output=True, text=True, timeout=timeout, env=env)


@pytest.fixture()
def fleet_world(tmp_path):
    db = str(tmp_path / "scheduler.db")
    fleet = str(tmp_path / "fleet.toml")
    tables = str(tmp_path / "tables")
    names = ["newproj-foreman", "newproj-docs", "newproj-sync", "existing-proj"]
    _write_db(db, names)
    _write_fleet(fleet, names)
    _write_tables(tables, project_rows=[
        {"id": "existing-proj", "sensitivity": "open", "board_type": "jsonl",
         "stack": "", "profile": "P0_FORE"},
    ])
    return {"db": db, "fleet": fleet, "tables": tables, "names": names}


# ---------------------------------------------------------------------------
# 3. generator behavior
# ---------------------------------------------------------------------------

def test_generator_appends_missing_and_is_idempotent(fleet_world):
    w = fleet_world
    p1 = _run_gen(w["db"], w["fleet"], w["tables"])
    assert p1.returncode == 0, p1.stderr[-400:]
    assert "appended 3 rows" in p1.stdout

    got = {}
    with open(os.path.join(w["tables"], "projects.jsonl")) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                got[r["id"]] = r
    assert got["newproj-foreman"]["profile"] == "P0_FORE"
    assert got["newproj-docs"]["profile"] == "P3_DOCS"
    assert got["newproj-sync"] == {"id": "newproj-sync", "sensitivity": "open",
                                   "board_type": "", "stack": "",
                                   "profile": "P8_SYNC"}
    assert got["existing-proj"]["profile"] == "P0_FORE"  # prior rows untouched

    # second run: nothing to do, exit 0 (idempotent)
    p2 = _run_gen(w["db"], w["fleet"], w["tables"])
    assert p2.returncode == 0, p2.stderr[-400:]
    assert "0 missing lanes" in p2.stdout
    assert "appended" not in p2.stdout


def test_generator_refuses_empty_fleet_db(tmp_path):
    db = str(tmp_path / "empty.db")
    fleet = str(tmp_path / "fleet.toml")
    tables = str(tmp_path / "tables")
    _write_db(db, [])
    _write_fleet(fleet, [])
    _write_tables(tables)
    p = _run_gen(db, fleet, tables)
    assert p.returncode == 2, (p.stdout, p.stderr)
    assert "REFUSING" in p.stderr and "0 enabled lanes" in p.stderr


def test_generator_refuses_unknown_profile_in_data(tmp_path):
    db = str(tmp_path / "s.db")
    fleet = str(tmp_path / "fleet.toml")
    tables = str(tmp_path / "tables")
    _write_db(db, ["newproj-foreman"])
    _write_fleet(fleet, ["newproj-foreman"])
    # task_profiles.jsonl WITHOUT P0_FORE: every appended row would reference
    # a profile the registry does not declare
    os.makedirs(tables)
    with open(os.path.join(tables, "task_profiles.jsonl"), "w") as f:
        f.write(json.dumps({"id": "P8_SYNC", "title": "sync", "created_at": "x",
                            "max_consecutive_per_provider": None,
                            "max_total_per_provider": None, "version": 1,
                            "tag": "P8_SYNC", "allow_slow": None}) + "\n")
    with open(os.path.join(tables, "projects.jsonl"), "w"):
        pass
    p = _run_gen(db, fleet, tables)
    assert p.returncode == 2, (p.stdout, p.stderr)
    assert "REFUSING" in p.stderr and "P0_FORE" in p.stderr


def test_generator_dry_run_reports_gap_and_appends_nothing(fleet_world):
    w = fleet_world
    before = open(os.path.join(w["tables"], "projects.jsonl")).read()
    p = _run_gen(w["db"], w["fleet"], w["tables"], "--dry-run")
    assert p.returncode == 1  # gaps found: the detector contract
    assert "dry-run: nothing appended" in p.stdout
    assert "newproj-foreman" in p.stdout
    assert open(os.path.join(w["tables"], "projects.jsonl")).read() == before


def test_generator_db_fleet_divergence_names_both_sides(tmp_path):
    db = str(tmp_path / "s.db")
    fleet = str(tmp_path / "fleet.toml")
    tables = str(tmp_path / "tables")
    _write_db(db, ["db-only-lane-foreman", "shared-lane"])
    _write_fleet(fleet, ["fleet-only-lane", "shared-lane"])
    _write_tables(tables, project_rows=[
        {"id": "shared-lane", "sensitivity": "open", "board_type": "jsonl",
         "stack": "", "profile": "P0_FORE"}])
    p = _run_gen(db, fleet, tables)
    assert p.returncode == 0, p.stderr[-400:]
    # db is authoritative per TR-260, but the divergence must be NAMED
    assert "NOTE" in p.stderr and "db-only-lane-foreman" in p.stderr \
        and "fleet-only-lane" in p.stderr
    assert "appended 1 rows" in p.stdout


# ---------------------------------------------------------------------------
# 4. the full seed path regenerates appended rows into the registry
# ---------------------------------------------------------------------------

def test_seed_regenerates_derived_rows_into_registry(tmp_path):
    """AC-d: append via the generator, reseed, the rows exist in registry.json
    with the right profile — so the next lane rename is closed by re-running
    the seed path, never by hand-editing a generated table."""
    data = tmp_path / "data" / "tables"
    os.makedirs(os.path.dirname(data))
    shutil.copytree(DATA_DIR, data)
    db = str(tmp_path / "scheduler.db")
    fleet = str(tmp_path / "fleet.toml")
    _write_db(db, ["fakelane-foreman", "fakelane-sync"])
    _write_fleet(fleet, ["fakelane-foreman", "fakelane-sync"])

    p = _run_gen(db, fleet, str(data))
    assert p.returncode == 0, p.stderr[-400:]
    assert "appended 2 rows" in p.stdout

    reg = str(tmp_path / "registry.json")
    env = dict(os.environ, ROUTING_REGISTRY=reg, ROUTING_DATA_DIR=str(data),
               ROUTING_NS=str(tmp_path / "ns"))
    seed = subprocess.run([PY, os.path.join(SCRIPTS, "router_seed.py")],
                          capture_output=True, text=True, timeout=SEED_TIMEOUT, env=env)
    assert seed.returncode == 0, seed.stderr[-400:]

    with open(reg) as f:
        doc = json.load(f)
    rows = {r["id"]: r for r in doc["tables"]["projects"]}
    assert rows["fakelane-foreman"]["profile"] == "P0_FORE"
    assert rows["fakelane-sync"]["profile"] == "P8_SYNC"
    assert rows["fakelane-sync"]["board_type"] == ""


# ---------------------------------------------------------------------------
# 5. the LIVE invariant (host fleet files required — skip elsewhere)
# ---------------------------------------------------------------------------

_FLEET = os.path.expanduser("~/.hermes/fleet.toml")
_DB = os.path.expanduser("~/.hermes/coding-hermes/scheduler.db")


@pytest.mark.skipif(not (os.path.exists(_FLEET) and os.path.exists(_DB)),
                    reason="host fleet.toml / scheduler.db absent (fresh clone)")
def test_enabled_fleet_lanes_all_in_committed_table():
    """The watchdog suite-2 invariant, host-conditional: every enabled lane in
    the live scheduler DB has a committed projects row. Red the day a rename
    wave lands without a re-derive — the tripwire this whole file exists for."""
    con = sqlite3.connect(_DB)
    try:
        enabled = {n for (n,) in con.execute(
            "SELECT name FROM projects WHERE enabled=1")}
    finally:
        con.close()
    assert enabled, "scheduler DB reported 0 enabled lanes — refuse to pass on that"
    committed = set(_committed_projects())
    missing = sorted(enabled - committed)
    assert not missing, (
        f"{len(missing)} enabled fleet lanes missing from data/tables/"
        f"projects.jsonl (ROUTER-MISS): {missing[:10]} — run "
        f"scripts/derive_projects_seed.py then scripts/router_seed.py"
    )
