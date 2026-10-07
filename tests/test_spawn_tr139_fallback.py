"""TR-139 (reopened) — the unrated/default fallback resolves the CHEAPEST profile.

The default arm of router_spawn.resolve() (no project, no --profile, no
--profile-req — i.e. an unrated prompt) used to stamp profile P0_FORE, the
priciest seat in the fleet. Measured live 2026-10-03: 65 of 97 post-flip proxy
rows carried profile_id=P0_FORE. Now the fallback is the CHEAPEST registered
profile (sum of its chain's lane prices, built with the same _build_chain
eligibility/price contract as resolve()), overridable via
ROUTER_EMPTY_MATRIX_PROFILE, and every fallback is STAMPED on the payload
(default_profile: complexity_source + profile_id + degrade_reason) so the
unrated rate is countable from the ledger.

The rated paths are pinned UNTOUCHED: an adhoc requirement list still selects
by the bars, an explicit --profile still resolves that profile, and neither
carries a fallback stamp.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import router_spawn as rs  # noqa: E402


def _tables():
    """Registry shaped like the house tier fixture (tests/test_spawn_board_profile.py).

    Four registered profiles spanning the price range; P0_FORE is deliberately
    the priciest to resolve (chain = [top], $1.0). 'free' carries the only
    review tier, so P9_REVIEW's review>=0 bar resolves to the single cheapest
    lane — making P9_REVIEW the cheapest profile (chain sum $0.01). P8_SYNC's
    lenient review>=-2 bar keeps every lane (sum $1.11). Missing tier = -1,
    per the BLANK default.
    """
    return {
        "models": [
            {"provider": "prov", "model": "free", "normalized_price": 0.01},
            {"provider": "prov", "model": "mid", "normalized_price": 0.1},
            {"provider": "prov", "model": "top", "normalized_price": 1.0},
        ],
        "model_tier": [
            {"model": "top", "category": "code_gen", "tier": 3},
            {"model": "free", "category": "review", "tier": 0},
        ],
        "task_profiles": [
            {"id": "P0_FORE"},
            {"id": "P1_CODING"},
            {"id": "P9_REVIEW"},
            {"id": "P8_SYNC"},
        ],
        "task_profile_requirements": [
            {"task_id": "P0_FORE", "category": "code_gen", "level": 2},
            {"task_id": "P1_CODING", "category": "code_gen", "level": 0},
            {"task_id": "P9_REVIEW", "category": "review", "level": 0},
            {"task_id": "P8_SYNC", "category": "review", "level": -2},
        ],
        "projects": [],
        "providers": [], "fallback_lanes": [], "plan_terms": [],
        # adhoc validation data (TR-023): category membership + the -5..+5 scale
        "category_levels": [{"category": "code_gen"}, {"category": "review"}],
        "level_defs": [{"level": -5}, {"level": -2}, {"level": -1},
                       {"level": 0}, {"level": 1}, {"level": 2},
                       {"level": 3}, {"level": 5}],
    }


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    """Fixture registry + open gate state + a clean override env."""
    monkeypatch.setattr(rs, "_load_registry_with_meta",
                        lambda: (_tables(), "test", False, None))
    # house pattern: hermetic gate state — the one provider OPEN, no
    # health/circuit files (gates open, fail-open). An EMPTY state dir would
    # gate every lane on missing quota.
    state = tmp_path / "state"
    state.mkdir()
    (state / "quota-state.json").write_text(
        '{"updated": "test", "providers": {"prov": {"status": "open"}}}')
    monkeypatch.setattr(rs, "MR", str(state))
    monkeypatch.delenv("ROUTER_EMPTY_MATRIX_PROFILE", raising=False)
    return state


# ---------------------------------------------------------------- AC1 -------
def test_empty_matrix_routes_cheapest(hermetic):
    """AC1: an empty/default classifier matrix resolves to the cheapest
    profile's chain, NOT P0_FORE."""
    doc = rs.resolve(use_health=False)
    assert "error" not in doc, doc.get("error")
    assert doc["resolved_as"] == "default"
    assert doc["profile"] == "P9_REVIEW", (
        "the unrated fallback must be the CHEAPEST profile (chain sum $0.01), "
        f"not the priciest — got {doc['profile']}")
    assert doc["profile"] != "P0_FORE"
    head = doc["head"]
    assert head is not None and head["model"] == "free"
    assert "top" not in {h["model"] for h in doc["chain"]}, \
        "the priciest lane must not ride an unrated fallback"


# ---------------------------------------------------------------- AC2 -------
def test_env_override_pins_fallback(hermetic, monkeypatch):
    """AC2: ROUTER_EMPTY_MATRIX_PROFILE pins the fallback profile — and beats
    the cheapest-profile default (P8_SYNC is provably NOT the cheapest here)."""
    monkeypatch.setenv("ROUTER_EMPTY_MATRIX_PROFILE", "P8_SYNC")
    doc = rs.resolve(use_health=False)
    assert "error" not in doc, doc.get("error")
    assert doc["profile"] == "P8_SYNC"
    assert doc["default_profile"]["profile_id"] == "P8_SYNC"


def test_env_override_unknown_profile_is_ignored_visibly(hermetic, monkeypatch):
    """An override the registry cannot honour is dropped LOUDLY; the cheap
    default still applies."""
    monkeypatch.setenv("ROUTER_EMPTY_MATRIX_PROFILE", "NOT_A_PROFILE")
    doc = rs.resolve(use_health=False)
    assert doc["profile"] == "P9_REVIEW", "the cheapest default still applies"
    problems = doc["default_profile"]["problems"]
    assert any("NOT_A_PROFILE" in p and "override ignored" in p for p in problems), \
        f"the ignored override must be visible, got {problems}"


# ---------------------------------------------------------------- AC3 -------
def test_real_matrix_unaffected(hermetic):
    """AC3: a real (non-empty) rating is unaffected — normal selection, no
    fallback stamp."""
    rated = rs.resolve(adhoc=["code_gen=2"], use_health=False)
    assert "error" not in rated, rated.get("error")
    assert rated["resolved_as"] == "adhoc"
    assert rated["profile"] is None
    assert rated["default_profile"] is None, "rated inputs carry no fallback stamp"
    assert {h["model"] for h in rated["chain"]} == {"top"}, \
        "the strict bar must still select exactly the capable lane"

    explicit = rs.resolve(profile_id="P1_CODING", use_health=False)
    assert explicit["profile"] == "P1_CODING"
    assert explicit["resolved_as"] == "profile-arg"
    assert explicit["default_profile"] is None
    assert {h["model"] for h in explicit["chain"]} == {"top"}


# ---------------------------------------------------------------- AC4 -------
def test_degrade_envelope_names_reason(hermetic):
    """AC4: the envelope records complexity_source + degrade_reason + the
    chosen profile_id for every fallback."""
    doc = rs.resolve(use_health=False)
    dp = doc["default_profile"]
    assert dp is not None, "the default arm must stamp the envelope"
    assert dp["complexity_source"] == "default"
    assert dp["profile_id"] == doc["profile"] == "P9_REVIEW"
    assert dp["degrade_reason"], "the degrade reason must name what happened"
    assert "unrated fallback" in dp["degrade_reason"]
    assert "ROUTER_EMPTY_MATRIX_PROFILE" in dp["degrade_reason"]


def test_degrade_envelope_names_the_override_when_pinned(hermetic, monkeypatch):
    monkeypatch.setenv("ROUTER_EMPTY_MATRIX_PROFILE", "P8_SYNC")
    doc = rs.resolve(use_health=False)
    dp = doc["default_profile"]
    assert dp["complexity_source"] == "default"
    assert dp["profile_id"] == "P8_SYNC" == doc["profile"]
    assert "P8_SYNC" in dp["degrade_reason"] and "override" in dp["degrade_reason"]


# ------------------------------------------------- helper-level last resort --
def test_last_resort_only_when_no_profile_resolves_a_chain():
    """P0_FORE survives ONLY as the explicitly-named last resort: a registry
    whose profiles resolve NO priced chain anywhere."""
    pid, meta = rs._empty_matrix_fallback(
        {"models": []}, {"P0_FORE": {"id": "P0_FORE"}}, {"P0_FORE": []})
    assert pid == "P0_FORE"
    assert meta["complexity_source"] == "default"
    assert meta["profile_id"] == "P0_FORE"
    assert "last resort" in meta["degrade_reason"]


def test_cheapest_scan_ties_break_deterministically():
    """Two profiles with identical chain sums: the lexicographically first id
    wins (sorted iteration + strict <), so the fallback is stable."""
    tables = {
        "models": [{"provider": "prov", "model": "free",
                    "normalized_price": 0.5}],
        "model_tier": [], "task_profiles": [{"id": "P_B"}, {"id": "P_A"}],
        "task_profile_requirements": [
            {"task_id": "P_A", "category": "review", "level": -2},
            {"task_id": "P_B", "category": "review", "level": -2},
        ],
        "projects": [], "providers": [], "fallback_lanes": [], "plan_terms": [],
    }
    profiles = {r["id"]: r for r in tables["task_profiles"]}
    reqs = {}
    for r in tables["task_profile_requirements"]:
        reqs.setdefault(r["task_id"], []).append((r["category"], r["level"]))
    assert rs._cheapest_profile_id(tables, profiles, reqs) == "P_A"
