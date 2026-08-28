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
              "normalized_price": 0.0001, "plan_tier": 0,
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
    assert _pair(r["head"]) == "prov-marker/marker-9000"
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
    assert _pair(r["head"]) == "opencode-go/mimo-v2.5"


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
    assert _pair(r["head"]) == "opencode-go/mimo-v2.5"


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
