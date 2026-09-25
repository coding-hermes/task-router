"""TR-031 regression tests — router_validate.py (`router validate`).

All tests are hermetic: tmp_path registry/data/state fixtures passed via
ROUTING_REGISTRY / ROUTING_DATA_DIR / ROUTER_STATE_DIR env overrides. No
network, no repo writes.
"""
import json
import os
import sys
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)
VALIDATE = os.path.join(REPO, "scripts", "router_validate.py")

MODEL_ROW = {
    "provider": "fakeprov", "model": "fake-model", "normalized_price": 1.0,
    "plan_tier": 1, "token_factor": 1.0, "data_class": "zdr",
    "disabled": False, "archive": False, "valid_to": None,
}


def _run(args, env, timeout=15):
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run([PY, VALIDATE] + list(args), cwd=REPO, env=full_env,
                          capture_output=True, text=True, timeout=timeout)


def _env(reg, data_dir, state_dir):
    return {
        "ROUTING_REGISTRY": str(reg),
        "ROUTING_DATA_DIR": str(data_dir),
        "ROUTER_STATE_DIR": str(state_dir),
    }


def _valid_fixture(tmp_path, level=3, corrupt_state=False):
    """Build a fully-valid fixture; return (registry, data_dir, state_dir)."""
    data_dir = tmp_path / "data" / "tables"
    data_dir.mkdir(parents=True)
    (data_dir / "task_profiles.jsonl").write_text(
        json.dumps({"id": "P0_TEST", "title": "fixture profile"}) + "\n")
    (data_dir / "task_profile_requirements.jsonl").write_text(
        json.dumps({"task_id": "P0_TEST", "category": "reasoning", "level": level}) + "\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "circuit-state.json").write_text(
        "NOT JSON {{{" if corrupt_state else json.dumps({"pairs": {}}))
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({
        "version": 3, "generated_at": "2026-09-01T00:00:00+00:00",
        "tables": {"models": [MODEL_ROW]},
    }))
    # registry strictly newer than the tables -> freshness check passes
    future = time.time() + 10
    os.utime(reg, (future, future))
    return reg, data_dir, state_dir


def test_validate_valid_fixture_exit0_pure_json(tmp_path):
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 0, f"valid fixture failed: {proc.stdout[:300]} {proc.stderr[:300]}"
    out = json.loads(proc.stdout)  # raises unless stdout is PURE JSON
    assert out["valid"] is True
    assert out["issues"] == []
    names = {c["name"] for c in out["checks"]}
    assert {"registry.exists", "registry.parse", "registry.version",
            "registry.schema", "registry.models_schema", "freshness",
            "profiles.table", "profiles.requirements"} <= names
    assert all(set(c) == {"name", "ok", "detail"} for c in out["checks"])


def test_validate_corrupt_registry_exit1(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text("{not valid json")
    _, data_dir, state_dir = tmp_path, tmp_path / "d", tmp_path / "s"
    data_dir.mkdir()
    state_dir.mkdir()
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["valid"] is False
    assert any("registry" in i and "corrupt" in i for i in out["issues"])
    assert "Traceback" not in proc.stderr


def test_validate_missing_registry_graceful(tmp_path):
    reg = tmp_path / "no-such-registry.json"
    data_dir = tmp_path / "d"
    state_dir = tmp_path / "s"
    data_dir.mkdir()
    state_dir.mkdir()
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["valid"] is False
    assert any("missing" in i and "registry" in i for i in out["issues"])
    assert "Traceback" not in proc.stderr


def test_validate_stale_registry_listed(tmp_path):
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    # make the registry OLDER than the data tables
    past = time.time() - 3600
    os.utime(reg, (past, past))
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["valid"] is False
    assert any("stale registry" in i for i in out["issues"])
    freshness = next(c for c in out["checks"] if c["name"] == "freshness")
    assert freshness["ok"] is False
    assert "warning-level" in freshness["detail"]


def test_validate_profile_level_out_of_range(tmp_path):
    reg, data_dir, state_dir = _valid_fixture(tmp_path, level=7)
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["valid"] is False
    assert any("level 7" in i and "-5..+5" in i for i in out["issues"])


def test_validate_duplicate_profile_ids(tmp_path):
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    (data_dir / "task_profiles.jsonl").write_text(
        json.dumps({"id": "P0_TEST", "title": "a"}) + "\n" +
        json.dumps({"id": "P0_TEST", "title": "b"}) + "\n")
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert any("duplicate profile id" in i for i in out["issues"])


def test_validate_unknown_profile_reference(tmp_path):
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    (data_dir / "task_profile_requirements.jsonl").write_text(
        json.dumps({"task_id": "P9_GHOST", "category": "reasoning", "level": 1}) + "\n")
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert any("unknown profile" in i for i in out["issues"])


def test_validate_corrupt_state_file(tmp_path):
    reg, data_dir, state_dir = _valid_fixture(tmp_path, corrupt_state=True)
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert any("circuit-state.json" in i and "corrupt" in i for i in out["issues"])


def test_validate_human_output_non_json(tmp_path):
    """Without --json the report is human-readable; exit code semantics hold."""
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    proc = _run([], _env(reg, data_dir, state_dir))
    assert proc.returncode == 0
    assert "[ok  ] registry.parse" in proc.stdout
    assert "valid:" in proc.stdout


# ─── TR-082: the freshness check must not red on its own successful write ──

def test_validate_same_second_seed_is_not_stale(tmp_path):
    """Seed writes the tables and registry.json in the same second; the table
    landing a few ms later must not make a correct first run report stale.

    This is the exact fresh-install path the README documents: before the fix
    it printed '[FAIL] freshness: stale registry ... 0s newer' and exited 1."""
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    now = time.time()
    os.utime(reg, (now, now))
    # the newest table 3 ms LATER — same second, bigger float mtime
    tables = sorted((data_dir).glob("*.jsonl"))
    os.utime(tables[-1], (now + 0.003, now + 0.003))
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    out = json.loads(proc.stdout)
    freshness = next(c for c in out["checks"] if c["name"] == "freshness")
    assert freshness["ok"] is True, freshness["detail"]
    assert not any("stale registry" in i for i in out["issues"])
    assert proc.returncode == 0


def test_validate_beyond_tolerance_is_still_stale(tmp_path):
    """The slack must not swallow a genuinely stale registry."""
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    now = time.time()
    os.utime(reg, (now - 60, now - 60))
    tables = sorted((data_dir).glob("*.jsonl"))
    os.utime(tables[-1], (now, now))
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    freshness = next(c for c in out["checks"] if c["name"] == "freshness")
    assert freshness["ok"] is False
    assert "stale registry" in freshness["detail"]


def test_validate_reports_which_paths_were_compared(tmp_path):
    """A reader must be able to see WHAT was compared, not infer the layout."""
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    out = json.loads(proc.stdout)
    paths = next(c for c in out["checks"] if c["name"] == "freshness.paths")
    assert str(reg) in paths["detail"]
    assert str(data_dir) in paths["detail"]


# ─── Worker task 2026-09-21: bare-run default must match seed + spawn ────────
#
# router_validate.py resolved its REGISTRY default through
# task_router.paths.registry_path() (data home) while router_seed.py and
# router_spawn.py default to <repo>/registry.json — so a bare
# `python3 scripts/router_validate.py` at repo root on a healthy checkout
# exited 1 with "registry.exists: missing ~/.local/share/task-router/
# registry.json". Installed CLI use keeps working through the env override:
# task_router.cli exports ROUTING_REGISTRY (data-home derived) before
# dispatching, so the env remains the only mechanism needed.

_BARE_ENV_KEYS = ("ROUTING_REGISTRY", "TASK_ROUTER_HOME", "XDG_DATA_HOME")


def _import_validate_fresh(monkeypatch, **env_over):
    """Import scripts/router_validate.py as a fresh module with the given env.

    Deletes every registry-affecting variable first so module-level DEFAULTS
    (not inherited shell env) decide the resolution. The module is popped from
    sys.modules after the assertion block via the returned cleanup contract.
    """
    for k in _BARE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env_over.items():
        monkeypatch.setenv(k, v)
    scripts_dir = os.path.join(REPO, "scripts")
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    sys.modules.pop("router_validate", None)
    try:
        import importlib
        return importlib.import_module("router_validate")
    finally:
        if scripts_dir in sys.path:
            sys.path.remove(scripts_dir)
        sys.modules.pop("router_validate", None)


def test_validate_default_registry_matches_seed_and_spawn(monkeypatch):
    """With no env overrides, validate's REGISTRY default == seed/spawn default.

    Both other scripts resolve ROUTING_REGISTRY or <repo>/registry.json; the
    validator must not diverge to the data home (measured divergence: bare run
    looked at ~/.local/share/task-router/registry.json and exited 1)."""
    mod = _import_validate_fresh(monkeypatch)
    assert mod.REGISTRY == os.path.join(REPO, "registry.json")


def test_validate_registry_env_override_still_wins(monkeypatch, tmp_path):
    """ROUTING_REGISTRY stays authoritative over the repo default."""
    override = str(tmp_path / "override-registry.json")
    mod = _import_validate_fresh(monkeypatch, ROUTING_REGISTRY=override)
    assert mod.REGISTRY == override


def test_bare_run_at_repo_root_exits0_on_healthy_checkout(monkeypatch):
    """THE acceptance: bare `python3 scripts/router_validate.py` at repo root
    on the current (seeded, committed-tables) tree exits 0 and validates the
    REPO registry — the same file `router seed` writes and `router spawn`
    reads. Runs against the real repo registry/data (no fixtures) because the
    bug was exactly in the no-env resolution path."""
    for k in _BARE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    proc = subprocess.run([PY, VALIDATE, "--json"], cwd=REPO,
                          env=dict(os.environ), capture_output=True,
                          text=True, timeout=60)
    assert "Traceback" not in proc.stderr, proc.stderr[:400]
    out = json.loads(proc.stdout)  # raises unless stdout is PURE JSON
    exists = next(c for c in out["checks"] if c["name"] == "registry.exists")
    assert exists["ok"] is True, out["issues"]
    assert os.path.realpath(exists["detail"]) == os.path.realpath(
        os.path.join(REPO, "registry.json"))
    assert out["valid"] is True, out["issues"]
    assert proc.returncode == 0


# ─── TR-108: the freshness gate gets an OPT-IN self-heal ────────────────────
#
# 2026-09-22 the refresh cron died mid-run ("Interrupted by shutdown before
# terminal completion"), leaving probe_gaps.jsonl newer than registry.json —
# every later `router validate` exited 1 until a human re-ran the seed. The
# heal re-runs scripts/router_seed.py (deterministic, idempotent full rebuild)
# BEFORE the verdict, but only when armed: --heal or ROUTER_VALIDATE_HEAL=1.
# Default OFF because the health plane runs run_checks_dict() in-process on
# every /health request and a monitor must never spawn seed subprocesses.

def test_heal_is_opt_in(monkeypatch):
    mod = _import_validate_fresh(monkeypatch)
    monkeypatch.delenv("ROUTER_VALIDATE_HEAL", raising=False)
    assert mod.heal_is_armed() is False
    monkeypatch.setenv("ROUTER_VALIDATE_HEAL", "1")
    assert mod.heal_is_armed() is True


def test_heal_needed_matches_incident_shapes(monkeypatch):
    """Exactly the two 2026-09-22 shapes heal: missing registry, stale
    freshness. Corrupt state / profile breaks are NOT seed-repairable."""
    mod = _import_validate_fresh(monkeypatch)
    assert mod._heal_needed({
        "issues": ["registry.exists: missing: /x — run scripts/router_seed.py"],
        "checks": []}) is True
    assert mod._heal_needed({
        "issues": [],
        "checks": [{"name": "freshness", "ok": False, "detail": "stale"}]}) is True
    assert mod._heal_needed({
        "issues": [],
        "checks": [{"name": "freshness", "ok": True, "detail": "fresh"}]}) is False
    assert mod._heal_needed({
        "issues": ["state.circuit-state.json: corrupt: bad"],
        "checks": []}) is False


def test_heal_default_off_is_readonly(tmp_path):
    """Without the flag a stale tree exits 1 and is left EXACTLY as found —
    no heal check, no mtime touched, no seed side effects."""
    reg, data_dir, state_dir = _valid_fixture(tmp_path)
    past = time.time() - 3600
    os.utime(reg, (past, past))
    proc = _run(["--json"], _env(reg, data_dir, state_dir))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert not any(c["name"] == "heal" for c in out["checks"])
    assert abs(os.path.getmtime(reg) - past) < 1.0  # untouched


def test_heal_reseeds_missing_registry(seed_registry_copy, tmp_path):
    """Fresh-clone shape: gitignored registry.json absent + --heal -> the seed
    writes it and the SAME run exits 0 with a `heal` check proving it ran."""
    env = dict(seed_registry_copy)
    os.remove(env["ROUTING_REGISTRY"])
    # seed_registry_copy ships the DATA PARENT (read-only consumers); seed and
    # validate both want the tables dir itself.
    env["ROUTING_DATA_DIR"] = os.path.join(env["ROUTING_DATA_DIR"], "tables")
    env["ROUTING_NS"] = str(tmp_path / "ns")  # keep the ns export hermetic
    proc = _run(["--json", "--heal"], env, timeout=300)
    assert proc.returncode == 0, proc.stdout[:400] + proc.stderr[:200]
    out = json.loads(proc.stdout)
    assert out["valid"] is True
    heal = next(c for c in out["checks"] if c["name"] == "heal")
    assert heal["ok"] is True
    assert os.path.exists(env["ROUTING_REGISTRY"])


def test_heal_reseeds_stale_registry(seed_registry_copy, tmp_path):
    """The 2026-09-22 shape: a table gained content the registry never
    learned (cron updated tables, died before the re-seed) + --heal -> the
    re-seed rebuilds the registry from the tables, verdict flips to valid.

    Mtime lag ALONE is not enough (the TR-082 content tiebreak correctly
    absorbs seed write-ordering), so this mutates content and PROVES the
    before-state is stale first — a heal that never fires must fail loudly.
    """
    env = dict(seed_registry_copy)
    tables_dir = os.path.join(env["ROUTING_DATA_DIR"], "tables")
    env["ROUTING_DATA_DIR"] = tables_dir
    reg = env["ROUTING_REGISTRY"]
    # RED precondition: registry is older AND its content no longer matches
    # the tables -> `router validate` must exit 1 with a stale issue.
    past = time.time() - 3600
    os.utime(reg, (past, past))
    models_path = os.path.join(tables_dir, "models.jsonl")
    with open(models_path) as f:
        rows = [json.loads(l) for l in f if l.strip()]
    rows[0]["tr108_probe_field"] = "registry-never-learned-this"
    with open(models_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    before = _run(["--json"], env, timeout=120)
    assert before.returncode == 1, before.stdout[:300]
    before_out = json.loads(before.stdout)
    before_fresh = next(c for c in before_out["checks"] if c["name"] == "freshness")
    assert before_fresh["ok"] is False, before_fresh["detail"]
    # The heal: re-seed converges the tree (the probe field is dropped by the
    # seed's column projection — same content, fresh registry) -> exit 0.
    env["ROUTING_NS"] = str(tmp_path / "ns")
    proc = _run(["--json", "--heal"], env, timeout=300)
    assert proc.returncode == 0, proc.stdout[:400] + proc.stderr[:200]
    out = json.loads(proc.stdout)
    fresh = next(c for c in out["checks"] if c["name"] == "freshness")
    assert fresh["ok"] is True, fresh["detail"]
    heal = next(c for c in out["checks"] if c["name"] == "heal")
    assert heal["ok"] is True, heal["detail"]


def test_heal_failure_keeps_original_issues(tmp_path):
    """Fail-open: when the seed cannot run (no inputs anywhere), the run still
    completes, reports the heal failure, and the ORIGINAL registry.exists
    issue stands — a broken heal never masks the diagnosis."""
    data_dir = tmp_path / "data" / "tables"
    data_dir.mkdir(parents=True)
    (data_dir / "task_profiles.jsonl").write_text(
        json.dumps({"id": "P0_TEST", "title": "x"}) + "\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _env(tmp_path / "registry.json", data_dir, state_dir)
    env["ROUTING_NS"] = str(tmp_path / "absent-ns")  # seed finds no inputs
    proc = _run(["--json", "--heal"], env, timeout=300)
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert any("registry.exists" in i for i in out["issues"])
    assert any(i.startswith("heal:") for i in out["issues"])
    assert "Traceback" not in proc.stderr
