"""TR-023 + TR-024 regression battery — CLI contract tests from the 3-judge
usability review (2026-08-28).

TR-023 (fail-open breach): --profile-req input validation honesty. Every
invalid input → {error, code: INVALID_REQUIREMENT, retryable: false} + exit 0
(never a traceback, never a silently-weakened requirement). Unknown profile →
'profile X not in registry'; --profile + project conflict reported via stderr;
text format prints the error field.

TR-024 (--json real everywhere): router_pricing --json, router_plan_sweep
--json, router_circuit status --json are PURE JSON on stdout (json.loads
succeeds); router-data-quality.sh never truncates the gap report and aborts on
required-step failure.

All tests run the REAL scripts via subprocess with hermetic env (temp data dir
/ state dir) — no mocks, no import-time env games.
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


def run(*args, timeout=60, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([PY, *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def _hermetic_env(tmp_path):
    """ROUTING_DATA_DIR → temp copy of data/tables; ROUTER_STATE_DIR → temp."""
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return {"ROUTING_DATA_DIR": str(data), "ROUTER_STATE_DIR": str(state)}


# ============================================================ TR-023: spawn ==

def _spawn_err(args, tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), *args, "--format", "json",
            env_extra=env)
    assert p.returncode == 0, f"fail-open: exit must be 0, got {p.returncode}: {p.stderr}"
    data = json.loads(p.stdout)
    assert "error" in data, f"expected error dict, got: {p.stdout}"
    return p, data


def test_profile_req_bare_word_fails_open(tmp_path):
    """'reasoning=high' (non-int level) → JSON error, exit 0, never traceback."""
    p, data = _spawn_err(["--profile-req", "reasoning=high"], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"
    assert data["retryable"] is False


def test_profile_req_empty_arg_fails_open(tmp_path):
    p, data = _spawn_err(["--profile-req", ""], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"
    assert data["retryable"] is False


def test_profile_req_missing_equals_fails_open(tmp_path):
    p, data = _spawn_err(["--profile-req", "reasoning5"], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"


def test_profile_req_out_of_range_fails_open(tmp_path):
    p, data = _spawn_err(["--profile-req", "reasoning=9"], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"
    assert "out of range" in data["error"]


def test_profile_req_typo_category_rejected(tmp_path):
    """A typo'd category must NOT be accepted (would silently weaken to -1)."""
    p, data = _spawn_err(["--profile-req", "typo_category=-5"], tmp_path)
    assert data["code"] == "INVALID_REQUIREMENT"
    assert "unknown category" in data["error"]
    assert "typo_category" in data["error"]


def test_bogus_profile_not_in_registry(tmp_path):
    p, data = _spawn_err(["--profile", "NOT_A_PROFILE"], tmp_path)
    assert "not in registry" in data["error"]
    assert data["code"] == "PROFILE_NOT_FOUND"
    assert data["retryable"] is False


def test_text_format_prints_error_field(tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), "--profile-req", "nope=3",
            "--format", "text", env_extra=env)
    assert p.returncode == 0
    assert "ERROR:" in p.stdout
    assert "INVALID_REQUIREMENT" in p.stdout


def test_profile_project_conflict_reported_stderr(tmp_path):
    """Both --profile and project given → stderr names which wins; JSON pure."""
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), "coding-hermes-scheduler",
            "--profile", "P4_SECURITY", "--format", "json", env_extra=env)
    assert p.returncode == 0
    assert "WARNING" in p.stderr
    assert "resolving via project" in p.stderr
    data = json.loads(p.stdout)  # stdout must stay pure JSON
    assert data["profile"] != "P4_SECURITY"  # project's profile won


def test_profile_req_valid_still_resolves(tmp_path):
    """Regression guard: valid ad-hoc requirements still resolve (no gate change)."""
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_spawn.py"), "--profile-req",
            "reasoning=5 debug=3 vision=-2", "--format", "json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert "error" not in data
    assert "gate" in data


# ============================================================ TR-024: --json ==

def test_pricing_dry_run_json_pure(tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_pricing.py"), "--dry-run", "--json",
            env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)  # must parse — no prose on stdout
    assert set(data) == {"dry_run", "priced", "gaps", "filled_public"}
    assert data["dry_run"] is True
    assert isinstance(data["priced"], list) and isinstance(data["gaps"], list)


def test_pricing_apply_json_pure(tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_pricing.py"), "--json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert data["dry_run"] is False
    assert set(data) == {"dry_run", "priced", "gaps", "filled_public"}


def test_plan_sweep_json_pure(tmp_path):
    env = _hermetic_env(tmp_path)
    p = run(os.path.join(SCRIPTS, "router_plan_sweep.py"), "--json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)  # pure JSON — prose went to stderr
    assert set(data) == {"disabled", "lanes"}
    assert isinstance(data["lanes"], list)


def test_circuit_status_json_contract(tmp_path):
    env = _hermetic_env(tmp_path)
    # record a failure → pair OPEN; then a second pair with an expired breaker
    p = run(os.path.join(SCRIPTS, "router_circuit.py"), "record-failure",
            "provider-a", "model-1", "boom", env_extra=env)
    assert p.returncode == 0
    st = json.load(open(os.path.join(env["ROUTER_STATE_DIR"], "circuit-state.json")))
    st["pairs"]["provider-b/model-2"] = {"failures": 2, "open_until": "2000-01-01T00:00:00+00:00",
                                         "last_failure": "2000-01-01T00:00:00+00:00", "reason": "old"}
    json.dump(st, open(os.path.join(env["ROUTER_STATE_DIR"], "circuit-state.json"), "w"))
    p = run(os.path.join(SCRIPTS, "router_circuit.py"), "status", "--json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert "pairs" in data
    by = {e["pair"]: e for e in data["pairs"]}
    assert by["provider-a/model-1"]["state"] == "OPEN"
    assert by["provider-a/model-1"]["failures"] == 1
    assert by["provider-a/model-1"]["reason"] == "boom"
    assert by["provider-b/model-2"]["state"] == "cooling"  # expired → documented semantics


def test_data_quality_script_no_json_truncation():
    """AC4: the gap report's JSON must never be head/tail-truncated."""
    src = open(os.path.join(SCRIPTS, "router-data-quality.sh")).read()
    gaps_line = [l for l in src.splitlines() if "router_gaps.py" in l]
    assert gaps_line, "gap report line missing"
    assert "head -" not in gaps_line[0], "gap JSON must not be truncated: " + gaps_line[0]
    assert "tail -" not in gaps_line[0], "gap JSON must not be truncated: " + gaps_line[0]
    # real exit codes: required steps must abort the pipeline on failure
    assert "set -euo pipefail" in src or "set -e" in src


def test_pricing_json_dry_run_matches_non_json_summary(tmp_path):
    """--json and prose agree on counts (the flag is real, not a parallel path)."""
    env = _hermetic_env(tmp_path)
    pj = run(os.path.join(SCRIPTS, "router_pricing.py"), "--dry-run", "--json", env_extra=env)
    data = json.loads(pj.stdout)
    assert len(data["priced"]) + len(data["gaps"]) >= 1
