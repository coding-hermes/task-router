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
