"""TR-069 wave 2 — lifecycle states in the resolver: pre-release gating,
retiring warnings on hops, counts-by-state. Dates decide; nothing invented;
nothing vanishes silently. Frozen clock via rs._today (monkeypatch)."""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import router_spawn as rs  # noqa: E402


# ---------------------------------------------------------------- state map
def test_state_map_all_four_states_frozen_clock(monkeypatch):
    monkeypatch.setattr(rs, "_today", lambda: "2026-09-19")
    assert rs.lifecycle_state({}, today="2026-09-19") == "live"
    assert rs.lifecycle_state({"valid_to": "2026-09-25"}, today="2026-09-19") == "retiring"
    assert rs.lifecycle_state({"valid_to": "2026-10-25"}, today="2026-09-19") == "live"
    assert rs.lifecycle_state({"valid_to": "2026-09-01"}, today="2026-09-19") == "retired"
    assert rs.lifecycle_state({"valid_to": "2026-09-19"}, today="2026-09-19") == "retired"
    assert rs.lifecycle_state({"available_from": "2026-10-01"}, today="2026-09-19") == "coming_soon"
    assert rs.lifecycle_state({"available_from": "2026-09-01"}, today="2026-09-19") == "live"
    # PRECEDENCE (pinned): retired > coming_soon > retiring > live. A past
    # valid_to is authoritative (matches row_is_retired); a future
    # available_from with a past valid_to is contradictory data, and
    # "retired" is the honest verdict.
    assert rs.lifecycle_state(
        {"available_from": "2026-10-01", "valid_to": "2026-09-01"},
        today="2026-09-19") == "retired"
    # future available_from + future valid_to = announced window, coming soon
    assert rs.lifecycle_state(
        {"available_from": "2026-10-01", "valid_to": "2026-12-01"},
        today="2026-09-19") == "coming_soon"


def test_retire_warn_window_is_data():
    """The 14-day warn window is an env knob, not folklore."""
    assert rs.RETIRE_WARN_DAYS == 14


# ------------------------------------------------------- eligibility gating
def _tables_with(rows):
    models = list(rows)
    return {
        "models": models,
        "tiers": {}, "providers": [], "fallback_lanes": [],
        "profiles": {}, "plan_terms": [], "fallback": False,
    }


def test_coming_soon_lane_never_routes_but_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(rs, "_today", lambda: "2026-09-19")
    # house verbosity contract: diagnostics print when ROUTER_MISS_VERBOSE is set
    monkeypatch.setenv("ROUTER_MISS_VERBOSE", "1")
    tables = _tables_with([
        {"provider": "prov", "model": "released", "normalized_price": 1.0},
        {"provider": "prov", "model": "future-model", "normalized_price": 0.5,
         "available_from": "2026-10-01"},
    ])
    counts = {}
    rows = rs._build_chain(tables, [], limit=5, lifecycle_counts=counts)
    names = [r[2] for r in rows]
    assert "future-model" not in names, "unreleased lane must never route"
    assert "released" in names
    assert counts.get("coming_soon") == 1, "the hidden lane is COUNTED"
    err = capsys.readouterr().err
    assert "future-model" in err and "coming_soon" in err, "and it SAYS SO (stderr)"


def test_retiring_lane_routes_with_warning_fields(monkeypatch, capsys):
    monkeypatch.setattr(rs, "_today", lambda: "2026-09-19")
    tables = _tables_with([
        {"provider": "ollama-cloud", "model": "deepseek-v4-flash:0731",
         "normalized_price": 0.1, "valid_to": "2026-09-25",
         "replaced_by": "ollama-cloud/deepseek-v4.1-flash"},
    ])
    counts = {}
    rows = rs._build_chain(tables, [], limit=5, lifecycle_counts=counts)
    assert len(rows) == 1, "retiring does NOT cut capacity"
    full = rows[0][5]
    assert counts.get("retiring") == 1
    # the hop fields are added at the resolve layer, not _build_chain; the
    # row survives eligibility is the contract here
    assert full["valid_to"] == "2026-09-25"


def test_resolve_end_to_end_counts_and_hop_warnings(monkeypatch, tmp_path):
    """Full resolve() path: lifecycle_counts in the payload; retires_on +
    replaced_by ride the retiring hop."""
    monkeypatch.setattr(rs, "_today", lambda: "2026-09-19")
    tables = {"models": [
            {"provider": "prov", "model": "ok", "normalized_price": 1.0},
            {"provider": "prov", "model": "dying", "normalized_price": 0.2,
             "valid_to": "2026-09-25", "replaced_by": "prov/successor"},
        ], "tiers": {}, "providers": [], "fallback_lanes": [],
        "profiles": [], "task_profiles": [
            {"id": "P0_FORE", "description": "test default profile"}],
        "projects": [],
        "task_profile_requirements": [
            # a lenient bar (missing tier = -1 clears it) so the chain builds
            {"task_id": "P0_FORE", "category": "review", "level": -2}],
        "plan_terms": [], "fallback": False}
    monkeypatch.setattr(rs, "_load_registry_with_meta",
                        lambda: (tables, "test", False, None))
    # hermetic gate state (missing files = gates open, fail-open; the house
    # pattern from tests/test_regression.py:_state_dir): an EMPTY state dir —
    # pointing MR at tmp_path would otherwise read LIVE gate files.
    state = tmp_path / "state"
    state.mkdir()
    (state / "quota-state.json").write_text(
        '{"updated": "test", "providers": {"prov": {"status": "open"}}}')
    (state / "health-state.json").write_text(
        '{"providers": {"prov": {"status": "OK", "latency_ms": 100}}}')
    (state / "circuit-state.json").write_text('{"pairs": {}}')
    monkeypatch.setattr(rs, "MR", str(state))
    result = rs.resolve(limit=5, use_health=False)
    counts = result.get("lifecycle_counts") or {}
    assert counts.get("retiring") == 1, f"counts missing/wrong: {counts}"
    assert counts.get("live") == 1
    hop = next(h for h in result["chain"] if h["model"] == "dying")
    assert hop["retires_on"] == "2026-09-25"
    assert hop["replaced_by"] == "prov/successor"
    # and the healthy lane carries NO lifecycle keys (additive-only contract)
    clean = next(h for h in result["chain"] if h["model"] == "ok")
    assert "lifecycle" not in clean and "retires_on" not in clean
