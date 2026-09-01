# TR-032 soft-gate integration test.
import datetime, json, os, subprocess, sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "router_spawn.py")
CIRCUIT = os.path.join(REPO, "scripts", "router_circuit.py")
LEDGER = os.path.join(REPO, "scripts", "router_ledger.py")
DATA_DIR = os.path.join(REPO, "data", "tables")


def _load_tables():
    tables = {}
    for fn in sorted(os.listdir(DATA_DIR)):
        if fn.endswith(".jsonl"):
            name = fn[: -len(".jsonl")]
            rows = [json.loads(l) for l in open(os.path.join(DATA_DIR, fn)) if l.strip()]
            tables[name] = rows
    return tables


def _write_registry(tmp_path, tables):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    return str(reg)


def _run(cmd, *args, timeout=60, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, cmd, *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def _env(tmp_path):
    return {"ROUTER_STATE_DIR": str(tmp_path / "state"),
            "LEDGER_FILE": str(tmp_path / "ledger.jsonl"),
            "TASK_ROUTER_HOME": str(tmp_path)}


def _state_dir(tmp_path, providers=None, health=None, circuit=None):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"updated": "test", "providers": providers if providers is not None else {}},
              open(d / "quota-state.json", "w"))
    json.dump({"providers": health if health is not None else {}},
              open(d / "health-state.json", "w"))
    json.dump(circuit if circuit is not None else {"pairs": {}},
              open(d / "circuit-state.json", "w"))
    return str(d)


def _open_providers(tables):
    return {r["id"]: {"status": "open"} for r in tables["providers"]}


def _provider_in_chain(data, provider):
    return any(h["provider"] == provider for h in data.get("chain", []))


def _provider_excluded(data, provider):
    return any(e["provider"] == provider for e in data.get("exclusions", []))


def _pair_str(entry):
    return f"{entry['provider']}/{entry['model']}"


# ----------------------------------------------------------------- AC1: TR-014 --

def test_provider_level_breaker_excludes_all_lanes_of_provider(tmp_path):
    """Open a provider-level breaker for ollama-cloud; spawn excludes every
    ollama-cloud lane with the documented reason string."""
    tables = _load_tables()
    provs = _open_providers(tables)
    _state_dir(tmp_path, providers=provs)
    env = _env(tmp_path)
    env["ROUTING_REGISTRY"] = _write_registry(tmp_path, tables)

    for i in range(3):
        r = _run(CIRCUIT, "record-failure", "ollama-cloud", f"model-{i}",
                 "--class", "api_down", "down", env_extra=env)
        assert r.returncode == 0, r.stderr

    p = _run(SCRIPT, "--profile-req", "reasoning=0", "--format", "json",
             env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data.get("error") is None, data.get("error")

    assert not _provider_in_chain(data, "ollama-cloud")
    assert _provider_excluded(data, "ollama-cloud")
    reasons = [w for e in data["exclusions"] if e["provider"] == "ollama-cloud"
               for w in e["why"]]
    assert any("provider-level" in w and "api_down" in w for w in reasons), reasons


# ------------------------------------------------------------------ AC2: no hard rejection --

def test_spawn_resolves_normally_despite_many_in_flight(tmp_path):
    """Ledger shows 99 in-flight sessions for the head lane but knob is off, so
    spawn still resolves the same lane."""
    tables = _load_tables()
    provs = _open_providers(tables)
    _state_dir(tmp_path, providers=provs)
    env = _env(tmp_path)
    env["ROUTING_REGISTRY"] = _write_registry(tmp_path, tables)

    head_lane = ("opencode-go", "mimo-v2.5")
    with open(env["LEDGER_FILE"], "a") as f:
        for i in range(99):
            row = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "trace_id": f"tr-busy-{i}", "provider": head_lane[0],
                   "model": head_lane[1], "outcome": "started"}
            f.write(json.dumps(row) + "\n")

    p = _run(SCRIPT, "coding-hermes-scheduler", "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data.get("error") is None, data.get("error")
    assert data["head"] is not None
    assert (data["head"]["provider"], data["head"]["model"]) == head_lane


# ----------------------------------------------------------------- AC3: reaction --

def test_overload_on_current_head_advances_to_next_chain_entry(tmp_path):
    """Record an overload failure on the current head lane and verify that a
    second resolve call returns a different head."""
    tables = _load_tables()
    provs = _open_providers(tables)
    _state_dir(tmp_path, providers=provs)
    env = _env(tmp_path)
    env["ROUTING_REGISTRY"] = _write_registry(tmp_path, tables)

    p1 = _run(SCRIPT, "coding-hermes-scheduler", "--format", "json", env_extra=env)
    assert p1.returncode == 0, p1.stderr
    data1 = json.loads(p1.stdout)
    assert data1["head"] is not None
    head_lane = (data1["head"]["provider"], data1["head"]["model"])

    r = _run(CIRCUIT, "record-failure", head_lane[0], head_lane[1],
             "--class", "overload", "HTTP 429", env_extra=env)
    assert r.returncode == 0, r.stderr

    p2 = _run(SCRIPT, "coding-hermes-scheduler", "--format", "json", env_extra=env)
    assert p2.returncode == 0, p2.stderr
    data2 = json.loads(p2.stdout)
    head2 = (data2["head"]["provider"], data2["head"]["model"])

    assert head2 != head_lane, (head_lane, head2)
    excluded = {_pair_str(e) for e in data2.get("exclusions", [])}
    assert _pair_str({"provider": head_lane[0], "model": head_lane[1]}) in excluded


# ------------------------------------------------------------------ AC4: knob default off --

def test_soft_gate_default_off_does_not_exclude_busy_models(tmp_path):
    """When quota-state.json has no soft_gate field and the ledger has a model at
    its configured limit, that model is still resolved (gate off)."""
    tables = _load_tables()
    provs = _open_providers(tables)
    # Only leave opencode-go open; gate all others so the head must be mimo-v2.5
    # if it is not excluded by the (disabled) soft gate.
    for p in provs:
        if p != "opencode-go":
            provs[p] = {"status": "gated", "reason": "gate-off-test"}
    qdoc = {"updated": "test", "providers": provs,
            "models": {"opencode-go/mimo-v2.5": {"concurrency_limit": 5}}}
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump(qdoc, open(d / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(d / "circuit-state.json", "w"))
    env = _env(tmp_path)
    env["ROUTING_REGISTRY"] = _write_registry(tmp_path, tables)

    with open(env["LEDGER_FILE"], "a") as f:
        for i in range(5):
            row = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "trace_id": f"tr-limit-{i}", "provider": "opencode-go",
                   "model": "mimo-v2.5", "outcome": "started"}
            f.write(json.dumps(row) + "\n")

    p = _run(SCRIPT, "coding-hermes-scheduler", "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data.get("error") is None, data.get("error")
    assert data["head"] is not None
    assert (data["head"]["provider"], data["head"]["model"]) == ("opencode-go", "mimo-v2.5")


def test_soft_gate_on_excludes_busy_models(tmp_path):
    """When soft_gate is true, a model at its concurrency limit is excluded."""
    tables = _load_tables()
    provs = _open_providers(tables)
    # Leave only opencode-go and one fallback option open so the head shift is
    # deterministic when the busy model is excluded.
    for p in provs:
        if p not in ("opencode-go", "ollama-cloud"):
            provs[p] = {"status": "gated", "reason": "gate-on-test"}
    qdoc = {"updated": "test", "providers": provs, "soft_gate": True,
            "models": {"opencode-go/mimo-v2.5": {"concurrency_limit": 5}}}
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump(qdoc, open(d / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(d / "circuit-state.json", "w"))
    env = _env(tmp_path)
    env["ROUTING_REGISTRY"] = _write_registry(tmp_path, tables)

    with open(env["LEDGER_FILE"], "a") as f:
        for i in range(5):
            row = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "trace_id": f"tr-limit-{i}", "provider": "opencode-go",
                   "model": "mimo-v2.5", "outcome": "started"}
            f.write(json.dumps(row) + "\n")

    p = _run(SCRIPT, "coding-hermes-scheduler", "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data.get("error") is None, data.get("error")
    head = (data["head"]["provider"], data["head"]["model"]) if data["head"] else None
    assert head != ("opencode-go", "mimo-v2.5"), data["head"]
    excluded = {_pair_str(e) for e in data.get("exclusions", [])}
    assert "opencode-go/mimo-v2.5" in excluded


# ------------------------------------------------------------------ AC5: fail-open --

def test_spawn_fail_open_with_corrupt_circuit_state(tmp_path):
    """Corrupt circuit-state.json must not crash spawn: exit 0, JSON output."""
    tables = _load_tables()
    _state_dir(tmp_path)
    env = _env(tmp_path)
    env["ROUTING_REGISTRY"] = _write_registry(tmp_path, tables)
    (tmp_path / "state" / "circuit-state.json").write_text("{not json")
    p = _run(SCRIPT, "--profile-req", "reasoning=0", "--format", "json",
             env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert "error" in data or data.get("gate") is not None


def test_spawn_fail_open_with_missing_quota_state(tmp_path):
    """Missing quota-state.json behaves as soft_gate off: spawn exits 0."""
    tables = _load_tables()
    env = _env(tmp_path)
    env["ROUTING_REGISTRY"] = _write_registry(tmp_path, tables)
    p = _run(SCRIPT, "coding-hermes-scheduler", "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert "error" not in data


# ---------------------------------------------------------------- ledger wiring --

def test_ledger_start_end_creates_trace_and_in_flight_count(tmp_path):
    """router_ledger.py start/end integration: start creates a trace; before end
    it counts as in-flight; after end it does not."""
    env = _env(tmp_path)
    r = _run(LEDGER, "start", "--provider", "opencode-go", "--model", "mimo-v2.5",
             "--project", "coding-hermes-scheduler", "--trace-id", "tr-ledger-1",
             env_extra=env)
    assert r.returncode == 0, r.stderr

    p1 = _run(SCRIPT, "coding-hermes-scheduler", "--format", "json", env_extra=env)
    data1 = json.loads(p1.stdout)
    assert data1["gates_loaded"]["ledger"]

    r2 = _run(LEDGER, "end", "--trace-id", "tr-ledger-1", "--outcome", "success",
              env_extra=env)
    assert r2.returncode == 0, r2.stderr

    p2 = _run(SCRIPT, "coding-hermes-scheduler", "--format", "json", env_extra=env)
    data2 = json.loads(p2.stdout)
    assert data2["gates_loaded"]["ledger"]
    assert data2["gates_loaded"]["ledger_rows"] == data1["gates_loaded"]["ledger_rows"] - 1


def test_ledger_cli_contract(tmp_path):
    """End with an invalid outcome exits non-zero; start returns a trace_id."""
    env = _env(tmp_path)
    r = _run(LEDGER, "start", "--provider", "p", "--model", "m", "--trace-id", "tr-cli-1",
             env_extra=env)
    assert r.returncode == 0
    tid = r.stdout.strip().splitlines()[-1].strip()
    assert tid.startswith("tr-")
    bad = _run(LEDGER, "end", "--trace-id", tid, "--outcome", "not-valid",
               env_extra=env)
    assert bad.returncode == 2
