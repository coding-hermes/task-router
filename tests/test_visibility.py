"""TR-025/TR-026 regression battery — runtime visibility (source + gates_loaded).

Locks in: resolve output SAYS where its data came from (registry.json vs
data/tables fallback), missing gate-state files are reported loudly in
gates_loaded (never a silent pass), and fail-open behavior is untouched
(corrupt registry still resolves from committed data/tables; missing state
files still behave as absent gates, not fabricated defaults).

TR-026 adds the ledger wired-flag: an empty/absent ledger (the scheduler does
not call router_ledger.py start/end yet) must be reported as
gates_loaded.ledger=false + a 'spawn ledger NOT WIRED' warning — the TR-007
'model busy' concurrency gate is visibly inactive, never silently dead.

TR-033/TR-055 add the stderr-telemetry switch: ROUTER-MISS per-lane lines are
QUIET by default (they flooded 1000+ lines per ad-hoc resolve and drowned real
warnings) and ROUTER_MISS_VERBOSE=1 restores the audit trail; --quiet /
ROUTER_SPAWN_QUIET=1 still silences unconditionally.

TR-043 adds the model_aliases lookup: a lane whose model id is an alias of a
tiered base inherits the base's tier and can enter the chain instead of being
dropped with tier=None before the gate stage.

The 2 pre-existing failures (P9_REVIEW invariants + fallback lane, TR-029)
are unrelated and deliberately NOT touched here.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import router_spawn  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, "data", "tables")


# ---------------------------------------------------------------- fixtures ----

def _load_tables():
    """Committed registry data as {table: [row...]} (the fallback path)."""
    tables = {}
    for fn in sorted(os.listdir(DATA_DIR)):
        if fn.endswith(".jsonl"):
            name = fn[: -len(".jsonl")]
            rows = [json.loads(l) for l in open(os.path.join(DATA_DIR, fn)) if l.strip()]
            tables[name] = rows
    return tables


def _state_dir(tmp_path, quota=True, health=True, circuit=True, ledger=True,
               providers=None, health_state=None, circuit_state=None):
    """State dir with per-file presence control (TR-025: missing files must be
    reported, so each file is independently omittable)."""
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    if quota:
        json.dump({"updated": "test",
                   "providers": providers if providers is not None else {}},
                  open(d / "quota-state.json", "w"))
    if health:
        json.dump({"providers": health_state if health_state is not None else {}},
                  open(d / "health-state.json", "w"))
    if circuit:
        json.dump({"pairs": circuit_state if circuit_state is not None else {}},
                  open(d / "circuit-state.json", "w"))
    if ledger:
        # fresh 'started' row (now) so ledger_in_flight counts it (a fixed
        # timestamp ages past STALE_MS and correctly drops to 0 rows)
        import datetime
        fresh = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        (d / "ledger.jsonl").write_text(
            '{"trace_id": "t1", "provider": "prov-a", "model": "a1", '
            f'"outcome": "started", "ts": "{fresh}"}}\n')
    return str(d)


def _open_providers(tables):
    """All registry providers open — zero gate interference for visibility
    assertions (mirrors test_regression)."""
    return {r["id"]: {"status": "open"} for r in tables["providers"]}


def _write_registry(tmp_path, tables):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    return str(reg)


def _resolve(monkeypatch, tmp_path, tables=None, project="coding-hermes-scheduler",
             state_dir=None, registry=None):
    tables = tables if tables is not None else _load_tables()
    monkeypatch.setattr(router_spawn, "REGISTRY",
                        registry if registry is not None else _write_registry(tmp_path, tables))
    if state_dir is not None:
        monkeypatch.setattr(router_spawn, "MR", state_dir)
    return router_spawn.resolve(project=project)


def _pair(e):
    return f"{e['provider']}/{e['model']}"


# -------------------------------------------------------- source + fallback ---

def test_healthy_path_source_registry_json(monkeypatch, tmp_path):
    """registry.json present + valid → source=registry.json, fallback_used
    False, no warning, all gate files reported loaded."""
    tables = _load_tables()
    state = _state_dir(tmp_path, providers=_open_providers(tables))
    r = _resolve(monkeypatch, tmp_path, tables, state_dir=state)
    assert "error" not in r, r.get("error")
    assert r["source"] == "registry.json"
    assert r["fallback_used"] is False
    assert r["warnings"] == []
    assert r["head"] is not None
    # chain must come from the registry copy, not data/tables: stamp the
    # registry with a marker model and confirm it is NOT in the chain
    # (proves the registry copy was actually loaded)
    assert r["gates_loaded"] == {
        "health": True, "circuit": True, "quota": True,
        "ledger": True, "ledger_rows": 1}


def test_healthy_path_marker_proves_registry_loaded(monkeypatch, tmp_path):
    """The registry copy — not data/tables — is what resolve() uses: stamp a
    unique marker model into registry.json's models and require it in the
    chain (it is absent from the committed data/tables)."""
    tables = _load_tables()
    marker = {"provider": "prov-marker", "model": "marker-9000",
              # 2026-09-17: xKiro's $0 deepseek-v4-flash lane carries ctx 1048576, so
              # the marker's context bump must clear THAT now (1.1M). Tie-break chain:
              # price ($0 tie) -> context -> model name. 0.0001 would lose outright.
              "normalized_price": 0.0, "plan_tier": 0, "context_limit": 1100000,
              "data_class": "public", "token_factor": 1.0}
    stamped = dict(tables)
    stamped["models"] = list(tables["models"]) + [marker]
    # tier +5 in every category so the marker clears every requirement
    # (blank tier defaults to -1 and would fail P1_CODING's >= 0 reqs)
    cats = sorted({r["category"] for r in tables["model_tier"]})
    stamped["model_tier"] = list(tables["model_tier"]) + [
        {"model": "marker-9000", "category": c, "tier": 5} for c in cats]
    state = _state_dir(tmp_path, providers=_open_providers(tables) | {"prov-marker": {"status": "open"}})
    r = _resolve(monkeypatch, tmp_path, stamped, state_dir=state)
    assert "error" not in r, r.get("error")
    assert r["source"] == "registry.json"
    assert _pair(r["head"]) == "prov-marker/marker-9000"  # marker must WIN (proves registry load); context 1.1M > xkiro 1,048,576
    # price ordering: marker (plan_tier 0, price 0.0001) must be hop 1


def test_corrupt_registry_source_fallback_and_warning(monkeypatch, tmp_path):
    """Corrupt registry.json → resolves from data/tables, source=data/tables,
    fallback_used True, warning names the failure. Fail-open preserved."""
    tables = _load_tables()
    reg = tmp_path / "registry.json"
    reg.write_text("{ this is not valid json !!!")
    state = _state_dir(tmp_path, providers=_open_providers(tables))
    r = _resolve(monkeypatch, tmp_path, tables, state_dir=state, registry=str(reg))
    assert "error" not in r, r.get("error")
    assert r["source"] == "data/tables"
    assert r["fallback_used"] is True
    assert any("registry.json" in w for w in r["warnings"]), r["warnings"]
    assert r["head"] is not None  # resilience: resolution still works
    # the fallback data is the committed registry — head must match the
    # golden fixed-point head for this profile (same tables as registry.json)
    assert _pair(r["head"]) == "xkiro/deepseek/deepseek-v4-flash"  # 2026-09-17: xKiro $0/1M-ctx lane wins tie-breaks (see test_regression goldens)


def test_missing_registry_source_fallback(monkeypatch, tmp_path):
    """Missing registry.json (fresh clone) → data/tables fallback, visible."""
    tables = _load_tables()
    state = _state_dir(tmp_path, providers=_open_providers(tables))
    r = _resolve(monkeypatch, tmp_path, tables, state_dir=state,
                 registry=str(tmp_path / "does-not-exist.json"))
    assert "error" not in r, r.get("error")
    assert r["source"] == "data/tables"
    assert r["fallback_used"] is True
    assert any("missing" in w for w in r["warnings"]), r["warnings"]


# ------------------------------------------------------------- gates_loaded ---

def test_missing_health_state_reported_false(monkeypatch, tmp_path):
    """health-state.json deleted → gates_loaded.health=false, circuit/quota/
    ledger still true. Gate behavior unchanged: with quota open and no health
    file, absent health != DOWN, so the head still resolves (fail-open)."""
    tables = _load_tables()
    state = _state_dir(tmp_path, providers=_open_providers(tables), health=False)
    assert not os.path.isfile(os.path.join(state, "health-state.json"))
    r = _resolve(monkeypatch, tmp_path, tables, state_dir=state)
    assert "error" not in r, r.get("error")
    assert r["gates_loaded"]["health"] is False
    assert r["gates_loaded"]["circuit"] is True
    assert r["gates_loaded"]["quota"] is True
    assert r["gates_loaded"]["ledger"] is True  # ledger fixture has a trace
    assert r["gates_loaded"]["ledger_rows"] == 1
    # behavior unchanged: a missing health file must NOT fabricate a DOWN
    # gate — the chain still resolves to the healthy head
    assert r["head"] is not None
    assert _pair(r["head"]) == "xkiro/deepseek/deepseek-v4-flash"  # 2026-09-17: xKiro $0/1M-ctx lane wins tie-breaks (see test_regression goldens)


def test_missing_all_state_files_reported(monkeypatch, tmp_path):
    """ALL state files deleted → every gates_loaded flag false, ledger_rows 0,
    resolution still succeeds (fail-open; absent != open, but nothing is
    fabricated into a gate either)."""
    tables = _load_tables()
    state = _state_dir(tmp_path, quota=False, health=False, circuit=False,
                       ledger=False)
    r = _resolve(monkeypatch, tmp_path, tables, state_dir=state)
    assert "error" not in r, r.get("error")
    assert r["gates_loaded"] == {
        "health": False, "circuit": False, "quota": False,
        "ledger": False, "ledger_rows": 0}
    # quota-state absent → every provider quota-gated (absent != open, the
    # pre-existing fail-closed CI semantics) → head None is CORRECT here;
    # the point of TR-025 is that the missing files are visible, not that
    # they change behavior
    assert r["head"] is None
    assert r["gate"] in ("NO-OPEN-HOP", "NO-CHAIN")


# --------------------------------------------------- ledger wired flag (TR-026) ---

def test_empty_ledger_reports_unwired_loudly(monkeypatch, tmp_path):
    """A present-but-EMPTY ledger.jsonl (the exact 0-byte unwired state) →
    gates_loaded.ledger=false, ledger_rows=0, and a 'spawn ledger NOT WIRED'
    warning. Concurrency accounting is visibly inactive — never a silent pass.
    Gate BEHAVIOR is unchanged (no rows → nothing busy → full chain)."""
    tables = _load_tables()
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    (d / "quota-state.json").write_text(
        json.dumps({"providers": _open_providers(tables)}))
    (d / "health-state.json").write_text(json.dumps({"providers": {}}))
    (d / "circuit-state.json").write_text(json.dumps({"pairs": {}}))
    (d / "ledger.jsonl").write_text("")  # 0 bytes — the live unwired state
    r = _resolve(monkeypatch, tmp_path, tables, state_dir=str(d))
    assert "error" not in r, r.get("error")
    assert r["gates_loaded"]["ledger"] is False
    assert r["gates_loaded"]["ledger_rows"] == 0
    assert any("NOT WIRED" in w for w in r["warnings"]), r["warnings"]
    assert any("model busy" in w for w in r["warnings"]), r["warnings"]
    # behavior unchanged: an empty ledger must NOT fabricate busy models
    assert r["head"] is not None
    assert not any("model busy" in g for g in r["gate_reasons"])


def test_missing_ledger_file_reports_unwired(monkeypatch, tmp_path):
    """No ledger.jsonl at all → same unwired visibility (wired=false + warning)."""
    tables = _load_tables()
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    (d / "quota-state.json").write_text(
        json.dumps({"providers": _open_providers(tables)}))
    (d / "health-state.json").write_text(json.dumps({"providers": {}}))
    (d / "circuit-state.json").write_text(json.dumps({"pairs": {}}))
    # no ledger.jsonl
    r = _resolve(monkeypatch, tmp_path, tables, state_dir=str(d))
    assert "error" not in r, r.get("error")
    assert r["gates_loaded"]["ledger"] is False
    assert r["gates_loaded"]["ledger_rows"] == 0
    assert any("NOT WIRED" in w for w in r["warnings"]), r["warnings"]


def test_ledger_with_trace_reports_wired(monkeypatch, tmp_path):
    """Once a trace lands (start/end wired), ledger=false flips to true and the
    warning disappears — the flag tracks the data feed, never a config file."""
    tables = _load_tables()
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    (d / "quota-state.json").write_text(
        json.dumps({"providers": _open_providers(tables)}))
    (d / "health-state.json").write_text(json.dumps({"providers": {}}))
    (d / "circuit-state.json").write_text(json.dumps({"pairs": {}}))
    import datetime
    fresh = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    (d / "ledger.jsonl").write_text(
        '{"trace_id": "t1", "provider": "prov-a", "model": "a1", '
        f'"outcome": "started", "ts": "{fresh}"}}\n')
    r = _resolve(monkeypatch, tmp_path, tables, state_dir=str(d))
    assert "error" not in r, r.get("error")
    assert r["gates_loaded"]["ledger"] is True
    assert r["gates_loaded"]["ledger_rows"] == 1
    assert not any("NOT WIRED" in w for w in r["warnings"]), r["warnings"]



# ------------------------------------------------- TR-033 / TR-055 quiet mode ---

def _open_providers(tables):
    """All registry providers open — zero gate interference."""
    return {r["id"]: {"status": "open"} for r in tables["providers"]}


def _scrub_telemetry_env(monkeypatch):
    """Remove BOTH telemetry env switches so the default can be asserted."""
    monkeypatch.delenv("ROUTER_SPAWN_QUIET", raising=False)
    monkeypatch.delenv("ROUTER_MISS_VERBOSE", raising=False)


def test_quiet_defaults_true_without_env(monkeypatch):
    """AC1 (TR-055): telemetry is QUIET by default — no env set at all."""
    _scrub_telemetry_env(monkeypatch)
    assert router_spawn._quiet() is True


def test_router_miss_verbose_restores_telemetry(monkeypatch):
    """AC2 (TR-055): ROUTER_MISS_VERBOSE=1 is the opt-in audit trail."""
    _scrub_telemetry_env(monkeypatch)
    monkeypatch.setenv("ROUTER_MISS_VERBOSE", "1")
    assert router_spawn._quiet() is False


def test_spawn_quiet_beats_router_miss_verbose(monkeypatch):
    """--quiet / ROUTER_SPAWN_QUIET=1 wins over the verbose opt-in (AC3)."""
    monkeypatch.setenv("ROUTER_SPAWN_QUIET", "1")
    monkeypatch.setenv("ROUTER_MISS_VERBOSE", "1")
    assert router_spawn._quiet() is True


def test_resolve_is_silent_by_default_and_loud_when_opted_in(monkeypatch, tmp_path, capsys):
    """AC1+AC2 end-to-end: an ad-hoc resolve that misses hundreds of lanes
    writes NOTHING to stderr by default; ROUTER_MISS_VERBOSE=1 restores the
    per-lane ROUTER-MISS audit trail.  Telemetry never changes the result."""
    tables = _load_tables()
    monkeypatch.setattr(router_spawn, "REGISTRY", _write_registry(tmp_path, tables))
    state_dir = _state_dir(tmp_path, quota=True, health=True, circuit=True,
                           ledger=False,  # empty ledger so the TR-026 warning fires
                           providers=_open_providers(tables))
    monkeypatch.setattr(router_spawn, "MR", state_dir)
    _scrub_telemetry_env(monkeypatch)

    # Ad-hoc requirement that almost every model fails — pre-TR-055 this
    # emitted ~1000+ ROUTER-MISS lines per resolve.
    r = router_spawn.resolve(project=None, profile_id=None,
                             adhoc=["reasoning=5"])
    captured = capsys.readouterr()
    assert captured.err == ""                     # default: silent
    assert "ROUTER-MISS:" not in captured.err
    assert "WARNING: spawn ledger NOT WIRED" not in captured.err

    # Opt in: the audit trail comes back.
    monkeypatch.setenv("ROUTER_MISS_VERBOSE", "1")
    r2 = router_spawn.resolve(project=None, profile_id=None,
                              adhoc=["reasoning=5"])
    captured2 = capsys.readouterr()
    assert "ROUTER-MISS:" in captured2.err
    assert "WARNING: spawn ledger NOT WIRED" in captured2.err
    # stdout untouched by telemetry: identical resolve (modulo timestamp)
    r.pop("resolved_at", None)
    r2.pop("resolved_at", None)
    assert r == r2


def test_quiet_env_var_alone_suppresses_stderr(monkeypatch, tmp_path, capsys):
    """ROUTER_SPAWN_QUIET=1 without CLI --quiet is sufficient (TR-033 kept)."""
    tables = _load_tables()
    monkeypatch.setattr(router_spawn, "REGISTRY", _write_registry(tmp_path, tables))
    state_dir = _state_dir(tmp_path, quota=True, health=True, circuit=True,
                           ledger=False, providers=_open_providers(tables))
    monkeypatch.setattr(router_spawn, "MR", state_dir)
    monkeypatch.setenv("ROUTER_SPAWN_QUIET", "1")
    monkeypatch.delenv("ROUTER_MISS_VERBOSE", raising=False)

    r = router_spawn.resolve(project="coding-hermes-scheduler")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert r.get("head")


def test_cli_quiet_flag_sets_env_and_suppresses_stderr(tmp_path):
    """AC3 (TR-055): the --quiet CLI flag still silences stderr (argparse sets
    ROUTER_SPAWN_QUIET=1).  Baseline flipped: the DEFAULT run is silent too;
    ROUTER_MISS_VERBOSE=1 is the only way back to the per-lane audit trail."""
    import subprocess
    tables = _load_tables()
    reg = _write_registry(tmp_path, tables)
    state_dir = _state_dir(tmp_path, quota=True, health=True, circuit=True,
                           ledger=False,
                           providers={r["id"]: {"status": "open"}
                                      for r in tables["providers"]})
    env = os.environ.copy()
    env["ROUTING_REGISTRY"] = reg
    env["ROUTER_STATE_DIR"] = state_dir
    env.pop("ROUTER_SPAWN_QUIET", None)
    env.pop("ROUTER_MISS_VERBOSE", None)

    default = subprocess.run(
        [sys.executable, "-m", "scripts.router_spawn", "coding-hermes-scheduler",
         "--format", "json"],
        capture_output=True, text=True, cwd=REPO, env=env)
    assert default.returncode == 0
    assert default.stderr == ""          # TR-055: quiet by default
    assert json.loads(default.stdout)    # pure JSON stdout

    env_verbose = dict(env, ROUTER_MISS_VERBOSE="1")
    loud = subprocess.run(
        [sys.executable, "-m", "scripts.router_spawn", "coding-hermes-scheduler",
         "--format", "json"],
        capture_output=True, text=True, cwd=REPO, env=env_verbose)
    assert loud.returncode == 0
    assert "ROUTER-MISS:" in loud.stderr or "WARNING: spawn ledger NOT WIRED" in loud.stderr

    quiet = subprocess.run(
        [sys.executable, "-m", "scripts.router_spawn", "coding-hermes-scheduler",
         "--format", "json", "--quiet"],
        capture_output=True, text=True, cwd=REPO, env=env_verbose)
    assert quiet.returncode == 0
    assert quiet.stderr == ""            # --quiet wins over verbose
    assert json.loads(quiet.stdout)


def _quiet_env(tmp_path, tables, state_dir):
    """Hermetic subprocess env for the TR-033/TR-055 CLI tests: registry + state
    dir pointed at the fixtures, metrics isolated into tmp (TASK_ROUTER_HOME,
    same convention as test_cli_paths), BOTH telemetry switches scrubbed so the
    default behavior (TR-055: quiet) is what a fresh embedder would see."""
    env = os.environ.copy()
    env["ROUTING_REGISTRY"] = _write_registry(tmp_path, tables)
    env["ROUTER_STATE_DIR"] = state_dir
    env["TASK_ROUTER_HOME"] = str(tmp_path / "metrics-home")
    env.pop("ROUTER_SPAWN_QUIET", None)
    env.pop("ROUTER_MISS_VERBOSE", None)
    return env


def _spawn_proc(env, *extra):
    import subprocess
    return subprocess.run(
        [sys.executable, "-m", "scripts.router_spawn",
         "coding-hermes-scheduler", "--format", "json", *extra],
        capture_output=True, text=True, cwd=REPO, env=env, timeout=60)


def test_quiet_stdout_identical_to_loud(tmp_path):
    """Criterion: --quiet must produce IDENTICAL JSON stdout — stderr silence
    alone is not enough.  The loud run opts in via ROUTER_MISS_VERBOSE=1
    (TR-055 moved the default to quiet); parsed JSON is equal modulo
    resolved_at."""
    tables = _load_tables()
    state_dir = _state_dir(tmp_path, quota=True, health=True, circuit=True,
                           ledger=False, providers=_open_providers(tables))
    env = _quiet_env(tmp_path, tables, state_dir)
    env["ROUTER_MISS_VERBOSE"] = "1"

    loud = _spawn_proc(env)
    assert loud.returncode == 0, loud.stderr
    assert "ROUTER-MISS:" in loud.stderr  # opt-in: telemetry ON

    quiet = _spawn_proc(env, "--quiet")
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stderr == ""  # telemetry suppressed
    loud_doc = json.loads(loud.stdout)
    quiet_doc = json.loads(quiet.stdout)
    loud_doc.pop("resolved_at", None)
    quiet_doc.pop("resolved_at", None)
    assert loud_doc == quiet_doc


def test_quiet_fail_open_error_json_on_stdout(tmp_path):
    """Fail-open contract under quiet: an error resolve (unknown project)
    still exits 0 and prints {"error": ...} on stdout; stderr stays empty.
    Telemetry (now opt-in) never changes the JSON contract: the
    ROUTER_MISS_VERBOSE=1 run's stdout is identical."""
    tables = _load_tables()
    state_dir = _state_dir(tmp_path, quota=True, health=True, circuit=True,
                           ledger=False, providers=_open_providers(tables))
    env = _quiet_env(tmp_path, tables, state_dir)

    import subprocess
    args = [sys.executable, "-m", "scripts.router_spawn",
            "no-such-project-xyz", "--format", "json"]

    default = subprocess.run(args, capture_output=True, text=True,
                             cwd=REPO, env=env, timeout=60)
    verbose = subprocess.run(args, capture_output=True, text=True,
                             cwd=REPO, env=dict(env, ROUTER_MISS_VERBOSE="1"),
                             timeout=60)
    quiet = subprocess.run(args + ["--quiet"], capture_output=True, text=True,
                           cwd=REPO, env=env, timeout=60)
    assert default.returncode == 0  # fail-open: exit 0 on error
    assert verbose.returncode == 0
    assert quiet.returncode == 0
    assert quiet.stderr == ""
    default_doc = json.loads(default.stdout)
    verbose_doc = json.loads(verbose.stdout)
    quiet_doc = json.loads(quiet.stdout)
    assert "error" in default_doc and "no-such-project-xyz" in default_doc["error"]
    assert default_doc == quiet_doc == verbose_doc


# ------------------------------------------------------- TR-043 aliases ----

def _alias_tables():
    """Minimal registry: two priced lanes, tier evidence only on the base.

    prov-b/variant-model is a serving variant of prov-a/base-model's weights
    and carries NO tier row of its own — without the alias lookup it scores
    tier=None for every requirement and is dropped before the gate stage.
    """
    return {
        "providers": [{"id": "prov-a"}, {"id": "prov-b"}],
        "models": [
            {"provider": "prov-a", "model": "base-model", "normalized_price": 0.5,
             "data_class": "open", "context_limit": 1000000},
            {"provider": "prov-b", "model": "variant-model", "normalized_price": 0.1,
             "data_class": "open", "context_limit": 1000000},
        ],
        "model_tier": [{"model": "base-model", "category": "reasoning", "tier": 5}],
        "category_levels": [{"category": "reasoning", "level": 5, "label": "q95",
                             "min_perf": 0.9}],
        "level_defs": [{"level": lvl, "label": str(lvl)} for lvl in range(-5, 6)],
        "fallback_lanes": [], "projects": [], "task_profiles": [],
        "task_profile_requirements": [],
    }


def _alias_data_dir(monkeypatch, tmp_path, rows, subdir="tables"):
    """Point router_spawn.DATA_DIR at a tmp table dir holding the alias rows."""
    ddir = tmp_path / subdir
    ddir.mkdir(exist_ok=True)
    with open(ddir / "model_aliases.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    monkeypatch.setattr(router_spawn, "DATA_DIR", str(ddir))
    return str(ddir)


def _resolve_alias_fixture(monkeypatch, tmp_path, tables, alias_rows):
    monkeypatch.setattr(router_spawn, "REGISTRY", _write_registry(tmp_path, tables))
    monkeypatch.setattr(router_spawn, "MR",
                        _state_dir(tmp_path, providers=_open_providers(tables)))
    _alias_data_dir(monkeypatch, tmp_path, alias_rows)
    _scrub_telemetry_env(monkeypatch)
    return router_spawn.resolve(adhoc=["reasoning=5"])


def test_alias_variant_inherits_base_tier_and_enters_chain(monkeypatch, tmp_path):
    """AC5 (TR-043): a lane whose model id is an alias of a tiered base inherits
    the base's tier and ENTERS the chain (cheapest-first), instead of being
    dropped with tier=None for every requirement."""
    tables = _alias_tables()
    r = _resolve_alias_fixture(
        monkeypatch, tmp_path, tables,
        [{"model": "variant-model", "inherits": "base-model",
          "note": "test fixture: serving variant of the base weights"}])

    chain = [f'{e["provider"]}/{e["model"]}' for e in r["chain"]]
    assert chain == ["prov-b/variant-model", "prov-a/base-model"]
    assert r["head"]["model"] == "variant-model"   # $0.1 < $0.5, both tier 5
    assert [e["hop"] for e in r["chain"]] == [1, 2]


def test_alias_variant_without_mapping_is_dropped(monkeypatch, tmp_path):
    """Control: with no alias row the variant keeps tier=None and never reaches
    the chain — the pre-TR-043 behavior, unchanged (fail-open, no fabrication)."""
    tables = _alias_tables()
    r = _resolve_alias_fixture(monkeypatch, tmp_path, tables, [])

    chain = [f'{e["provider"]}/{e["model"]}' for e in r["chain"]]
    assert chain == ["prov-a/base-model"]
    assert r["head"]["model"] == "base-model"


def test_alias_chain_is_transitive(monkeypatch, tmp_path):
    """variant -> mid (untiered) -> base (tiered): the lookup follows the whole
    chain, because registry renames chain (deepseek-v4-flash-flex ->
    deepseek-v4-flash -> deepseek-flash)."""
    tables = _alias_tables()
    tables["models"].append(
        {"provider": "prov-a", "model": "mid-model", "normalized_price": 0.2,
         "data_class": "open", "context_limit": 1000000})
    r = _resolve_alias_fixture(
        monkeypatch, tmp_path, tables,
        [{"model": "variant-model", "inherits": "mid-model"},
         {"model": "mid-model", "inherits": "base-model"}])

    assert router_spawn._alias_chain("variant-model") == \
        ["variant-model", "mid-model", "base-model"]
    chain = [f'{e["provider"]}/{e["model"]}' for e in r["chain"]]
    assert chain == ["prov-b/variant-model", "prov-a/mid-model", "prov-a/base-model"]


def test_alias_tiers_own_evidence_wins_and_blanks_fill(monkeypatch, tmp_path):
    """The variant's OWN tier always wins; only missing/blank categories are
    filled from the alias base."""
    _alias_data_dir(monkeypatch, tmp_path,
                    [{"model": "variant-model", "inherits": "base-model"}])
    tiers = {"variant-model": {"reasoning": 2, "debug": None},
             "base-model": {"reasoning": 5, "debug": 4, "review": 3}}
    folded = router_spawn._fold_tier_names(tiers)

    mt = router_spawn._alias_tiers(tiers, folded, "variant-model")
    assert mt["reasoning"] == 2   # own evidence wins over the base's 5
    assert mt["debug"] == 4       # blank/None row filled from the base
    assert mt["review"] == 3      # absent category filled from the base


def test_alias_tier_lookup_is_case_folded(monkeypatch, tmp_path):
    """Registry ids drift in case — the committed tables carry both casings of
    the same weights (an openrouter lane `stepfun/step-3.5-flash` next to the
    tier table's `stepfun/Step-3.5-Flash`).  A case mismatch must not blank a
    lane: the lane's OWN rows are found folded, and the alias base's too."""
    _alias_data_dir(monkeypatch, tmp_path,
                    [{"model": "Variant-Model", "inherits": "Base-Model"}])

    # (a) the lane's own rows are stored under a different casing
    tiers = {"Variant-Model": {"reasoning": 5}}
    folded = router_spawn._fold_tier_names(tiers)
    assert router_spawn._alias_tiers(tiers, folded, "variant-model")["reasoning"] == 5

    # (b) the alias base's rows are stored under a different casing
    tiers = {"Base-Model": {"debug": 4}}
    folded = router_spawn._fold_tier_names(tiers)
    mt = router_spawn._alias_tiers(tiers, folded, "variant-model")
    assert mt["debug"] == 4


def test_alias_map_is_cached_per_path(monkeypatch, tmp_path):
    """The map is loaded once per resolved path (module-level cache) and a
    different data dir gets a FRESH read — a stale map can never leak across
    tests or across an ops data-home switch."""
    d1 = _alias_data_dir(monkeypatch, tmp_path,
                         [{"model": "a", "inherits": "b"}])
    assert router_spawn._alias_map() == {"a": "b"}

    # same path, rewritten on disk: the cached map still answers
    with open(os.path.join(d1, "model_aliases.jsonl"), "w") as f:
        f.write(json.dumps({"model": "a", "inherits": "c"}) + "\n")
    assert router_spawn._alias_map() == {"a": "b"}

    # different path = different cache key = fresh read
    _alias_data_dir(monkeypatch, tmp_path,
                    [{"model": "x", "inherits": "y"}], subdir="tables2")
    assert router_spawn._alias_map() == {"x": "y"}


def test_alias_map_fail_open_on_malformed_file(monkeypatch, tmp_path):
    """A corrupt alias file degrades to {} (no inheritance, no exception) — the
    router's fail-open contract is never broken by enrichment data."""
    ddir = tmp_path / "tables"
    ddir.mkdir()
    (ddir / "model_aliases.jsonl").write_text("{not json\n")
    monkeypatch.setattr(router_spawn, "DATA_DIR", str(ddir))

    assert router_spawn._alias_map() == {}
    assert router_spawn._alias_chain("anything") == ["anything"]
    assert router_spawn._alias_tiers({}, {}, "anything") == {}
