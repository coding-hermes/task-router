"""Over-rating clamp + last-resort fallback tier (2026-10-20).

The bug measured live: the gateway auto-classifies every tick (the scheduler
never sends x-router-profile), the classifier rated a tick `tool_use >= 2`, and
NO lane can hold 1..4 — `tool_use` is a cliff (-5..-2, 0, 5). Every lane in the
0 cluster (all nine deepseek/PAYG lanes included) was excluded from the primary
chain. When xkiro / commandcode / openrouter went circuit-open or health-down
the chain emptied, and the fallback ladder ALSO came back empty: the one healthy
lane (`deepseek-duckbrain-sync/deepseek-flash`) is curated for profiles
['P8_SYNC'], and the ladder only used a profile-scoped lane when the resolved
profile was in its list. Zero hops -> gateway HTTP 503.

Two contracts pinned here:

1. INFERRED MATRICES ARE CLAMPED (the primary fix, owner ruling 2026-10-20):
   a scorer may not invent a requirement the ladder cannot express, nor demand
   a category the reference profile never declares. Explicit caller matrices
   (`--profile-req`) are untouched.
2. A PROFILE-SCOPED LANE IS A LAST RESORT, never a peer: it is reachable ONLY
   when the curated + generic tiers produced zero hops, so it can never outrank
   a healthy sub lane or change price ordering.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import router_outcomes as ro  # noqa: E402
import router_spawn as sp     # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _registry(tmp_path, *, include_generic_lane=False):
    """Registry with the CLIFF shape we measured: tool_use has rungs at
    -3, 0 and 5 only, and the reference profile P0_FORE deliberately does not
    declare it (router_seed.py: 'tool_use ratings are a cliff artifact')."""
    reg = {"version": 3, "tables": {
        "task_profiles": [{"id": "P0_FORE"}],
        "task_profile_requirements": [
            {"task_id": "P0_FORE", "category": "agent_tick", "level": 2},
        ],
        "models": [
            {"provider": "sub-lane", "model": "m0", "normalized_price": 0.2},
            {"provider": "sub-lane", "model": "m5", "normalized_price": 9.0},
            {"provider": "deepseek-foreman", "model": "deepseek-flash",
             "normalized_price": 0.05},
            {"provider": "deepseek-duckbrain-sync", "model": "deepseek-flash",
             "normalized_price": 0.05},
        ],
        "model_tier": (
            [{"model": "m0", "category": "tool_use", "tier": 0},
             {"model": "m0", "category": "agent_tick", "tier": 2},
             {"model": "m5", "category": "tool_use", "tier": 5},
             {"model": "m5", "category": "agent_tick", "tier": 2},
             {"model": "deepseek-flash", "category": "tool_use", "tier": 0},
             {"model": "deepseek-flash", "category": "tool_use", "tier": 0},
             {"model": "deepseek-flash", "category": "agent_tick", "tier": -1}]
        ),
        "fallback_lanes": ([
            {"provider": "deepseek-duckbrain-sync", "model": "deepseek-flash",
             "order": 7, "key_env": "K", "profiles": ["P8_SYNC"]},
        ] + ([{"provider": "sub-lane", "model": "m0", "order": 1, "key_env": "K"}]
             if include_generic_lane else [])),
    }}
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(reg))
    return str(p)


def _tables(registry_path):
    return json.loads(Path(registry_path).read_text())["tables"]


def _state(**overrides):
    """quota/health/circuit as the resolve path passes them in: everything
    OPEN unless overridden. `_resolve_fallback` takes the PROVIDER MAPS, not the
    state documents (resolve() unwraps them)."""
    providers = {"sub-lane": {"status": "open"},
                 "deepseek-duckbrain-sync": {"status": "open"},
                 "deepseek-foreman": {"status": "open"}}
    hs = {k: {"status": "open"} for k in providers}
    for prov, status in overrides.items():
        hs[prov] = {"status": status}
    return providers, hs, {}


# ---------------------------------------------------------------------------
# 1. the over-rating clamp (primary fix)
# ---------------------------------------------------------------------------

def test_fabricated_tool_use_gate_is_dropped_not_honoured(tmp_path):
    """THE over-rating bug: an inferred tool_use=2 excludes every 0-tier lane."""
    reg = _registry(tmp_path)
    levels, notes = ro.clamp_inferred_levels(
        {"agent_tick": 2, "tool_use": 2}, profile_id="P0_FORE", registry_path=reg)
    assert "tool_use" not in levels, (
        "P0_FORE never declares tool_use and no lane holds 1..4 — the scorer "
        f"invented that pressure; got {levels}")
    assert levels["agent_tick"] == 2, "the real requirement is untouched"
    assert "tool_use" in notes, "a clamp is never silent"


def test_a_declared_cliff_level_is_not_weakened(tmp_path):
    """Declared category: the profile really asks for it, so an inferred cliff
    level is intent — never silently lowered."""
    reg = _registry(tmp_path)
    levels, notes = ro.clamp_inferred_levels(
        {"agent_tick": 4}, profile_id="P0_FORE", registry_path=reg)
    assert levels["agent_tick"] == 4, levels
    assert notes == {}


def test_a_requirement_that_is_already_a_rung_is_untouched(tmp_path):
    reg = _registry(tmp_path)
    levels, notes = ro.clamp_inferred_levels(
        {"agent_tick": 2, "tool_use": 5}, profile_id="P0_FORE",
        registry_path=reg)
    assert levels == {"agent_tick": 2, "tool_use": 5}
    assert notes == {}, "no adjustment, no note — the healthy path is silent"


def test_an_unknown_profile_refuses_to_guess_what_it_declares(tmp_path):
    reg = _registry(tmp_path)
    levels, notes = ro.clamp_inferred_levels({"tool_use": 2}, profile_id="NOPE",
                                             registry_path=reg)
    assert levels["tool_use"] == 2, "no reference profile -> no clamp"
    assert notes == {}


# ---------------------------------------------------------------------------
# 2. the last-resort fallback tier (strictly subordinate)
# ---------------------------------------------------------------------------

def test_profile_scoped_lane_serves_a_foreign_profile_when_nothing_else_can(tmp_path):
    """The exact measured shape: profile agent_tick (not P8_SYNC), no generic
    lane healthy, one healthy P8_SYNC lane -> >= 1 hop, marked last_resort."""
    reg = _registry(tmp_path)
    tables = _tables(reg)
    qs, hs, cs = _state(**{"sub-lane": "DOWN"})
    hops = sp._resolve_fallback(tables, qs, hs, cs, [("tool_use", 2)],
                                profile_id="P1_CODING")
    assert len(hops) >= 1, "zero hops is the 503; a foreign-scoped lane must serve"
    assert hops[0]["provider"] == "deepseek-duckbrain-sync"
    assert hops[0]["last_resort"] is True
    assert hops[0]["profile_scope"] == ["P8_SYNC"]
    assert "P8_SYNC" in hops[0]["last_resort_reason"]


def test_last_resort_never_outranks_a_healthy_generic_lane(tmp_path):
    """Strict subordination: with a healthy generic lane available, the
    profile-scoped lane is NOT in the chain at all."""
    reg = _registry(tmp_path, include_generic_lane=True)
    tables = _tables(reg)
    qs, hs, cs = _state()
    hops = sp._resolve_fallback(tables, qs, hs, cs, [("tool_use", 2)],
                                profile_id="P1_CODING")
    assert [h["provider"] for h in hops] == ["sub-lane"], hops
    assert not any(h.get("last_resort") for h in hops)


def test_curated_lane_still_wins_its_own_profile(tmp_path):
    reg = _registry(tmp_path, include_generic_lane=True)
    tables = _tables(reg)
    qs, hs, cs = _state()
    hops = sp._resolve_fallback(tables, qs, hs, cs, [("tool_use", 2)],
                                profile_id="P8_SYNC")
    assert hops[0]["provider"] == "deepseek-duckbrain-sync"
    assert not hops[0].get("last_resort"), "its own profile is not a last resort"
    assert hops[0]["hop"] == 1


def test_a_gated_last_resort_still_returns_zero_not_a_fabrication(tmp_path):
    """Fail-closed is preserved: a DOWN last-resort lane must not be invented."""
    reg = _registry(tmp_path)
    tables = _tables(reg)
    qs, hs, cs = _state(**{"deepseek-duckbrain-sync": "DOWN"})
    hops = sp._resolve_fallback(tables, qs, hs, cs, [("tool_use", 2)],
                                profile_id="P1_CODING")
    assert hops == []


# ---------------------------------------------------------------------------
# 3. end-to-end: an empty primary chain must yield a hop, not a 503
# ---------------------------------------------------------------------------

def test_empty_primary_chain_yields_a_hop_not_zero(monkeypatch, tmp_path):
    """THE proof: every eligible lane gated/unmatchable + one healthy
    foreign-profile lane -> resolve returns a head (the gateway's 503 was the
    zero-hop chain)."""
    reg = tmp_path / "registry.json"
    doc = json.loads(Path(_registry(tmp_path)).read_text())
    doc["tables"]["task_profiles"].append({"id": "P1_CODING"})
    doc["tables"]["task_profile_requirements"].append(
        {"task_id": "P1_CODING", "category": "agent_tick", "level": 5})
    reg.write_text(json.dumps(doc))
    state = tmp_path / "state"
    state.mkdir()
    (state / "quota-state.json").write_text(json.dumps(
        {"updated": "test", "providers": {
            "sub-lane": {"status": "open"},
            "deepseek-duckbrain-sync": {"status": "open"},
            "deepseek-foreman": {"status": "open"}}}))
    # every subscription lane health-DOWN: the measured 2026-10-20 condition
    (state / "health-state.json").write_text(json.dumps(
        {"providers": {"sub-lane": {"status": "DOWN",
                                    "ts": "2026-10-20T00:00:00+00:00"}}}))
    (state / "circuit-state.json").write_text(json.dumps({"pairs": {}}))
    monkeypatch.setattr(sp, "REGISTRY", str(reg))
    monkeypatch.setattr(sp, "MR", str(state))
    out = sp.resolve(profile_id="P1_CODING")
    assert "error" not in out, out.get("error")
    assert out["head"], "zero hops is the 503"
    assert out["head"]["provider"] == "deepseek-duckbrain-sync"
    assert out["head"]["last_resort"] is True
    assert out["degraded_fallback"] is True
