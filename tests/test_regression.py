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
    tier = {(t["model"], t["category"]): t["tier"]
            for t in tables["model_tier"]}
    prices = {}
    for m in tables["models"]:
        prices[f'{m["provider"]}/{m["model"]}'] = m["normalized_price"]
    plan = {}
    for m in tables["models"]:
        # mirror _build_chain: NULL plan_tier sorts LAST (1<<30), never 0
        pt = m.get("plan_tier")
        plan[f'{m["provider"]}/{m["model"]}'] = pt if pt is not None else (1 << 30)
    seen = set()
    prev_key = None
    prev_hop = 0
    for e in chain:
        assert e["hop"] > prev_hop, f"{pid}: hops must be strictly increasing (pre-gate positions; gates skip)"
        prev_hop = e["hop"]
        pair = _pair(e)
        assert pair not in seen, f"{pid}: duplicate pair {pair}"
        seen.add(pair)
        # dominance: every requirement must be cleared — a tier row >= lvl,
        # OR (BLANK default, Bane 2026-08-27) no tier row with lvl <= -1
        for cat, lvl in reqs:
            t = tier.get((e["model"], cat))
            if t is None:
                assert lvl <= -1, f"{pid}: {pair} BLANK tier for {cat} cannot clear {lvl}"
            else:
                assert t >= lvl, f"{pid}: {pair} tier {t} < req {lvl} for {cat}"
        # order: (plan_tier, price) non-decreasing
        key = (plan[pair], prices[pair])
        if prev_key is not None:
            assert key >= prev_key, f"{pid}: order violation {prev_key} -> {key} at {pair}"
        prev_key = key
    assert _pair(r["head"]) == chain[0]["provider"] + "/" + chain[0]["model"]


@pytest.mark.parametrize("pid,head", [
    ("P0_FORE", "ollama-cloud/deepseek-v4-flash"),
    ("P1_CODING", "opencode-go/mimo-v2.5"),
    ("P2_AGENTIC", "ollama-cloud/kimi-k2.7-code"),
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
    eligible provider is quota-gated (absent != open) -> empty chain, never a
    blind spawn on an unverified provider. Per-hop exclusions: the count must
    equal the full eligible chain (a provider with zero eligible models for
    the profile has nothing to gate and does not appear)."""
    tables = _load_tables()
    # full eligible chain with an open state dir (nothing gated)
    open_state = _state_dir(tmp_path, providers=_open_providers(tables))
    monkeypatch.setattr(router_spawn, "MR", str(open_state))
    r_open = _resolve(monkeypatch, tmp_path, tables)
    assert r_open["head"] is not None
    n_eligible = len(r_open["chain"])
    # absent state dir -> every eligible hop gated
    monkeypatch.setattr(router_spawn, "MR", str(tmp_path / "no-such-state"))
    r = _resolve(monkeypatch, tmp_path, tables)
    assert r["head"] is None
    assert r["chain"] == []
    assert len(r["exclusions"]) == n_eligible, (
        f"fail-closed must gate ALL {n_eligible} eligible hops, got {len(r['exclusions'])}"
    )
    gated = {e["provider"] for e in r["exclusions"]}
    assert gated <= {p["id"] for p in tables["providers"]}
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
        "model_tier": [{"model": m, "category": c, "tier": 5}
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
    core = {"providers", "models", "archetypes", "benchmarks", "projects",
            "level_defs", "category_levels", "model_perf", "model_tier",
            "task_profiles", "task_profile_requirements"}
    sidecars = {"model_catalog", "model_notes", "plan_terms", "temporary_discounts",
                "provider_rules", "quality_estimates", "fallback_lanes"}
    assert core <= set(tables), f"missing core tables: {core - set(tables)}"
    assert set(tables) - core <= sidecars, f"unexpected tables: {set(tables) - core - sidecars}"
    models = tables["models"]
    active = [m for m in models if not m.get("archive") and m.get("valid_to") is None]
    assert len(models) >= 50
    assert len(active) >= 50
    # model_perf: only EVIDENCED (model, category) rows — metrics ONCE PER
    # MODEL (Bane 2026-08-27), never per provider lane; BLANK default: models
    # with no data have NO perf/tier rows, resolved as -1 at chain time.
    perfs = tables["model_perf"]
    cats_in_perf = {p["category"] for p in perfs}
    assert cats_in_perf <= ALL_CATS, cats_in_perf - ALL_CATS
    assert len(cats_in_perf) >= 20, f"only {len(cats_in_perf)} categories have evidence"
    per_key = {p["model"] for p in perfs}
    act_key = {m["model"] for m in active}
    assert per_key <= act_key, f"orphan perf rows: {per_key - act_key}"
    # every model with ANY evidence must have it consistently (no dup pairs)
    assert len(perfs) == len({(p["model"], p["category"]) for p in perfs}), "dup perf cells"
    # model_tier: only known models/categories, levels within -5..+5
    tier = tables["model_tier"]
    assert all(t["model"] in act_key for t in tier)
    assert all(t["category"] in ALL_CATS for t in tier)
    assert all(-5 <= t["tier"] <= 5 for t in tier)
    # lane-disable feature: explicit + reason required; spawn skips them
    disabled = [m for m in models if m.get("disabled")]
    assert all(m.get("disabled_reason") for m in disabled), "disabled lanes must carry a reason"
    assert len(disabled) < len(active), "disabled lanes must never outnumber active lanes"
    # profiles + requirements
    profs = {p["id"] for p in tables["task_profiles"]}
    assert len(profs) >= 8
    # P6_DEFAULT (Bane 2026-08-27): the ONE default chain for most cron work
    assert "P6_DEFAULT" in profs, "P6_DEFAULT profile missing"
    reqs = tables["task_profile_requirements"]
    assert all(r["task_id"] in profs for r in reqs)
    assert all(r["category"] in ALL_CATS for r in reqs)
    # projects reference valid profiles
    assert all(p.get("profile") in profs for p in tables["projects"])
    # prices sane
    assert all((m.get("normalized_price") or 0) >= 0 for m in models)
    assert all((m.get("token_factor") or 1) > 0 for m in models)


def test_plan_included_lanes_never_disabled():
    """REGIME LOCK (Bane 2026-08-27, after flip-flop): a lane listed in a
    provider's plan_terms included_models must NEVER be disabled. The plan
    sweep disabled glm-5.3 + qwen3.8-max off a STALE 11-model plans-API list
    while docs.cline.bot said 13 — cron re-enabled, and the flip-flop must
    not recur. Also: the fallback list hardcoded in router_clinepass.py must
    stay in sync with plan_terms (it's what re-seeds when the row is lost)."""
    tables = _load_tables()
    models = tables["models"]
    terms = tables["plan_terms"]
    for t in terms:
        for mid in (t.get("included_models") or []):
            lanes = [m for m in models if m["provider"] == t["provider"] and m["model"] == mid]
            assert lanes, f"plan-included {t['provider']}/{mid} missing from registry"
            assert all(
                not m.get("disabled") for m in lanes
            ), f"plan-included lane disabled: {t['provider']}/{mid} — sweep ran on stale plan list?"
    # clinepass: provider facts (plan cost, included models, multiplier) must
    # NOT be hardcoded in scripts — they live in plan_terms.jsonl. A script
    # that fabricates a plan row from code is a regime violation (caused the
    # stale-11 flip-flop). Missing row = visible gap for the research agent.
    for script_name in ("router_clinepass.py", "router_pricing.py",
                        "router_plan_sweep.py", "router_seed.py"):
        src = open(os.path.join(REPO, "scripts", script_name)).read()
        assert "'included_models': [" not in src, (
            f"{script_name} hardcodes a plan included_models list — provider "
            f"facts belong in plan_terms.jsonl, never in code"
        )


def test_fallback_lane_fires_when_all_subs_down(monkeypatch, tmp_path):
    """Bane 2026-08-27 (cron integration): crons must ALWAYS run. When every
    eligible hop is gated/down, resolve() falls back to the designated
    always-run lane (deepseek-v4 + cron key) — never a None chain. The
    fallback must (a) clear the profile bars, (b) be priced, (c) be open."""
    tables = _load_tables()
    provs = _open_providers(tables)
    # gate EVERY provider except deepseek
    for p in provs:
        if p != "deepseek":
            provs[p] = {"status": "gated", "reason": "sim-down"}
    state = _state_dir(tmp_path, providers=provs)
    monkeypatch.setattr(router_spawn, "MR", str(state))
    r = _resolve(monkeypatch, tmp_path, tables, project="coding-hermes-scheduler")
    assert r["head"] is not None, "fallback must fire — crons always run"
    assert r["head"]["provider"] == "deepseek"
    assert r["head"]["model"] == "deepseek-v4-flash"
    assert r["head"].get("fallback") is True
    assert r["head"].get("key_env") == "DEEPSEEK_CRON_KEY"
    assert any("FALLBACK" in w for w in r["gate_reasons"])
    # fallback lane must exist in the registry table
    fbs = tables.get("fallback_lanes") or []
    assert any(f.get("provider") == "deepseek" and f.get("model") == "deepseek-v4-flash"
               for f in fbs), "fallback_lanes table missing deepseek-v4-flash lane"


def test_fallback_skipped_when_primary_chain_open(monkeypatch, tmp_path):
    """The fallback must NOT fire when subs are healthy — cheap providers
    first, deepseek only as the last resort."""
    tables = _load_tables()
    state = _state_dir(tmp_path, providers=_open_providers(tables))
    monkeypatch.setattr(router_spawn, "MR", str(state))
    r = _resolve(monkeypatch, tmp_path, tables, project="coding-hermes-scheduler")
    assert r["head"] is not None
    assert r["head"].get("fallback") is None, "primary chain open — fallback must not fire"
    assert not any("FALLBACK" in w for w in r["gate_reasons"])


def test_subscription_normalized_beats_payg_sticker():
    """SUBSCRIPTION-FIRST DOCTRINE (Bane, repeated 2026-08-27): the whole
    point of a sub is the normalized price being CHEAPER than the PAYG
    sticker — 'why get a sub if the price is going to be the same as the
    on-demand payg, better off paying for what you need'. For every
    flat_subscription provider, each active priced lane must be cheaper than
    its blended PAYG sticker (catalog cost). If this fails, the multiplier
    / pool assumption is wrong — redo the pricing, don't weaken the test."""
    tables = _load_tables()
    terms = {t["provider"]: t for t in tables["plan_terms"]
             if t.get("billing_model") == "flat_subscription"}
    assert terms, "no flat_subscription plan_terms rows — doctrine untested"
    cat = {}
    for c in tables.get("model_catalog") or []:
        if c.get("cost_input") is not None and c.get("cost_output") is not None:
            cat[(c.get("provider"), c.get("model"))] = (
                (c["cost_input"] + c["cost_output"]) / 2.0)
    checked = 0
    for m in tables["models"]:
        if m.get("archive") or m.get("valid_to") is not None or m.get("disabled"):
            continue
        if m["provider"] not in terms:
            continue
        price = m.get("normalized_price")
        if price is None:
            continue  # documented-gap lanes covered by the next test
        sticker = cat.get((m["provider"], m["model"]))
        if sticker is None:
            continue
        assert price < sticker, (
            f"sub lane NOT cheaper than PAYG: {m['provider']}/{m['model']} "
            f"normalized ${price} >= sticker ${sticker:.3f} — sub is pointless; "
            f"fix the multiplier/pool assumption in plan_terms.jsonl")
        checked += 1
    assert checked >= 10, f"only {checked} sub lanes verified — doctrine under-tested"


def test_unpriced_sub_lanes_are_documented_gaps():
    """NULL on a sub provider is ONLY allowed as a DOCUMENTED gap (Bane
    2026-08-27): every active unpriced lane of a flat_subscription provider
    must have a model_notes row (provider-wide '*' or per-model) explaining
    why — never a silent NULL pretending the sub doesn't exist."""
    tables = _load_tables()
    terms = {t["provider"] for t in tables["plan_terms"]
             if t.get("billing_model") == "flat_subscription"}
    notes = set()
    for n in tables.get("model_notes") or []:
        if n.get("model") == "*":
            notes.add(n.get("provider"))
        else:
            notes.add((n.get("provider"), n.get("model")))
    silent = []
    for m in tables["models"]:
        if m.get("archive") or m.get("valid_to") is not None or m.get("disabled"):
            continue
        if m["provider"] not in terms or m.get("normalized_price") is not None:
            continue
        if m["provider"] not in notes and (m["provider"], m["model"]) not in notes:
            silent.append(f"{m['provider']}/{m['model']}")
    assert not silent, (
        f"silent NULL on sub providers — each needs a model_notes gap row: {silent}"
    )


def test_p0_fore_has_no_tool_use_bar():
    """BANE 2026-08-27 (2nd correction, regime lock): tool_use REMOVED from
    P0_FORE — tool-calling is table stakes in 2026; the foreman profile must
    not gate on a cliff-artifact category. If this ever regresses, the
    committed task_profile_requirements.jsonl was re-exported from a stale
    seed run."""
    tables = _load_tables()
    p0 = [(r["category"], r["level"]) for r in tables["task_profile_requirements"]
          if r["task_id"] == "P0_FORE"]
    assert all(cat != "tool_use" for cat, _ in p0), (
        "P0_FORE must NOT require tool_use (Bane 2nd correction) — re-seed "
        "from current router_seed.py PROFILES"
    )
    # sanity: the profile still has its core bars (not accidentally emptied)
    cats = {c for c, _ in p0}
    assert {"agent_tick", "delegation", "reasoning", "schema"} <= cats


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
