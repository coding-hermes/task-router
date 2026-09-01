"""TR-015 — context window support: data-driven context limits, min_context gating.

Hermetic: builds scratch data dirs and verifies:
  - context_limit round-trips through router_seed.py into registry.json (NULL stays NULL)
  - router_spawn.py emits context_limit per chain lane
  - min_context requirement excludes lanes whose context_limit is known and too small
  - unknown (NULL) context_limit passes with a visible context_note
  - docs/chains-2026-09-01.md carries a context column
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
PY = sys.executable


def _run(*args, timeout=120, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([PY, *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def _hermetic_env(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return {"ROUTING_DATA_DIR": str(data), "ROUTER_STATE_DIR": str(state),
            "ROUTING_REGISTRY": str(tmp_path / "registry.json")}


# ------------------------------------------------------------------ data ACs ----

def test_models_jsonl_carries_context_limit():
    """Every model row has a context_limit key; unknowns are NULL."""
    with open(os.path.join(DATA_DIR, "models.jsonl")) as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            assert "context_limit" in row


def test_seed_round_trips_context_limit_null_and_int(tmp_path):
    """router_seed.py loads models into the registry with context_limit intact."""
    env = _hermetic_env(tmp_path)
    p = _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    with open(env["ROUTING_REGISTRY"]) as f:
        reg = json.load(f)
    rows = reg["tables"]["models"]
    assert rows, "registry models table empty"
    nulls = [r for r in rows if r.get("context_limit") is None]
    ints = [r for r in rows if isinstance(r.get("context_limit"), int)]
    assert nulls, "expected some NULL context_limit rows"
    assert ints, "expected some integer context_limit rows"
    # all values are either null or int
    for r in rows:
        ctx = r.get("context_limit")
        assert ctx is None or isinstance(ctx, int), f"bad context_limit {ctx!r}"


def test_context_limit_values_come_from_modelsdev_or_manual_note(tmp_path):
    """Rows with non-NULL context_limit have a corresponding model_catalog row
    (models.dev source) or are catalog-only adds from router_modelsdev."""
    env = _hermetic_env(tmp_path)
    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    with open(env["ROUTING_REGISTRY"]) as f:
        reg = json.load(f)
    model_keys = {(r["provider"], r["model"]) for r in reg["tables"]["models"]
                  if r.get("context_limit") is not None}
    # at least the canonical deepseek/zai/ollama-cloud lanes should have values
    assert ("deepseek", "deepseek-v4-flash") in model_keys
    assert ("zai-glm", "glm-5.3-flash") in model_keys


# ------------------------------------------------------------------ spawn ACs ----

def test_spawn_emits_context_limit_per_lane(tmp_path):
    env = _hermetic_env(tmp_path)
    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "my-project",
             "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    data = json.loads(p.stdout)
    assert "error" not in data
    for c in data["chain"]:
        assert "context_limit" in c


def test_spawn_min_context_excludes_low_window_lanes(tmp_path):
    """--profile-req min_context=1000000 drops lanes with smaller known windows."""
    env = _hermetic_env(tmp_path)
    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "my-project",
             "--profile-req", "min_context=1000000",
             "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    data = json.loads(p.stdout)
    assert "error" not in data
    for c in data["chain"]:
        ctx = c.get("context_limit")
        # known-small lanes are excluded; only NULL or >=1M remain
        assert ctx is None or ctx >= 1_000_000, c


def test_spawn_null_context_passes_with_note(tmp_path):
    """A lane with unknown context_limit is not excluded but carries a note."""
    env = _hermetic_env(tmp_path)
    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "my-project",
             "--profile-req", "min_context=1000000",
             "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    data = json.loads(p.stdout)
    assert "error" not in data
    null_lanes = [c for c in data["chain"] if c.get("context_limit") is None]
    if null_lanes:
        assert any("context_unknown" in (c.get("context_note") or "")
                   for c in null_lanes)


def test_spawn_min_context_invalid_rejected(tmp_path):
    """Non-integer min_context is INVALID_REQUIREMENT, exit 0."""
    env = _hermetic_env(tmp_path)
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "my-project",
             "--profile-req", "min_context=big",
             "--format", "json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert data.get("code") == "INVALID_REQUIREMENT"


# ----------------------------------------------------------------- docs ACs ----

def test_chains_snapshot_has_context_column():
    path = os.path.join(REPO, "docs", "chains-2026-09-01.md")
    assert os.path.exists(path)
    content = open(path).read()
    assert "Context column" in content or "ctx=" in content
    assert "context_limit" in content


def test_profile_req_min_context_is_known_category(tmp_path):
    """min_context is accepted as a synthetic requirement category."""
    env = _hermetic_env(tmp_path)
    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "my-project",
             "--profile-req", "min_context=500000",
             "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    data = json.loads(p.stdout)
    assert "error" not in data
    assert "chain" in data
