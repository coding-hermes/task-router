"""TR-048 regression battery — direct-invocation seed NS guard (2026-09-14).

Locks in the script-layer half of the TR-045 guard: `python3
scripts/router_seed.py` with NO ROUTING_NS must never silently resolve to the
fleet DuckBrain mirror (/home/kara/duckbrain/namespaces/routing). The TR-045
fix lived only in the `router` CLI env-export map, and a sibling tick proved
the bare direct path bypasses it entirely.

Resolution order now encoded in scripts/router_seed.py when ROUTING_NS is
unset:
  1. ROUTING_ALLOW_FLEET_MIRROR=1 (+ mirror exists) -> the fleet mirror
     (explicit opt-in; production maintain/CLI always set ROUTING_NS).
  2. <data home>/scratch/ns/routing when a scratch run OR a real DuckBrain ns
     exists (data home: TASK_ROUTER_HOME > XDG_DATA_HOME > ~/.local/share).
  3. <duckbrain home>/namespaces/routing — absent on a fresh clone, so the
     seed prints 'ns mirror absent' and writes nothing (legacy fresh-clone
     semantics preserved).

Every probe runs in scratch dirs (ROUTING_REGISTRY/ROUTING_DATA_DIR +
audit-hook writer) — never against the live registry or the live mirror
(same doctrine as tests/test_maintain_repair.py). The audit hook is installed
IN the child process (sys.addaudithook + runpy) so no write can escape the
measurement window, and mirror safety is asserted BOTH by the recorded
write set and by byte-comparing the live mirror's derived tables before and
after the bare run.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
SEED = os.path.join(SCRIPTS, "router_seed.py")
MAINTAIN = os.path.join(SCRIPTS, "router_maintain.py")
PY = sys.executable

try:
    import duckdb  # noqa: F401
    _HAS_DUCKDB = True
except ImportError:
    _HAS_DUCKDB = False

FLEET_MIRROR = "/home/kara/duckbrain/namespaces/routing"

# Runs the seed under an in-process audit hook recording every write-mode
# open, then dumps {writes, mirror_writes} as JSON. argv: <mirror-prefix>
# <out-json> <script> [seed args...]  (sys.argv seen by the seed is clean).
_AUDIT_WRAPPER = r'''
import sys, os, json, runpy
prefix, out, script = sys.argv[1], sys.argv[2], sys.argv[3]
# rewrite argv for the child: the seed's bottom argparse sees only real args
sys.argv = [script] + sys.argv[4:]
writes = []
def _hook(event, args):
    if event == "open":
        path, mode = args[0], args[1]
        if isinstance(path, (str, bytes, os.PathLike)) and mode and \
                any(m in mode for m in ("w", "a", "x", "+")):
            writes.append(os.path.abspath(os.fsdecode(path)))
sys.addaudithook(_hook)
rc = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as e:
    rc = e.code or 0
except BaseException:
    rc = 1
json.dump({"writes": sorted(set(writes)),
           "mirror_writes": [w for w in sorted(set(writes))
                             if w.startswith(prefix)]},
          open(out, "w"))
sys.exit(rc)
'''


def _base_env(tmp_path, **extra):
    """Scratch registry + committed-data copy; ROUTING_NS stripped."""
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    env = dict(os.environ)
    env.pop("ROUTING_NS", None)
    env.pop("TASK_ROUTER_HOME", None)
    env.pop("XDG_DATA_HOME", None)
    env.update({"ROUTING_REGISTRY": str(tmp_path / "registry.json"),
                "ROUTING_DATA_DIR": str(data)})
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run_audited(env, cwd, mirror_prefix=FLEET_MIRROR):
    audit_out = cwd / "audit.json"
    wrapper = cwd / "_audit_runner.py"
    wrapper.write_text(_AUDIT_WRAPPER)
    p = subprocess.run([PY, str(wrapper), mirror_prefix, str(audit_out), SEED],
                       cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=240)
    report = {}
    if audit_out.exists():
        report = json.load(open(audit_out))
    return p, report


def _mirror_derived_snapshot():
    """Byte snapshot of the live mirror's derived tables (or None if absent)."""
    snap = {}
    for t in ("model_perf", "model_tier", "category_levels", "level_defs"):
        path = os.path.join(FLEET_MIRROR, "tables", f"{t}.jsonl")
        if os.path.exists(path):
            snap[path] = open(path, "rb").read()
    return snap


# --------------------------------------------------------------------------
# A) the acceptance case: bare seed, no ROUTING_NS -> zero writes under the
#    live fleet mirror; scratch outputs still produced.
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_bare_seed_writes_nothing_under_fleet_mirror(tmp_path):
    """TR-048 acceptance: `python3 scripts/router_seed.py` with no ROUTING_NS
    resolves/exports NOWHERE under the fleet mirror (audit hook + byte
    comparison), while registry/data outputs still land in their scratch
    overrides."""
    scratch_home = tmp_path / "scratchhome"  # no .duckbrain, no prior ns
    (scratch_home / "home").mkdir(parents=True)
    snap = _mirror_derived_snapshot()
    env = _base_env(tmp_path, HOME=str(scratch_home / "home"))
    p, report = _run_audited(env, tmp_path)
    assert p.returncode == 0, p.stderr[-2000:]
    assert report.get("mirror_writes") == [], (
        f"bare seed wrote under the live mirror: {report.get('mirror_writes')}")
    # belt-and-braces: mirror derived tables byte-identical after the run
    assert _mirror_derived_snapshot() == snap
    # the seed still produced its registry + data outputs in scratch
    assert os.path.exists(env["ROUTING_REGISTRY"])
    assert os.path.exists(os.path.join(env["ROUTING_DATA_DIR"], "model_tier.jsonl"))


@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_bare_seed_resolves_under_data_home_when_duckbrain_home_exists(tmp_path):
    """On a box with a real DuckBrain home (Bane's box), the unset-NS bare run
    must NOT resolve to the fleet mirror's /home/kara path — it redirects to
    the data-home scratch ns (self-contained scratch-first rule)."""
    scratch_home = tmp_path / "scratchhome"
    (scratch_home / "home").mkdir(parents=True)
    env = _base_env(
        tmp_path,
        HOME=str(scratch_home / "home"),
        ROUTING_DUCKBRAIN_HOME=str(scratch_home / "duckbrain"),
    )
    # a prior scratch ns exists under this data home
    prior = scratch_home / "priorhome" / "scratch" / "ns" / "routing"
    os.makedirs(prior, exist_ok=True)
    env["TASK_ROUTER_HOME"] = str(scratch_home / "priorhome")
    p = subprocess.run([PY, SEED], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, p.stderr[-2000:]
    assert f"exported tables to {prior}" in p.stdout, p.stdout[-2000:]
    assert "/home/kara/duckbrain" not in p.stdout
    assert os.path.exists(prior / "tables" / "model_tier.jsonl")


# --------------------------------------------------------------------------
# B) fresh-clone semantics preserved: nothing exists anywhere -> the
#    duckbrain-home default is absent -> 'ns mirror absent', zero NS writes.
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_fresh_clone_still_skips_ns_export(tmp_path):
    empty_home = tmp_path / "emptyhome"
    empty_home.mkdir()
    env = _base_env(
        tmp_path,
        HOME=str(empty_home),
        ROUTING_DUCKBRAIN_HOME=str(empty_home / ".duckbrain"),
    )
    p, report = _run_audited(env, tmp_path,
                             mirror_prefix=str(empty_home / ".duckbrain"))
    assert p.returncode == 0, p.stderr[-2000:]
    assert "ns mirror absent" in p.stdout, p.stdout[-2000:]
    assert "exported tables to" not in p.stdout
    # no write landed under the (absent) duckbrain home either
    assert report.get("mirror_writes") == []
    assert os.path.exists(env["ROUTING_REGISTRY"])


# --------------------------------------------------------------------------
# C) explicit callers keep working: ROUTING_NS override still writes exactly
#    where told; ROUTING_ALLOW_FLEET_MIRROR=1 restores legacy mirror
#    resolution (proved against a SCRATCH mirror, never the live one).
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_explicit_routing_ns_override_still_writes_there(tmp_path):
    ns = tmp_path / "explicitns"
    os.makedirs(ns, exist_ok=True)
    env = _base_env(tmp_path, ROUTING_NS=ns)
    p, report = _run_audited(env, tmp_path)
    assert p.returncode == 0, p.stderr[-2000:]
    assert report.get("mirror_writes") == []
    ns_writes = [w for w in report.get("writes", [])
                 if w.startswith(str(ns))]
    assert ns_writes, "explicit ROUTING_NS caller lost its ns export"
    assert os.path.exists(ns / "tables" / "model_tier.jsonl")
    assert f"exported tables to {ns}" in p.stdout


@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_fleet_mirror_opt_in_restores_legacy_resolution(tmp_path):
    """ROUTING_ALLOW_FLEET_MIRROR=1 + an existing mirror path -> the unset-NS
    seed resolves to ROUTING_FLEET_MIRROR. Proven against a scratch mirror so
    the live mirror is never touched."""
    scratch_home = tmp_path / "scratchhome"
    (scratch_home / "home").mkdir(parents=True)
    mirror = tmp_path / "mirrorscratch"
    os.makedirs(mirror / "tables", exist_ok=True)
    env = _base_env(
        tmp_path,
        HOME=str(scratch_home / "home"),
        ROUTING_FLEET_MIRROR=str(mirror),
        ROUTING_ALLOW_FLEET_MIRROR="1",
    )
    p, report = _run_audited(env, tmp_path, mirror_prefix=str(mirror))
    assert p.returncode == 0, p.stderr[-2000:]
    assert f"exported tables to {mirror}" in p.stdout, p.stdout[-2000:]
    assert report.get("mirror_writes"), "opt-in mirror write not observed"
    assert os.path.exists(mirror / "tables" / "model_tier.jsonl")


@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_redirected_run_creates_the_scratch_ns(tmp_path):
    """The Bane's-box shape end to end: real DuckBrain home exists, scratch ns
    does NOT exist yet, no ROUTING_NS -> the run creates <data home>/scratch/
    ns/routing and exports into it (never the mirror)."""
    scratch_home = tmp_path / "scratchhome"
    (scratch_home / "home" / "duckbrain" / "namespaces" / "routing").mkdir(
        parents=True)
    trh = scratch_home / "freshhome"  # deliberately NO scratch/ subtree
    env = _base_env(tmp_path, HOME=str(scratch_home / "home"),
                    TASK_ROUTER_HOME=str(trh))
    p, report = _run_audited(env, tmp_path)
    assert p.returncode == 0, p.stderr[-2000:]
    exported = scratch_home / "freshhome" / "scratch" / "ns" / "routing"
    assert f"exported tables to {exported}" in p.stdout, p.stdout[-2000:]
    assert os.path.exists(exported / "tables" / "model_tier.jsonl")
    assert report.get("mirror_writes") == []


# --------------------------------------------------------------------------
# D) data-home precedence: TASK_ROUTER_HOME > XDG_DATA_HOME for the
#    scratch redirect target (same contract as task_router/paths.py).
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_scratch_redirect_honors_data_home_precedence(tmp_path):
    scratch_home = tmp_path / "scratchhome"
    (scratch_home / "home").mkdir(parents=True)
    env = _base_env(
        tmp_path,
        HOME=str(scratch_home / "home"),
        ROUTING_DUCKBRAIN_HOME=str(scratch_home / "duckbrain"),
    )
    trh = scratch_home / "trh"
    xdg = scratch_home / "xdg"
    # XDG branch: $XDG_DATA_HOME/task-router IS the data home (paths.py)
    for base, rel in ((trh, "scratch/ns/routing"),
                      (xdg, "task-router/scratch/ns/routing")):
        os.makedirs(base / rel, exist_ok=True)
    # TASK_ROUTER_HOME wins over XDG_DATA_HOME
    env["TASK_ROUTER_HOME"] = str(trh)
    env["XDG_DATA_HOME"] = str(xdg)
    p = subprocess.run([PY, SEED], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, p.stderr[-2000:]
    assert f"exported tables to {trh / 'scratch' / 'ns' / 'routing'}" \
        in p.stdout, p.stdout[-2000:]
    # XDG fallback when TASK_ROUTER_HOME is unset
    env2 = dict(env)
    env2.pop("TASK_ROUTER_HOME")
    p2 = subprocess.run([PY, SEED], cwd=tmp_path, env=env2,
                        capture_output=True, text=True, timeout=240)
    assert p2.returncode == 0, p2.stderr[-2000:]
    assert f"exported tables to {xdg / 'task-router' / 'scratch' / 'ns' / 'routing'}" \
        in p2.stdout, p2.stdout[-2000:]


# --------------------------------------------------------------------------
# E) companion pins in router_maintain.py: step_seed hands its resolved NS to
#    the child, and step_export reads DERIVED tables from the ns the seed
#    actually wrote (in-process, every path redirected to scratch).
# --------------------------------------------------------------------------

def test_maintain_step_seed_pins_child_routing_ns(tmp_path, monkeypatch):
    sys.path.insert(0, SCRIPTS)
    import router_maintain as rm

    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    ns = tmp_path / "maintainns"
    tns = tmp_path / "taskrouterns"
    os.makedirs(ns, exist_ok=True)
    env = dict(os.environ)
    monkeypatch.delenv("ROUTING_NS", raising=False)  # bare operator env
    env.pop("ROUTING_NS", None)  # bare operator env: the pin must supply it
    env.update({"ROUTING_REGISTRY": str(tmp_path / "registry.json"),
                "ROUTING_DATA_DIR": str(data)})
    monkeypatch.setattr(rm, "ROUTING_NS", str(ns))
    monkeypatch.setattr(rm, "TASKROUTER_NS", str(tns))
    monkeypatch.setattr(rm, "REGISTRY", env["ROUTING_REGISTRY"])
    monkeypatch.setattr(rm, "DATA_DIR", str(data))
    monkeypatch.setattr(rm, "BOARD_PY", PY)
    monkeypatch.setenv("ROUTING_REGISTRY", env["ROUTING_REGISTRY"])
    monkeypatch.setenv("ROUTING_DATA_DIR", str(data))
    rc = rm.step_seed(False)
    assert rc == 0
    assert rm._SEEDED_NS == str(ns), "step_seed did not record the pinned ns"
    # the child seed actually wrote the derived tables INTO the pinned ns
    assert os.path.exists(ns / "tables" / "model_tier.jsonl")


def test_maintain_export_reads_derived_from_seeded_ns(tmp_path, monkeypatch):
    """`maintain export` after a BARE seed must not read derived tables from
    this process's own ROUTING_NS (stale forever) — it uses the ns the seed
    actually wrote. Proven with a STALE stand-in ns: its marker row must NOT
    reach the task-router ns."""
    sys.path.insert(0, SCRIPTS)
    import router_maintain as rm

    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    seeded = tmp_path / "seededns"
    stale = tmp_path / "stalens"
    tns = tmp_path / "taskrouterns"
    os.makedirs(seeded, exist_ok=True)
    os.makedirs(stale / "tables", exist_ok=True)
    os.makedirs(tns / "tables", exist_ok=True)
    # fresh derived tables in the ns the (bare) seed wrote
    subprocess.run([PY, SEED],
                   env={**os.environ,
                        "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
                        "ROUTING_DATA_DIR": str(data),
                        "ROUTING_NS": str(seeded)},
                   cwd=tmp_path, capture_output=True, text=True,
                   timeout=240, check=True)
    # the process's own ROUTING_NS holds STALE derived rows (what a bare-seed
    # + export loop would keep re-copying without the handoff)
    with open(stale / "tables" / "model_tier.jsonl", "w") as f:
        f.write(json.dumps({"stale": True}) + "\n")
    monkeypatch.setattr(rm, "ROUTING_NS", str(stale))
    monkeypatch.setattr(rm, "TASKROUTER_NS", str(tns))
    monkeypatch.setattr(rm, "REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setattr(rm, "DATA_DIR", str(data))
    monkeypatch.setattr(rm, "_SEEDED_NS", str(seeded))
    rc = rm.step_export(False)
    assert rc == 0
    exported = open(tns / "tables" / "model_tier.jsonl").read()
    assert exported == open(seeded / "tables" / "model_tier.jsonl").read(), \
        "export did not read the seeded ns"
    assert '"stale": true' not in exported, \
        "export read THIS process's stale ROUTING_NS instead of _SEEDED_NS"
