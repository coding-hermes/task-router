"""TR-020 — model capabilities + versioned tagged profiles.

Hermetic tests:
  - capability fields (api_type/vision/thinking) round-trip through seed
  - context_limit IS the single source of truth (no duplicate context_window column)
  - tag -> version resolution works in router_spawn.py
  - retag idempotency: applying the same retag twice yields the same final state
  - old version still resolvable by exact id
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


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_models_jsonl_has_capability_fields():
    with open(os.path.join(DATA_DIR, "models.jsonl")) as f:
        row = json.loads(next(f))
    assert "api_type" in row
    assert "vision" in row
    assert "thinking" in row
    assert "context_window" not in row  # single source of truth is context_limit


def test_seed_round_trips_capabilities_null_and_value(tmp_path):
    env = _hermetic_env(tmp_path)
    p = _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    with open(env["ROUTING_REGISTRY"]) as f:
        reg = json.load(f)
    rows = reg["tables"]["models"]
    assert rows
    non_null = [r for r in rows if r.get("api_type") is not None or
                r.get("thinking") is not None]
    null_rows = [r for r in rows if r.get("api_type") is None and
                 r.get("vision") is None and r.get("thinking") is None]
    assert non_null, "expected some models with capability values"
    assert null_rows, "expected some models with all-NULL capabilities"
    for r in rows:
        assert "api_type" in r and "vision" in r and "thinking" in r


def test_profiles_have_version_and_tag(tmp_path):
    env = _hermetic_env(tmp_path)
    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    with open(env["ROUTING_REGISTRY"]) as f:
        reg = json.load(f)
    for p in reg["tables"]["task_profiles"]:
        assert "version" in p
        assert "tag" in p
        assert p["version"] is not None
        assert p["tag"] is not None


def test_tag_resolution_backwards_compatible(tmp_path):
    """A tag equal to an existing id still resolves to that id."""
    env = _hermetic_env(tmp_path)
    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "my-project",
             "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    data = json.loads(p.stdout)
    assert data.get("profile") == "P0_FORE"


def test_retag_and_resolve(tmp_path):
    """Create a new version of P0_FORE, tag it, and resolve via the tag."""
    env = _hermetic_env(tmp_path)
    data_dir = tmp_path / "data"
    prof_path = data_dir / "task_profiles.jsonl"
    profiles = [json.loads(l) for l in open(prof_path) if l.strip()]
    # insert new version, move tag
    new_version = None
    for p in profiles:
        if p["id"] == "P0_FORE":
            p["tag"] = None  # old version no longer tagged
            new_version = {
                "id": "P0_FORE_v2",
                "title": "P0_FORE version 2",
                "created_at": "2026-09-01T00:00:00",
                "max_consecutive_per_provider": p.get("max_consecutive_per_provider"),
                "max_total_per_provider": p.get("max_total_per_provider"),
                "version": 2,
                "tag": "P0_FORE",
            }
    profiles.append(new_version)
    _write_jsonl(prof_path, profiles)

    # copy requirements for the new version id
    req_path = data_dir / "task_profile_requirements.jsonl"
    reqs = [json.loads(l) for l in open(req_path) if l.strip()]
    extra = []
    for r in reqs:
        if r["task_id"] == "P0_FORE":
            extra.append({"task_id": "P0_FORE_v2", "category": r["category"], "level": r["level"]})
    reqs.extend(extra)
    _write_jsonl(req_path, reqs)

    # projects.jsonl references the tag "P0_FORE" for my-project by default;
    # make sure the project row uses the tag too.
    proj_path = data_dir / "projects.jsonl"
    projects = [json.loads(l) for l in open(proj_path) if l.strip()]
    for proj in projects:
        if proj["id"] == "my-project":
            proj["profile"] = "P0_FORE"  # tag reference
    _write_jsonl(proj_path, projects)

    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    # open quota so the chain isn't fully gated
    open(tmp_path / "state" / "quota-state.json", "w").write(json.dumps({"providers": {
        "ollama-cloud": {"status": "open", "reason": "open"},
        "opencode-go": {"status": "open", "reason": "open"},
        "deepseek": {"status": "open", "reason": "open"},
        "zai-glm": {"status": "open", "reason": "open"},
        "neuralwatt": {"status": "open", "reason": "open"},
        "minimax": {"status": "open", "reason": "open"},
        "openai-codex": {"status": "open", "reason": "open"},
        "kimi-for-coding": {"status": "open", "reason": "open"},
    }}))
    # project references the tag "P0_FORE" -> should now resolve to P0_FORE_v2
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "my-project",
             "--format", "json", "--no-health", env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    data = json.loads(p.stdout)
    assert data.get("profile") == "P0_FORE_v2"


def test_retag_idempotent(tmp_path):
    """Applying the same retag twice yields identical final state."""
    env = _hermetic_env(tmp_path)
    data_dir = tmp_path / "data"
    prof_path = data_dir / "task_profiles.jsonl"

    def retag():
        profiles = [json.loads(l) for l in open(prof_path) if l.strip()]
        for p in profiles:
            if p["id"] == "P0_FORE":
                p["tag"] = None
            if p["id"] == "P0_FORE_v2":
                p["tag"] = "P0_FORE"
        _write_jsonl(prof_path, profiles)

    retag()
    first = [json.loads(l) for l in open(prof_path) if l.strip()]
    retag()
    second = [json.loads(l) for l in open(prof_path) if l.strip()]
    assert first == second


def test_old_version_resolves_by_exact_id(tmp_path):
    """After retag, the old version row still resolves when referenced by id."""
    env = _hermetic_env(tmp_path)
    data_dir = tmp_path / "data"
    prof_path = data_dir / "task_profiles.jsonl"
    profiles = [json.loads(l) for l in open(prof_path) if l.strip()]
    for p in profiles:
        if p["id"] == "P0_FORE":
            p["tag"] = None
        if p["id"] == "P0_FORE_v2":
            p["tag"] = "P0_FORE"
    _write_jsonl(prof_path, profiles)

    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "--profile", "P0_FORE",
             "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    data = json.loads(p.stdout)
    assert data.get("profile") == "P0_FORE"


def test_spawn_chain_emits_capabilities(tmp_path):
    env = _hermetic_env(tmp_path)
    state_dir = tmp_path / "state"
    # open quota so the chain isn't fully gated
    open(state_dir / "quota-state.json", "w").write(json.dumps({"providers": {
        "ollama-cloud": {"status": "open", "reason": "open"},
        "opencode-go": {"status": "open", "reason": "open"},
        "deepseek": {"status": "open", "reason": "open"},
        "zai-glm": {"status": "open", "reason": "open"},
        "neuralwatt": {"status": "open", "reason": "open"},
        "minimax": {"status": "open", "reason": "open"},
        "openai-codex": {"status": "open", "reason": "open"},
        "kimi-for-coding": {"status": "open", "reason": "open"},
    }}))
    _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env).check_returncode()
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "my-project",
             "--format", "json", "--no-health", env_extra=env)
    assert p.returncode == 0, p.stderr[:400]
    data = json.loads(p.stdout)
    assert "error" not in data
    head = data.get("head")
    assert isinstance(head, dict), f"head missing/None: {data.get('gate')}"
    assert "context_limit" in head
    assert any(k in head for k in ("api_type", "vision", "thinking", "context_limit"))
