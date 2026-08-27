"""TR-012 regression battery — locks in the behaviors proven by the CI + E2E
pass (2026-08-27) so future changes cannot silently break the router.

Covers: chain invariants per profile (against the COMMITTED data/tables),
gate semantics (including the absent-state fail-closed CI regression),
ordering/tie-break semantics, registry cross-table integrity, fresh-clone
stdlib fallback, and seed idempotency (duckdb-gated).
"""
import datetime
import importlib.util
import json
import os
import subprocess
import sys

import pytest

_HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import router_spawn  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, "data", "tables")

ALL_CATS = {"code_gen", "debug", "refactor", "terminal", "mechanical", "test", "schema",
            "reasoning", "math", "agent_tick", "tool_use", "delegation", "long_horizon", "guard",
            "vision", "e2e_vision", "ui_frontend", "long_doc", "spec_docs", "creative",
            "multilingual", "review", "security", "mock"}

CATS = ("agent_tick", "debug")  # fixture categories (mirror test_diversity)


# ---------------------------------------------------------------- fixtures ----

def _load_tables():
    """Committed registry data as {table: [row...]} (the spawn fallback path)."""
    tables = {}
    for fn in sorted(os.listdir(DATA_DIR)):
        if fn.endswith(".jsonl"):
            name = fn[: -len(".jsonl")]
            rows = [json.loads(l) for l in open(os.path.join(DATA_DIR, fn)) if l.strip()]
            tables[name] = rows
    return tables


def _state_dir(tmp_path, providers=None, health=None, circuit=None):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"updated": "test", "providers": providers if providers is not None else {}},
              open(d / "quota-state.json", "w"))
    json.dump({"providers": health if health is not None else {}},
              open(d / "health-state.json", "w"))
    json.dump({"pairs": circuit if circuit is not None else {}},
              open(d / "circuit-state.json", "w"))
    return str(d)


def _open_providers(tables):
    """All registry providers open (grok-build/crof too — chain invariants want
    zero gate interference)."""
    return {r["id"]: {"status": "open"} for r in tables["providers"]}


_PROD_GATED = {"grok-build", "crof"}  # quota policy (mirrors live quota-state)
_PROD_DOWN = {"clinepass", "groq", "kimi-for-coding", "openai-codex", "xai"}  # health (mirrors live health-state)


def _prod_state(tmp_path, tables):
    """Hermetic state mirroring TODAY's production gates (2026-08-27), so
    golden heads match the live sweep. Deliberately hardcoded: when the fleet's
    gates change, these golden tests must be updated intentionally."""
    provs = {r["id"]: ({"status": "gated", "reason": "prod-mirror"} if r["id"] in _PROD_GATED
                       else {"status": "open"})
             for r in tables["providers"]}
    health = {p: ({"status": "DOWN", "ts": "2026-08-27T15:00:36+00:00", "error": "mirror"}
                  if p in _PROD_DOWN else {"status": "OK", "latency_ms": 500})
              for p in provs}
    return _state_dir(tmp_path, providers=provs, health=health)


def _resolve(monkeypatch, tmp_path, tables, project="coding-hermes-scheduler", profile=None):
    """Write tables to a temp registry.json and point REGISTRY at it.

    MR is NOT touched here — each test sets the state dir itself (a helper
    that clobbers MR caused all-gated empty chains in the first version).
    """
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    monkeypatch.setattr(router_spawn, "REGISTRY", str(reg))
    if profile:
        return router_spawn.resolve(profile_id=profile)
    return router_spawn.resolve(project=project)


def _pair(e):
    return f"{e['provider']}/{e['model']}"


def _excluded_pairs(r):
    return {_pair(e) for e in r["exclusions"]}


# ------------------------------------------------ chain invariants (committed) --

PROFILES = ["P0_FORE", "P1_CODING", "P2_AGENTIC", "P3_DOCS", "P4_SECURITY",
            "P5_VISION_E2E", "P7_MOCK", "P9_REVIEW"]


@pytest.mark.parametrize("pid", PROFILES)
def test_chain_invariants_per_profile(monkeypatch, tmp_path, pid):
    """For every profile: non-empty, dominance respected, order = (plan_tier,
    price) lexicographic, no dup pairs, contiguous hops."""
    tables = _load_tables()
    state = _prod_state(tmp_path, tables)
    monkeypatch.setattr(router_spawn, "MR", state)
    r = _resolve(monkeypatch, tmp_path, tables, profile=pid)
    assert "error" not in r, r.get("error")
    chain = r["chain"]
    assert chain, f"{pid}: chain must not be empty"
    reqs = [(x["category"], x["level"])
            for x in tables["task_profile_requirements"] if x["task_id"] == pid]
    assert reqs, f"{pid}: profile must have requirements"
    tier = {(t["provider"], t["model"], t["category"]): t["tier"]
            for t in tables["model_tier"]}
    prices = {}
    for m in tables["models"]:
        prices[f'{m["provider"]}/{m["model"]}'] = m["normalized_price"]
    plan = {}
    for m in tables["models"]:
        plan[f'{m["provider"]}/{m["model"]}'] = m.get("plan_tier") or 0
    seen = set()
    prev_key = None
    prev_hop = 0
    for e in chain:
        assert e["hop"] > prev_hop, f"{pid}: hops must be strictly increasing (pre-gate positions; gates skip)"
        prev_hop = e["hop"]
        pair = _pair(e)
        assert pair not in seen, f"{pid}: duplicate pair {pair}"
        seen.add(pair)
        # dominance: every requirement must be cleared
        for cat, lvl in reqs:
            t = tier.get((e["provider"], e["model"], cat))
            assert t is not None, f"{pid}: {pair} missing tier for {cat}"
            assert t >= lvl, f"{pid}: {pair} tier {t} < req {lvl} for {cat}"
        # order: (plan_tier, price) non-decreasing
        key = (plan[pair], prices[pair])
        if prev_key is not None:
            assert key >= prev_key, f"{pid}: order violation {prev_key} -> {key} at {pair}"
        prev_key = key
    assert _pair(r["head"]) == chain[0]["provider"] + "/" + chain[0]["model"]


@pytest.mark.parametrize("pid,head", [
    ("P0_FORE", "opencode-go/mimo-v2.5"),
    ("P1_CODING", "opencode-go/mimo-v2.5"),
    ("P2_AGENTIC", "opencode-go/mimo-v2.5"),
    ("P4_SECURITY", "ollama-cloud/glm-5.2"),
])
def test_golden_fixed_point_heads(monkeypatch, tmp_path, pid, head):
    """Known heads as of 2026-08-27 (intentional reprice/new-model changes must
    update these deliberately — this test exists to catch ACCIDENTAL drift)."""
    tables = _load_tables()
    state = _prod_state(tmp_path, tables)
    monkeypatch.setattr(router_spawn, "MR", state)
    r = _resolve(monkeypatch, tmp_path, tables, profile=pid)
    assert _pair(r["head"]) == head


def test_adhoc_profile_invariants(monkeypatch, tmp_path):
    tables = _load_tables()
    state = _prod_state(tmp_path, tables)
    monkeypatch.setattr(router_spawn, "MR", state)
    r = router_spawn.resolve(adhoc=["reasoning=5", "debug=3"])
    assert "error" not in r
    chain = r["chain"]
    for e in chain:
        assert e["usd_1m"] > 0
    # no duplicates
    pairs = [_pair(e) for e in chain]
    assert len(pairs) == len(set(pairs))


# ----------------------------------------------------------- gate semantics ----

def test_absent_state_is_fail_closed_all_excluded(monkeypatch, tmp_path):
    """CI regression (2026-08-27): a missing router-state dir means EVERY
    provider is quota-gated (absent != open) -> empty chain, never a blind
    spawn on an unverified provider."""
    tables = _load_tables()
    monkeypatch.setattr(router_spawn, "MR", str(tmp_path / "no-such-state"))
    r = _resolve(monkeypatch, tmp_path, tables)
    assert r["head"] is None
    assert r["chain"] == []
    assert len(r["exclusions"]) == len(tables["providers"])
    reasons = [w for e in r["exclusions"] for w in e["why"]]
    assert any("quota" in w or "gate" in w for w in reasons)


def test_quota_gated_provider_excluded(monkeypatch, tmp_path):
    tables = _load_tables()
    provs = _open_providers(tables)
    provs["opencode-go"] = {"status": "gated", "reason": "policy"}
    state = _state_dir(tmp_path, providers=provs)
    monkeypatch.setattr(router_spawn, "MR", state)
    r = _resolve(monkeypatch, tmp_path, tables, profile="P1_CODING")
    assert all(not _pair(e).startswith("opencode-go/") for e in r["chain"])
    assert any(e["provider"] == "opencode-go" for e in r["exclusions"])


def test_health_down_provider_excluded(monkeypatch, tmp_path):
    tables = _load_tables()
    state = _prod_state(tmp_path, tables)
    # flip the head provider to DOWN (uppercase — the live probe's status)
    with open(os.path.join(state, "health-state.json")) as f:
        h = json.load(f)
    h["providers"]["opencode-go"] = {"status": "DOWN", "ts": "2026-08-27T16:00:00+00:00"}
    with open(os.path.join(state, "health-state.json"), "w") as f:
        json.dump(h, f)
    monkeypatch.setattr(router_spawn, "MR", state)
    r = _resolve(monkeypatch, tmp_path, tables, profile="P1_CODING")
    assert all(not _pair(e).startswith("opencode-go/") for e in r["chain"])
    assert any(e["provider"] == "opencode-go" for e in r["exclusions"])


def test_circuit_open_exclusion_reason_format(monkeypatch, tmp_path):
    """Breaker regression: exact reason format 'circuit OPEN until ... (N failures)'.
    open_until must be in the FUTURE (ISO strings compare lexicographically)."""
    tables = _load_tables()
    future = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(seconds=300)).isoformat(timespec="seconds")
    state = _prod_state(tmp_path, tables)
    with open(os.path.join(state, "circuit-state.json")) as f:
        c = json.load(f)
    c["pairs"]["opencode-go/mimo-v2.5"] = {"failures": 1, "open_until": future}
    with open(os.path.join(state, "circuit-state.json"), "w") as f:
        json.dump(c, f)
    monkeypatch.setattr(router_spawn, "MR", state)
    r = _resolve(monkeypatch, tmp_path, tables, profile="P1_CODING")
    for e in r["exclusions"]:
        if _pair(e) == "opencode-go/mimo-v2.5":
            assert any(f"circuit OPEN until {future} (1 failures)" in w for w in e["why"]), e
            return
    pytest.fail("exclusion for open pair missing")


# ------------------------------------------------------ ordering / tie-breaks ----

def _mini_registry(models, reqs, profile_caps=None):
    """registry tables dict; models: (provider, model, price, plan_tier)."""
    cats = ("agent_tick", "debug")
    tables = {
        "models": [{"provider": p, "model": m, "normalized_price": pr,
                    "token_factor": 1.0, "plan_tier": pt, "data_class": "zdr",
                    "valid_to": None, "archive": False}
                   for p, m, pr, pt in models],
        "model_tier": [{"provider": p, "model": m, "category": c, "tier": 5}
                       for p, m, _, _ in models for c in cats],
        "task_profiles": [{"id": "TP", "title": "t", "created_at": None,
                           "max_consecutive_per_provider": None, "max_total_per_provider": None}],
        "task_profile_requirements": [{"task_id": "TP", "category": c, "level": lvl}
                                      for c, lvl in reqs.items()],
        "projects": [{"id": "proj", "profile": "TP", "sensitivity": None}],
    }
    return tables


def test_plan_tier_dominates_price(monkeypatch, tmp_path):
    """ORDER BY plan_tier ASC, price ASC — a plan_tier=-1 model at ANY price
    outranks plan_tier=0 models (the v_task_chain semantics, TR-012 lock)."""
    tables = _mini_registry([("prov-a", "a1", 100.0, -1), ("prov-b", "b1", 1.0, 0),
                             ("prov-c", "c1", 2.0, 0)], {"agent_tick": 1, "debug": 1})
    state = _state_dir(tmp_path, providers={"prov-a": {"status": "open"},
                                            "prov-b": {"status": "open"},
                                            "prov-c": {"status": "open"}})
    monkeypatch.setattr(router_spawn, "MR", state)
    r = _resolve(monkeypatch, tmp_path, tables, profile="TP")
    assert [_pair(e) for e in r["chain"]] == ["prov-a/a1", "prov-b/b1", "prov-c/c1"]


def test_price_tie_breaks_deterministically_by_model(monkeypatch, tmp_path):
    """Same plan_tier + same price → model ASC, provider ASC (stable, no
    insertion-order dependence)."""
    tables = _mini_registry([("prov-b", "b1", 1.0, 0), ("prov-a", "a2", 1.0, 0),
                             ("prov-a", "a1", 1.0, 0)], {"agent_tick": 1, "debug": 1})
    state = _state_dir(tmp_path, providers={"prov-a": {"status": "open"},
                                            "prov-b": {"status": "open"}})
    monkeypatch.setattr(router_spawn, "MR", state)
    r = _resolve(monkeypatch, tmp_path, tables, profile="TP")
    assert [_pair(e) for e in r["chain"]] == ["prov-a/a1", "prov-a/a2", "prov-b/b1"]


# -------------------------------------------------------- registry integrity ----

def test_registry_integrity():
    tables = _load_tables()
    assert set(tables) == {"providers", "models", "archetypes", "benchmarks", "projects",
                           "level_defs", "category_levels", "model_perf", "model_tier",
                           "task_profiles", "task_profile_requirements"}
    models = tables["models"]
    active = [m for m in models if not m.get("archive") and m.get("valid_to") is None]
    assert len(models) >= 50
    assert len(active) >= 50
    # model_perf: every category × every active model (24 × N)
    perfs = tables["model_perf"]
    cats_in_perf = {p["category"] for p in perfs}
    assert cats_in_perf == ALL_CATS, cats_in_perf - ALL_CATS
    per_key = {(p["provider"], p["model"]) for p in perfs}
    act_key = {(m["provider"], m["model"]) for m in active}
    assert per_key == act_key, f"perf rows missing for {act_key - per_key} or orphans {per_key - act_key}"
    assert len(perfs) == 24 * len(active)
    # model_tier: only known pairs/categories, levels within -5..+5
    tier = tables["model_tier"]
    assert all((t["provider"], t["model"]) in act_key for t in tier)
    assert all(t["category"] in ALL_CATS for t in tier)
    assert all(-5 <= t["tier"] <= 5 for t in tier)
    # profiles + requirements
    profs = {p["id"] for p in tables["task_profiles"]}
    assert len(profs) == 8
    reqs = tables["task_profile_requirements"]
    assert all(r["task_id"] in profs for r in reqs)
    assert all(r["category"] in ALL_CATS for r in reqs)
    # projects reference valid profiles
    assert all(p.get("profile") in profs for p in tables["projects"])
    # prices sane
    assert all((m.get("normalized_price") or 0) >= 0 for m in models)
    assert all((m.get("token_factor") or 1) > 0 for m in models)


# ------------------------------------------ fresh-clone stdlib fallback (TR-010) --

def test_stdlib_fallback_when_registry_missing(monkeypatch, tmp_path):
    """TR-010 regression: registry.json absent + committed data/tables present
    → resolve works with no seed run (this is the CI smoke path)."""
    monkeypatch.setattr(router_spawn, "REGISTRY", str(tmp_path / "no-registry.json"))
    tables = _load_tables()
    state = _state_dir(tmp_path, providers=_open_providers(tables))
    monkeypatch.setattr(router_spawn, "MR", state)
    r = router_spawn.resolve(project="coding-hermes-scheduler")
    assert "error" not in r
    assert r["head"] is not None


# -------------------------------------------------- seed idempotency (duckdb) ----

@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_seed_is_idempotent(tmp_path):
    """TR-009/010 regression: two consecutive seed runs (same inputs) produce
    identical registry tables — profile created_at preserved, deterministic
    ordering. Guards against the now() stamping bug found 2026-08-27.
    Both runs use the SAME registry path (the production shape: one live
    store, re-seeded in place)."""
    import shutil

    seed = os.path.join(REPO, "scripts", "router_seed.py")
    env = dict(os.environ)
    reg = tmp_path / "registry.json"
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    ns = tmp_path / "ns"
    env.update({"ROUTING_REGISTRY": str(reg), "ROUTING_DATA_DIR": str(data),
                "ROUTING_NS": str(ns)})
    for _ in range(2):
        p = subprocess.run([sys.executable, seed], capture_output=True, text=True, env=env, timeout=180)
        assert p.returncode == 0, p.stderr[-500:]
    a = json.load(open(reg))
    b = json.load(open(reg))
    assert a["tables"] == b["tables"], "seed is not idempotent — tables differ between runs"
