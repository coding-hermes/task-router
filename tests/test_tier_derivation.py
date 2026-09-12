"""TR-039 — tier derivation for vendor-prefixed providers (commandcode /
aws-bedrock / fireworks-ai).

Hermetic tests (the seed is run against a copied data dir + scratch registry):
  - a `test`-keyed quality estimate becomes a model_perf row for a real lane
  - legacy-only estimate rows (guard/mock/multilingual) still insert NO perf row
  - vendor-prefixed lanes of the three providers derive model_tier rows and
    stop emitting ROUTER-MISS telemetry on resolve
"""
import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
PY = sys.executable
TARGETS = ("commandcode", "aws-bedrock", "fireworks-ai")
# one representative lane per provider (verified live in models.jsonl)
SAMPLE_LANES = {
    "commandcode": "MiniMaxAI/MiniMax-M3",
    "aws-bedrock": "amazon.nova-lite-v1:0",
    "fireworks-ai": "accounts/fireworks/models/glm-5p3-flash",
}


def _run(*args, timeout=180, env_extra=None):
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
            "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
            "ROUTER_SEED_PYTHON": PY, "ROUTING_BOARD_PY": PY}


def _seed(env):
    p = _run(os.path.join(SCRIPTS, "router_seed.py"), env_extra=env)
    assert p.returncode == 0, p.stderr[-800:]
    with open(env["ROUTING_REGISTRY"]) as f:
        return json.load(f)


def _tier_rows(reg):
    return {(r["model"], r["category"]): r["tier"] for r in reg["tables"]["model_tier"]}


def test_target_provider_samples_derive_tier_rows(tmp_path):
    """Every target provider's lane has a model_tier row it can resolve from."""
    reg = _seed(_hermetic_env(tmp_path))
    tiers = _tier_rows(reg)
    for provider, lane in SAMPLE_LANES.items():
        row = [t for (m, c), t in tiers.items() if m == lane and c == "test"]
        assert row, f"{provider}: no 'test' tier row for {lane}"
        assert row[0] >= 0, f"{provider}: {lane} tier {row[0]} below the >=0 bar"


def test_target_providers_emit_no_router_miss(tmp_path):
    """Resolve emits zero ROUTER-MISS lines for the three providers.

    Uses the board's sanity project (coding-hermes-scheduler, P1_CODING): its
    only positive bar is `test>=0`, which is the bar the vendor lanes fail on
    tier absence. Other profiles add bars (agent_tick/delegation/…) that these
    lanes legitimately miss — that is not the defect TR-039 fixes.
    """
    env = _hermetic_env(tmp_path)
    _seed(env)
    p = _run(os.path.join(SCRIPTS, "router_spawn.py"), "coding-hermes-scheduler",
             "--format", "json", env_extra=env)
    assert p.returncode == 0, p.stderr[-400:]
    misses = [l for l in p.stderr.splitlines()
              if "ROUTER-MISS" in l and any(f"{t}/" in l for t in TARGETS)]
    assert not misses, f"unexpected ROUTER-MISS lines: {misses[:5]}"


def test_estimate_insertion_only_for_real_lanes(tmp_path):
    """A `test` key inserts a perf row for a live lane; a non-lane name is ignored."""
    env = _hermetic_env(tmp_path)
    qe = os.path.join(env["ROUTING_DATA_DIR"], "quality_estimates.jsonl")
    with open(qe, "a") as f:
        f.write(json.dumps({"model": "amazon.nova-micro-v1:0", "test": 0.72,
                            "note": "TR-039 test fixture"}) + "\n")
        f.write(json.dumps({"model": "no-such-lane-xyz", "test": 0.72,
                            "note": "not a registry model"}) + "\n")
    reg = _seed(env)
    perf = {(r["model"], r["category"]) for r in reg["tables"]["model_perf"]}
    assert ("amazon.nova-micro-v1:0", "test") in perf
    assert ("no-such-lane-xyz", "test") not in perf


def test_category_estimates_never_override_existing_perf(tmp_path):
    """An estimate must not shadow measured evidence already present."""
    env = _hermetic_env(tmp_path)
    qe = os.path.join(env["ROUTING_DATA_DIR"], "quality_estimates.jsonl")
    with open(qe, "a") as f:
        f.write(json.dumps({"model": "deepseek-v4-flash", "test": 0.05,
                            "note": "would-be downgrade"}) + "\n")
    reg = _seed(env)
    perf = {(r["model"], r["category"]): r["perf"]
            for r in reg["tables"]["model_perf"]}
    assert perf[("deepseek-v4-flash", "test")] > 0.05
