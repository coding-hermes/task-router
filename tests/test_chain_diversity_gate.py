"""TR-368 — chain-diversity CI gate: red/green acceptance evidence.

Hermetic: every test drives scripts/chain_diversity_gate.py through a FRESH
subprocess (the gate isolates its own measurement plane via env — registry
forced to the committed-tables fallback, a scratch ci-mirror policy dir, no
outcome-floor data), and the starved fixtures are built in tmp_path and fed
through ROUTING_DATA_DIR (the scratch-fixture pattern TR-005 pinned, reused by
test_null_debt_invariants.py). Never the live deployment state, never the
seeded registry.json.

Per violation arm, a deliberately-starved fixture must FAIL the gate (exit 1,
violation named with the profile and code) and the committed real data must
PASS (exit 0) — a gate that only ever ran green proves nothing about whether
it can fail.
"""

import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "chain_diversity_gate.py")
TABLES = os.path.join(REPO, "data", "tables")
PY = sys.executable

#: env vars that would silently steer the measurement into a non-CI plane
ISOLATE = ("ROUTING_DATA_DIR", "ROUTING_REGISTRY", "ROUTER_STATE_DIR",
           "ROUTING_AVERAGES_FILE", "ROUTER_SPAWN_QUIET", "TASK_ROUTER_HOME")

_spec = importlib.util.spec_from_file_location("chain_diversity_gate", SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ISOLATE}
    return env


def _run_gate(tables_dir: str, *extra_args: str) -> subprocess.CompletedProcess:
    env = _clean_env()
    env["ROUTING_DATA_DIR"] = str(tables_dir)
    return subprocess.run(
        [PY, SCRIPT, *extra_args], capture_output=True, text=True,
        timeout=300, env=env, cwd=REPO,
    )


def _fixture(tmp_path, level: int) -> str:
    """Minimal tables that resolve a 1-provider, 1-lane chain for P_FIXTURE.

    Requirement level chooses the violation arm: level 0 satisfies the lane's
    tier 2 (resolves 1 hop / 1 provider -> single-provider), level 9 exceeds
    every lane (resolves 0 hops -> no-hops). Shapes mirror the committed
    tables: models carry the eligibility evidence fields (normalized_price +
    a model_perf.perf / model_tier row).
    """
    d = tmp_path / "tables"
    d.mkdir(parents=True, exist_ok=True)
    rows = {
        "providers.jsonl": [{"id": "exprov0", "plan": "sub"}],
        "models.jsonl": [{
            "provider": "exprov0", "model": "exmodel0",
            "normalized_price": 0.1, "plan_tier": None, "token_factor": 1.0,
            "archive": False, "disabled": None,
            "valid_from": "2026-01-01", "valid_to": None,
            "context_limit": 128000,
        }],
        "model_tier.jsonl": [{
            "model": "exmodel0", "category": "code_gen", "tier": 2,
            "tier_source": "bench",
        }],
        "model_perf.jsonl": [{
            "model": "exmodel0", "category": "code_gen", "perf": 0.6,
            "tier": 2, "tier_source": "bench", "source_ref": "bench:fixture",
        }],
        "task_profiles.jsonl": [{"id": "P_FIXTURE", "tag": "P_FIXTURE"}],
        "task_profile_requirements.jsonl": [
            {"task_id": "P_FIXTURE", "category": "code_gen", "level": level},
        ],
        "fallback_lanes.jsonl": [],
    }
    for fn, table in rows.items():
        with open(d / fn, "w") as f:
            for r in table:
                f.write(json.dumps(r) + "\n")
    return str(d)


# ---------------------------------------------------------------- real data


def test_chain_diversity_passes_on_committed_data_today():
    """The acceptance pin: exit 0 on the repo's own tables right now."""
    proc = _run_gate(TABLES, "--json")
    assert proc.returncode == 0, f"gate FAILED on committed data:\n{proc.stdout}"
    payload = json.loads(proc.stdout)
    assert payload["passed"] is True
    assert payload["violations"] == []


def test_gate_reports_every_committed_profile():
    """Every profile in data/tables/task_profiles.jsonl appears, with hops >= 1
    and >= 2 distinct providers — the census must cover ALL of them, not a
    hard-coded subset (the profile set may grow)."""
    committed = [json.loads(l)["id"]
                 for l in open(os.path.join(TABLES, "task_profiles.jsonl"))
                 if l.strip()]
    proc = _run_gate(TABLES, "--json")
    payload = json.loads(proc.stdout)
    assert sorted(payload["table"]) == sorted(committed)
    for pid, stat in payload["table"].items():
        assert stat["hops"] >= 1, (pid, stat)
        assert len(stat["providers"]) >= 2, (pid, stat)


def test_profile_ids_ignores_torn_lines():
    """A torn task_profiles row must not silently drop profiles from the census."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "data"))
        with open(os.path.join(tmp, "data", "task_profiles.jsonl"), "w") as f:
            f.write('{"id": "P_A"}\n')
            f.write('{"id": "P_B\n')  # torn line — garbage, not a profile
            f.write('{"id": "P_C"}\n')
        ids = gate.profile_ids(os.path.join(tmp, "data"))
    assert ids == ["P_A", "P_C"]


# ------------------------------------------------------------------- teeth


def test_evaluate_names_no_hops_and_single_provider():
    """Unit: the pure verdict arms, red first — a gate that only ever ran green
    proves nothing."""
    stats_zero = [{"profile": "P_X", "hops": 0, "providers": {}, "error": None}]
    violations, passed = gate.evaluate(stats_zero)
    assert not passed
    assert violations[0]["code"] == "no-hops"
    assert "P_X" in violations[0]["message"]

    stats_single = [{"profile": "P_Y", "hops": 5, "providers": {"only": 5},
                     "error": None}]
    violations, passed = gate.evaluate(stats_single)
    assert not passed
    assert violations[0]["code"] == "single-provider"
    assert "'only'" in violations[0]["message"]

    ok = [{"profile": "P_Z", "hops": 3, "providers": {"a": 2, "b": 1},
           "error": None}]
    violations, passed = gate.evaluate(ok)
    assert passed and violations == []


def test_requirement_beyond_every_lane_fails_zero_hops(tmp_path):
    """RED arm 1: a profile no lane can serve resolves 0 hops -> exit non-zero,
    violation names the profile + no-hops code."""
    proc = _run_gate(_fixture(tmp_path, level=9), "--json")
    assert proc.returncode != 0, f"0-hop fixture must FAIL:\n{proc.stdout}"
    payload = json.loads(proc.stdout)
    codes = {v["code"] for v in payload["violations"]}
    assert "no-hops" in codes
    assert "P_FIXTURE" in {v["profile"] for v in payload["violations"]}
    assert payload["passed"] is False


def test_single_provider_chain_fails(tmp_path):
    """RED arm 2: a resolvable 1-lane chain has 1 hop on 1 provider -> exit
    non-zero, violation names the profile + single-provider code."""
    proc = _run_gate(_fixture(tmp_path, level=0), "--json")
    assert proc.returncode != 0, f"1-provider fixture must FAIL:\n{proc.stdout}"
    payload = json.loads(proc.stdout)
    codes = {v["code"] for v in payload["violations"]}
    assert "single-provider" in codes
    assert "P_FIXTURE" in {v["profile"] for v in payload["violations"]}
    assert payload["passed"] is False


def test_gate_never_writes_production_state(tmp_path):
    """Hermeticity: the gate's policy plane must live in a scratch dir, not the
    deployment state dirs (it resolves through router_spawn.resolve)."""
    for marker in ("quota-state.json", "health-state.json", "circuit-state.json"):
        for prod_dir in (os.path.expanduser("~/.hermes/model-router"),
                         os.path.expanduser("~/.local/share/task-router")):
            path = os.path.join(prod_dir, marker)
            if os.path.exists(path):
                # pre-existing production files are fine — the gate must not
                # have UPDATED them during this run; compare mtime before/after.
                before = os.stat(path).st_mtime_ns
                proc = _run_gate(_fixture(tmp_path, level=0))
                after = os.stat(path).st_mtime_ns
                assert proc is not None
                assert before == after, f"gate mutated production state: {path}"
