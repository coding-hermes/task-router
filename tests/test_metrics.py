"""TR-021 — metrics append + query CLI tests.

Hermetic: every test uses tmp_path / env monkeypatch; the real
~/.hermes/model-router ledger and the repo data/metrics.jsonl are never touched.
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)
SPAWN = os.path.join(REPO, "scripts", "router_spawn.py")
METRICS = os.path.join(REPO, "scripts", "router_metrics.py")


def _run(script_path, argv, env=None, cwd=REPO, timeout=30):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [PY, script_path] + list(argv),
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _metrics_rows(metrics_file):
    if not os.path.exists(metrics_file):
        return []
    rows = []
    with open(metrics_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _seed_registry(tmp_path):
    """Build a minimal registry.json so resolve produces deterministic hops."""
    reg = tmp_path / "registry.json"
    tables = {
        "providers": [
            {"id": "prov-a"},
            {"id": "prov-b"},
        ],
        "projects": [
            {"id": "my-project", "profile": "P0_FORE"},
        ],
        "task_profiles": [
            {"id": "P0_FORE"},
        ],
        "task_profile_requirements": [
            {"task_id": "P0_FORE", "category": "reasoning", "level": 1},
        ],
        "category_levels": [
            {"category": "reasoning"},
        ],
        "level_defs": [
            {"level": -5},
            {"level": 5},
        ],
        "models": [
            {
                "provider": "prov-a",
                "model": "model-a1",
                "normalized_price": 0.1,
                "token_factor": 1.0,
                "plan_tier": 1,
                "data_class": "zdr",
            },
            {
                "provider": "prov-a",
                "model": "model-a2",
                "normalized_price": 0.2,
                "token_factor": 1.0,
                "plan_tier": 1,
                "data_class": "zdr",
            },
            {
                "provider": "prov-b",
                "model": "model-b1",
                "normalized_price": 0.3,
                "token_factor": 1.0,
                "plan_tier": 1,
                "data_class": "zdr",
            },
        ],
        "model_tier": [
            {"model": "model-a1", "category": "reasoning", "tier": 2},
            {"model": "model-a2", "category": "reasoning", "tier": 2},
            {"model": "model-b1", "category": "reasoning", "tier": 2},
        ],
    }
    reg.write_text(json.dumps({"version": 3, "tables": tables}, indent=1))
    return str(reg)


def _open_quota_state(tmp_path):
    """Create a router state dir with all providers marked quota-open."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "quota-state.json").write_text(json.dumps({
        "providers": {"prov-a": {"status": "open"}, "prov-b": {"status": "open"}}
    }))
    (state_dir / "health-state.json").write_text(json.dumps({"providers": {}}))
    (state_dir / "circuit-state.json").write_text(json.dumps({"version": 1, "pairs": {}}))
    return str(state_dir)


def test_spawn_appends_one_row_per_hop(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    state_dir = _open_quota_state(tmp_path)
    metrics_file = tmp_path / "metrics.jsonl"
    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": str(tmp_path),
        "ROUTER_STATE_DIR": state_dir,
    }
    proc = _run(SPAWN, ("my-project", "--format", "json"), env=env)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "_chain_rows" not in data
    assert data["gate"] == "OPEN"
    rows = _metrics_rows(str(metrics_file))
    assert len(rows) == 3
    for i, row in enumerate(rows, start=1):
        assert row["project"] == "my-project"
        assert row["profile"] == "P0_FORE"
        assert row["order"] == i
        assert row["outcome"] == "resolved"
        assert row["config_snapshot"]["chain_length"] == 3
        assert "registry_source" in row["config_snapshot"]
        assert "gates_loaded" in row["config_snapshot"]
        assert isinstance(row["config_snapshot"]["routing_env_vars"], list)
    assert {r["provider"] for r in rows} == {"prov-a", "prov-b"}
    assert rows[0]["provider"] == "prov-a" and rows[0]["model"] == "model-a1"
    assert rows[0]["price_usd_per_m"] == 0.1


def test_metrics_counter_invariant(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    state_dir = _open_quota_state(tmp_path)
    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": str(tmp_path),
        "ROUTER_STATE_DIR": state_dir,
    }
    proc = _run(SPAWN, ("my-project", "--format", "json"), env=env)
    assert proc.returncode == 0, proc.stderr

    q = _run(METRICS, ("--json",), env={"TASK_ROUTER_HOME": str(tmp_path)})
    assert q.returncode == 0, q.stderr
    data = json.loads(q.stdout)
    assert data["_invariant_check"] is True
    assert sum(data["providers"].values()) == 3
    assert sum(data["models"].values()) == 3
    assert sum(data["pairs"].values()) == 3


def test_metrics_profile_filter(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    state_dir = _open_quota_state(tmp_path)
    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": str(tmp_path),
        "ROUTER_STATE_DIR": state_dir,
    }
    _run(SPAWN, ("my-project", "--format", "json"), env=env)

    q = _run(METRICS, ("--profile", "P0_FORE", "--json"),
             env={"TASK_ROUTER_HOME": str(tmp_path)})
    assert q.returncode == 0
    data = json.loads(q.stdout)
    assert data["total_hops"] == 3

    q2 = _run(METRICS, ("--profile", "NOPE", "--json"),
              env={"TASK_ROUTER_HOME": str(tmp_path)})
    assert q2.returncode == 0
    data2 = json.loads(q2.stdout)
    assert data2["total_hops"] == 0


def test_metrics_since_window(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    metrics_file = tmp_path / "metrics.jsonl"
    now = "2026-09-01T12:00:00+00:00"
    # Seed one old row and one recent row
    old = {"ts": "2026-08-20T12:00:00+00:00", "project": "x", "profile": "P0_FORE",
           "provider": "p", "model": "m", "order": 1, "price_usd_per_m": 1.0,
           "outcome": "resolved", "exclusion_reason": None,
           "config_snapshot": {}}
    recent = {"ts": now, "project": "x", "profile": "P0_FORE",
              "provider": "p", "model": "m", "order": 1, "price_usd_per_m": 1.0,
              "outcome": "resolved", "exclusion_reason": None,
              "config_snapshot": {}}
    with open(metrics_file, "a") as f:
        f.write(json.dumps(old) + "\n")
        f.write(json.dumps(recent) + "\n")

    q = _run(METRICS, ("--since", "7d", "--json"),
             env={"TASK_ROUTER_HOME": str(tmp_path)})
    assert q.returncode == 0
    data = json.loads(q.stdout)
    assert data["total_hops"] == 1

    q2 = _run(METRICS, ("--since", "30d", "--json"),
              env={"TASK_ROUTER_HOME": str(tmp_path)})
    assert q2.returncode == 0
    data2 = json.loads(q2.stdout)
    assert data2["total_hops"] == 2


def test_spawn_hook_swallows_broken_metrics_path(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    state_dir = _open_quota_state(tmp_path)
    # Point metrics at a path that cannot be created / written.
    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": "/proc/nonexistent",
        "ROUTER_STATE_DIR": state_dir,
    }
    proc = _run(SPAWN, ("my-project", "--format", "json"), env=env)
    assert proc.returncode == 0, proc.stderr
    assert "error" not in proc.stdout.lower() or "\"error\"" not in proc.stdout
    data = json.loads(proc.stdout)
    assert data["gate"] == "OPEN"


def test_broken_registry_still_exit_zero_and_records_error(tmp_path, monkeypatch):
    metrics_file = tmp_path / "metrics.jsonl"
    env = {
        "ROUTING_REGISTRY": "/tmp/nonexistent.json",
        "ROUTING_DATA_DIR": str(tmp_path / "empty_tables"),
        "TASK_ROUTER_HOME": str(tmp_path),
    }
    os.makedirs(tmp_path / "empty_tables", exist_ok=True)
    proc = _run(SPAWN, ("my-project", "--format", "json"), env=env)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "error" in data
    rows = _metrics_rows(str(metrics_file))
    # Accept either no metrics rows or a single error row (documented choice)
    assert len(rows) in (0, 1)
    if rows:
        assert rows[0]["outcome"] == "error"
        assert rows[0]["order"] == 0


def test_metrics_json_purity(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    state_dir = _open_quota_state(tmp_path)
    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": str(tmp_path),
        "ROUTER_STATE_DIR": state_dir,
    }
    _run(SPAWN, ("my-project", "--format", "json"), env=env)
    q = _run(METRICS, ("--top-providers", "--top-models", "--top-pairs", "--json"),
             env={"TASK_ROUTER_HOME": str(tmp_path)})
    assert q.returncode == 0, q.stderr
    # stdout must be pure JSON — no stray logging lines
    data = json.loads(q.stdout)
    assert "providers" in data and "models" in data and "pairs" in data


def test_metrics_top_n_limits(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    state_dir = _open_quota_state(tmp_path)
    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": str(tmp_path),
        "ROUTER_STATE_DIR": state_dir,
    }
    _run(SPAWN, ("my-project", "--format", "json"), env=env)
    q = _run(METRICS, ("--top-pairs", "2", "--json"),
             env={"TASK_ROUTER_HOME": str(tmp_path)})
    data = json.loads(q.stdout)
    assert len(data["pairs"]) == 2
    assert data["total_hops"] == 3


def test_metrics_env_var_names_not_values(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    state_dir = _open_quota_state(tmp_path)
    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": str(tmp_path),
        "ROUTER_STATE_DIR": state_dir,
        "ROUTING_SUPER_SECRET": "do-not-leak",
    }
    _run(SPAWN, ("my-project", "--format", "json"), env=env)
    rows = _metrics_rows(str(tmp_path / "metrics.jsonl"))
    assert rows
    names = rows[0]["config_snapshot"]["routing_env_vars"]
    assert "ROUTING_SUPER_SECRET" in names
    assert all("do-not-leak" not in str(v) for v in rows[0]["config_snapshot"].values())
    assert all("do-not-leak" not in json.dumps(row) for row in rows)


def test_spawn_metrics_excluded_hop_recorded(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    # Add a circuit OPEN for the cheapest hop so it is excluded
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "quota-state.json").write_text(json.dumps({
        "providers": {"prov-a": {"status": "open"}, "prov-b": {"status": "open"}}
    }))
    (state_dir / "health-state.json").write_text(json.dumps({"providers": {}}))
    (state_dir / "circuit-state.json").write_text(json.dumps({
        "version": 1,
        "pairs": {
            "prov-a/model-a1": {
                "open_until": "2099-01-01T00:00:00+00:00",
                "failures": 1,
            }
        }
    }))

    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": str(tmp_path),
        "ROUTER_STATE_DIR": str(state_dir),
    }
    proc = _run(SPAWN, ("my-project", "--format", "json"), env=env)
    assert proc.returncode == 0, proc.stderr
    rows = _metrics_rows(str(tmp_path / "metrics.jsonl"))
    assert len(rows) == 3
    outcomes = {r["order"]: r["outcome"] for r in rows}
    assert outcomes[1] == "excluded"
    assert outcomes[2] == "resolved"
    assert outcomes[3] == "resolved"
    excluded = [r for r in rows if r["outcome"] == "excluded"][0]
    assert "circuit" in (excluded["exclusion_reason"] or "").lower()


def test_metrics_table_output_is_not_json(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path)
    state_dir = _open_quota_state(tmp_path)
    env = {
        "ROUTING_REGISTRY": reg,
        "TASK_ROUTER_HOME": str(tmp_path),
        "ROUTER_STATE_DIR": state_dir,
    }
    _run(SPAWN, ("my-project", "--format", "json"), env=env)
    q = _run(METRICS, ("--top-pairs",), env={"TASK_ROUTER_HOME": str(tmp_path)})
    assert q.returncode == 0
    # No --json → human table, should not parse as JSON
    with pytest.raises(json.JSONDecodeError):
        json.loads(q.stdout)
