"""TR-030 UX commands — router_status / router_estimate / router_diff.

Hermetic contract tests: every run points the scripts at fixture registries,
fixture state dirs and fixture chains snapshots under tmp_path (env overrides
the scripts already honor: ROUTING_REGISTRY, ROUTING_DATA_DIR,
ROUTER_STATE_DIR, LEDGER_FILE, TASK_ROUTER_HOME, ROUTING_DOCS_DIR).
No network, no real ~/.hermes state, no repo registry.

Locked in:
- status --format json is pure JSON with registry/health/quota/circuit/
  in_flight/gaps/gates keys, exit 0 ALWAYS (missing registry, broken state
  dir) — fail-open like router_spawn.
- status circuit/ledger semantics match router_circuit/router_ledger (OPEN =
  open_until in the future; fresh 'started' trace = in flight).
- estimate costs are real multiplications of the fixture public prices
  (hand-checked: 0.2M in * $0.25/M + 0.1M out * $0.50/M = $0.10) with
  PAYG-vs-subscription labels from providers.plan; resolver errors stay
  fail-open (structured error, exit 0).
- diff parses the real docs/chains-<date>.md snapshot format (head moves,
  new/dropped lanes, price deltas); a missing snapshot is a clean exit 2
  with an empty stdout — never a traceback, never a fabricated diff.
"""
import json
import os
import subprocess
import sys
import datetime

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = "/home/kara/.hermes/venvs/board/bin/python3"


def _run(script, argv, monkeypatch, tmp_path, env=None):
    full = {}
    monkeypatch.setenv("ROUTING_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("ROUTING_DATA_DIR", str(tmp_path / "tables"))
    monkeypatch.setenv("ROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LEDGER_FILE", str(tmp_path / "state" / "ledger.jsonl"))
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ROUTING_DOCS_DIR", str(tmp_path / "docs"))
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return subprocess.run(
        [PY, os.path.join(REPO, "scripts", script)] + list(argv),
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )


def _write_registry(tmp_path, tables):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({
        "version": 3, "generated_at": "2026-09-01T00:00:00+00:00",
        "source": "fixture", "tables": tables}))
    return reg


EST_TABLES = {
    "providers": [
        {"id": "sub-cloud", "plan": "Max $100", "archive": False},
        {"id": "metered", "plan": "PAYG", "archive": False},
    ],
    "projects": [{"id": "proj-x", "profile": "P_EST"}],
    "task_profiles": [{"id": "P_EST", "title": "estimate fixture"}],
    "task_profile_requirements": [
        {"task_id": "P_EST", "category": "agent_tick", "level": 1}],
    "model_tier": [
        {"model": "mod-a", "category": "agent_tick", "tier": 3},
        {"model": "mod-b", "category": "agent_tick", "tier": 3},
    ],
    "models": [
        {"provider": "sub-cloud", "model": "mod-a",
         "normalized_price": 0.33, "public_price": 0.33,
         "public_in_per_m": 0.25, "public_out_per_m": 0.5,
         "plan_tier": 1, "token_factor": 1.0, "archive": False,
         "valid_to": None, "disabled": None},
        {"provider": "metered", "model": "mod-b",
         "normalized_price": 1.0, "public_price": 1.0,
         "public_in_per_m": 0.8, "public_out_per_m": 2.0,
         "plan_tier": 1, "token_factor": 1.0, "archive": False,
         "valid_to": None, "disabled": None},
    ],
}


def _write_state(tmp_path, circuit=True, quota=True, health=True, ledger=True):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    if circuit:
        (d / "circuit-state.json").write_text(json.dumps({
            "version": 1, "pairs": {
                "dead-prov/broken": {"failures": 3,
                                     "open_until": "2099-01-01T00:00:00+00:00",
                                     "reason": "test"},
                "old-prov/thing": {"failures": 1,
                                   "open_until": "2020-01-01T00:00:00+00:00",
                                   "reason": "cooled"}}}))
    if quota:
        (d / "quota-state.json").write_text(json.dumps({
            "updated": "test", "providers": {
                "dead-prov": {"status": "gated", "reason": "fixture gate"},
                "sub-cloud": {"status": "open"},
                # router_spawn treats an ABSENT provider as gated (quota-state
                # is the whitelist) — metered must be explicitly open to chain
                "metered": {"status": "open"}}}))
    if health:
        (d / "health-state.json").write_text(json.dumps({
            "updated": "2026-09-01T00:00:00+00:00", "probe_version": 3,
            "providers": {
                "sub-cloud": {"status": "OK", "latency_ms": 100,
                              "models": {"mod-a": {"status": "OK"}}},
                "dead-prov": {"status": "DOWN", "latency_ms": None,
                              "models": {"broken": {"status": "SLOW"}}}}}))
    if ledger:
        now = datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds")
        (d / "ledger.jsonl").write_text(
            json.dumps({"trace_id": "t1", "provider": "sub-cloud",
                        "model": "mod-a", "outcome": "started",
                        "ts": now}) + "\n")
    return d


# ------------------------------------------------------------------ status ----

def test_status_json_full_keys(monkeypatch, tmp_path):
    """All sections present, correct counts, pure JSON, exit 0."""
    _write_registry(tmp_path, EST_TABLES)
    _write_state(tmp_path)
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "models.jsonl").write_text(
        json.dumps({"provider": "p", "model": "gapmodel"}) + "\n")
    proc = _run("router_status.py", ["--format", "json"], monkeypatch, tmp_path)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)  # pure JSON, nothing else on stdout
    for key in ("registry", "health", "quota", "circuit", "in_flight",
                "gaps", "gates", "generated_at"):
        assert key in doc, f"missing key {key}"
    reg = doc["registry"]
    assert reg["unavailable"] is False
    assert reg["generated_at"] == "2026-09-01T00:00:00+00:00"
    assert reg["generated_at_source"] == "metadata"
    assert reg["counts"]["active"] == 2
    assert doc["circuit"]["open"] == 1          # 2099 open_until
    assert doc["circuit"]["cooling"] == 1       # 2020 open_until
    assert doc["quota"]["gated"] == 1
    assert doc["quota"]["gated_providers"][0]["provider"] == "dead-prov"
    assert doc["health"]["ok"] == 1 and doc["health"]["down"] == ["dead-prov"]
    assert doc["health"]["models_slow"] == 1
    inf = doc["in_flight"]
    assert inf["wired"] is True and inf["total"] == 1
    assert inf["pairs"][0]["pair"] == "sub-cloud/mod-a"
    assert doc["gaps"]["total_models"] == 1 and doc["gaps"]["gapped"] == 1
    gate_by = {g["provider"]: g for g in doc["gates"]["providers"]}
    assert gate_by["sub-cloud"]["health"] == "OK"
    assert gate_by["sub-cloud"]["quota"] == "open"
    assert gate_by["sub-cloud"]["in_flight"] == 1
    assert gate_by["dead-prov"]["quota"] == "gated"
    assert gate_by["dead-prov"]["circuit_open"] == 1
    assert gate_by["metered"]["plan"] == "PAYG"


def test_status_missing_registry_marks_unavailable_exit0(monkeypatch, tmp_path):
    """ROUTING_REGISTRY pointing nowhere → unavailable=True + data/tables
    fallback reported loudly, exit 0 (fail-open status tool)."""
    monkeypatch.setenv("ROUTING_REGISTRY",
                       str(tmp_path / "no-such-registry.json"))
    proc = _run("router_status.py", ["--format", "json"], monkeypatch, tmp_path,
                env={"ROUTING_REGISTRY": str(tmp_path / "no-such-registry.json")})
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["registry"]["unavailable"] is True
    assert doc["registry"]["source"] == "data/tables"
    assert doc["registry"]["fallback_used"] is True
    assert doc["registry"]["warning"]


def test_status_text_overview(monkeypatch, tmp_path):
    """--format text = human table with the per-provider gate section."""
    _write_registry(tmp_path, EST_TABLES)
    _write_state(tmp_path)
    proc = _run("router_status.py", ["--format", "text"], monkeypatch, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.startswith("task-router status")
    assert "per-provider gates" in out
    assert "sub-cloud" in out and "dead-prov" in out
    assert "gaps" in out
    try:
        json.loads(out)
        raise AssertionError("text format must not be JSON")
    except json.JSONDecodeError:
        pass


def test_status_exit0_with_broken_state_dir(monkeypatch, tmp_path):
    """ROUTER_STATE_DIR pointing at a FILE → every state section degrades,
    exit 0 still (status never crashes on a broken environment)."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    proc = _run("router_status.py", ["--format", "json"], monkeypatch, tmp_path,
                env={"ROUTER_STATE_DIR": str(blocker)})
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["circuit"]["present"] is False
    assert doc["in_flight"]["present"] is False
    assert doc["quota"]["present"] is False


# ---------------------------------------------------------------- estimate ----

def test_estimate_costs_are_real_math(monkeypatch, tmp_path):
    """Per-hop $ = tokens/1e6 * public prices — hand-checked fixture math:
    mod-a: 0.2M in * $0.25/M + 0.1M out * $0.50/M  = $0.10
    mod-b: 0.2M in * $0.80/M + 0.1M out * $2.00/M  = $0.36
    head = cheapest (mod-a); top = next hops; billing from providers.plan."""
    _write_registry(tmp_path, EST_TABLES)
    _write_state(tmp_path)
    proc = _run("router_estimate.py",
                ["--project", "proj-x", "--tokens-in", "200000",
                 "--tokens-out", "100000", "--json"],
                monkeypatch, tmp_path)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert "error" not in doc, doc.get("error")
    head = doc["head"]
    assert head["provider"] == "sub-cloud" and head["model"] == "mod-a"
    assert head["billing"] == "subscription"
    assert head["plan"] == "Max $100"
    assert head["cost_usd"] == pytest.approx(0.2 * 0.25 + 0.1 * 0.5)
    assert head["estimate_basis"] == "public_in_out_split"
    top = doc["top"]
    assert len(top) == 1
    assert top[0]["provider"] == "metered"
    assert top[0]["billing"] == "payg"
    assert top[0]["cost_usd"] == pytest.approx(0.2 * 0.8 + 0.1 * 2.0)
    assert doc["totals"]["chain_cost_usd"] == pytest.approx(0.10 + 0.36)
    assert doc["totals"]["head_cost_usd"] == pytest.approx(0.10)
    assert doc["totals"]["payg_hops"] == 1
    assert doc["totals"]["subscription_hops"] == 1
    assert doc["chain_estimated"] == 2
    assert doc["unpriced_hops"] == []
    # the spawn subprocess must not leak a traceback into stderr
    assert "Traceback" not in proc.stderr


def test_estimate_default_tokens(monkeypatch, tmp_path):
    """No token flags → 100000 in / 100000 out (documented defaults)."""
    _write_registry(tmp_path, EST_TABLES)
    _write_state(tmp_path)
    proc = _run("router_estimate.py", ["--project", "proj-x", "--json"],
                monkeypatch, tmp_path)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["tokens_in"] == 100000 and doc["tokens_out"] == 100000
    # 0.1M * 0.25 + 0.1M * 0.5 = 0.075
    assert doc["head"]["cost_usd"] == pytest.approx(0.075)


def test_estimate_unknown_project_failopen(monkeypatch, tmp_path):
    """Unknown project → resolver error surfaced structurally, exit 0."""
    _write_registry(tmp_path, EST_TABLES)
    proc = _run("router_estimate.py", ["--project", "ghost", "--json"],
                monkeypatch, tmp_path)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc.get("error")
    assert "ghost" in doc["error"]
    assert doc["head"] is None and doc["top"] == []
    assert "Traceback" not in proc.stderr


# -------------------------------------------------------------------- diff ----

CHAINS_OLD = """# Chains snapshot — 2026-08-30T10:00+00:00

Eligibility: model must clear EVERY category requirement of the profile (tier >= level).
Order: plan_tier ASC, effective $/M ASC, model/provider tie-break. Health/circuit/quota exclusions NOT applied here (see state/).

## P_A — Alpha profile
profile: agent_tick=++ delegation=+
  1. $ 0.100/M  prov-a/old-head
  2. $ 0.200/M  prov-b/keep
  3. $ 0.300/M  prov-c/dropme

## P_ONLY_OLD — gone tomorrow
profile: review=+
  1. $ 0.050/M  prov-a/lonely
"""

CHAINS_NEW = """# Chains snapshot — 2026-08-31T10:00+00:00

Eligibility: model must clear EVERY category requirement of the profile (tier >= level).
Order: plan_tier ASC, effective $/M ASC, model/provider tie-break. Health/circuit/quota exclusions NOT applied here (see state/).

## P_A — Alpha profile
profile: agent_tick=++ delegation=+
  1. $ 0.150/M  prov-b/keep
  2. $ 0.250/M  prov-d/newkid
"""


def _write_chains(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "chains-2026-08-30.md").write_text(CHAINS_OLD)
    (docs / "chains-2026-08-31.md").write_text(CHAINS_NEW)
    return docs


def test_diff_head_move_lanes_prices(monkeypatch, tmp_path):
    """Real snapshot format: head move, new/dropped lanes, price delta."""
    _write_chains(tmp_path)
    proc = _run("router_diff.py", ["2026-08-30", "2026-08-31", "--json"],
                monkeypatch, tmp_path)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    by_id = {p["profile"]: p for p in doc["profiles"]}
    pa = by_id["P_A"]
    assert pa["head_move"] == {"from": "prov-a/old-head", "to": "prov-b/keep",
                               "from_price": 0.1, "to_price": 0.15}
    assert pa["new_lanes"] == ["prov-d/newkid"]
    assert pa["dropped_lanes"] == ["prov-a/old-head", "prov-c/dropme"]
    assert pa["price_changes"] == [
        {"lane": "prov-b/keep", "from": 0.2, "to": 0.15, "delta": -0.05}]
    assert by_id["P_ONLY_OLD"]["note"] == "profile absent from 2026-08-31 snapshot"
    t = doc["totals"]
    # dropped = 2 lanes of P_A + prov-a/lonely (its whole profile vanished —
    # still a dropped lane, the per-profile `note` explains why)
    assert t == {"profiles_compared": 1, "head_moves": 1, "new_lanes": 1,
                 "dropped_lanes": 3, "price_changes": 1}
    assert doc["from"] == "2026-08-30" and doc["to"] == "2026-08-31"


def test_diff_missing_file_clean_exit2(monkeypatch, tmp_path):
    """Missing snapshot → stderr message + exit 2 + EMPTY stdout."""
    _write_chains(tmp_path)
    proc = _run("router_diff.py", ["2026-08-30", "2026-09-09", "--json"],
                monkeypatch, tmp_path)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "chains-2026-09-09.md" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_diff_same_content_no_changes(monkeypatch, tmp_path):
    """Identical snapshots → zero totals (no phantom diffs)."""
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "chains-2026-08-30.md").write_text(CHAINS_NEW)
    (docs / "chains-2026-08-31.md").write_text(CHAINS_NEW)
    proc = _run("router_diff.py", ["2026-08-30", "2026-08-31", "--json"],
                monkeypatch, tmp_path)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["totals"]["head_moves"] == 0
    assert doc["totals"]["new_lanes"] == 0
    assert doc["totals"]["dropped_lanes"] == 0
    assert doc["totals"]["price_changes"] == 0
    assert doc["profiles"][0]["head_move"] is None


def test_diff_real_repo_snapshots(monkeypatch, tmp_path):
    """The live repo docs/ snapshots parse (skips gracefully when fewer than
    two dated snapshots exist — e.g. a fresh clone)."""
    names = [f for f in sorted(os.listdir(os.path.join(REPO, "docs")))
             if f.startswith("chains-") and f.endswith(".md")]
    if len(names) < 2:
        pytest.skip("fewer than two chains snapshots in docs/")
    old = names[0][len("chains-"):-len(".md")]
    new = names[-1][len("chains-"):-len(".md")]
    monkeypatch.setenv("ROUTING_DOCS_DIR", os.path.join(REPO, "docs"))
    proc = _run("router_diff.py", [old, new, "--json"],
                monkeypatch, tmp_path, env={"ROUTING_DOCS_DIR":
                                            os.path.join(REPO, "docs")})
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["from"] == old and doc["to"] == new
    assert doc["totals"]["profiles_compared"] >= 1
