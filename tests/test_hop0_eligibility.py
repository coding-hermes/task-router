"""TR-357 M2 unit tests: the hop-0 eligibility rule.

Board acceptance — a lane whose observed dry rate (failures / known outcomes,
the success verdicts TR-357 M1 stamps into the outcome store) exceeds a
configurable floor over n >= min_known CANNOT HOLD HOP 0 regardless of price:
  (a) cheapest lane, dry 0.80 over 10 known -> displaced from the head, the
      displacement VISIBLE (entry marker + payload audit block), lane kept
  (b) same lane with n < min_known (7 known)        -> keeps hop 0
  (c) dry rate at / under the floor (0.65 / 0.60)   -> keeps hop 0
  (d) the knobs (ROUTER_HOP0_MAX_DRY / ROUTER_HOP0_MIN_KNOWN /
      ROUTER_HOP0=0 / malformed env) change the behavior
  (e) the rest of the chain order is unchanged by the rule (one swap)
plus the layered relationship with TR-354 (displacement is NOT an exclusion,
even with that gate live), the model-family judged record, the held-at-hop-0
fail-open when no alternative exists, and the price-sort inert state.

Hermetic: mini registry + scratch state dir + scratch averages, all under
tmp_path — the same fixture pattern as tests/test_tr354_outcomes_floor.py.
The live fleet state is never read or written. M2-focused resolves disable
the TR-354 exclusion gate with its documented kill switch (ROUTER_OUTCOME_FLOOR=0)
so the displacement rule is pinned alone; the layering test keeps it live.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import router_spawn  # noqa: E402

CAT = "agent_tick"
DRY = "gpt-5.6-dry"               # the cheap, well-measured, dry lane (model)
ALT = "alt-model"                 # the pricier healthy alternative (model)
DRY_P, ALT_P = "cheaplane", "altprov"
DRY_ID = f"{DRY_P}/{DRY}"
ALT_ID = f"{ALT_P}/{ALT}"


def _tables():
    tables = {
        "providers": [{"id": DRY_P}, {"id": ALT_P}, {"id": "fb"}],
        "models": [
            {"provider": DRY_P, "model": DRY, "normalized_price": 0.01,
             "data_class": "open", "context_limit": 200000},
            {"provider": ALT_P, "model": ALT, "normalized_price": 0.50,
             "data_class": "open", "context_limit": 200000},
        ],
        "model_tier": [{"model": m, "category": CAT, "tier": 5}
                       for m in (DRY, ALT)],
        "category_levels": [{"category": CAT, "level": 5, "label": "q95",
                             "min_perf": 0.9}],
        "level_defs": [{"level": lvl, "label": str(lvl)} for lvl in range(-5, 6)],
        "fallback_lanes": [{"provider": "fb", "model": "always-run",
                            "order": 1}],
        "projects": [{"id": "proj", "profile": "TP"}],
        "task_profiles": [{"id": "TP", "title": "test profile"}],
        "task_profile_requirements": [{"task_id": "TP", "category": CAT,
                                       "level": 5}],
    }
    # the always-run lane exists in `models` — _resolve_fallback skips
    # fallback_lanes rows whose (provider, model) is not a registry model
    # ('a gap, not a fabrication'). It carries NO model_tier row, so it never
    # enters the primary chain (a ROUTER-MISS lane) and stays fallback-only.
    tables["models"].append(
        {"provider": "fb", "model": "always-run", "normalized_price": 9.0,
         "data_class": "open", "context_limit": 200000})
    return tables


def _state(tmp_path, tables):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"providers": {r["id"]: {"status": "open"}
                             for r in tables["providers"]}},
              open(d / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(d / "circuit-state.json", "w"))
    return str(d)


def _wire(monkeypatch, tmp_path, tables=None, averages=None):
    tables = tables or _tables()
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    monkeypatch.setattr(router_spawn, "REGISTRY", str(reg))
    monkeypatch.setattr(router_spawn, "MR", _state(tmp_path, tables))
    avg_path = tmp_path / "averages.jsonl"
    if averages:
        with open(avg_path, "w") as f:
            for row in averages:
                f.write(json.dumps(row) + "\n")
    monkeypatch.setenv("ROUTING_AVERAGES_FILE", str(avg_path))
    # deterministic knob baseline: the hop-0 knobs at their defaults and the
    # TR-354 exclusion gate OFF (its documented kill switch) unless a test
    # re-arms it — each M2 rule test pins ONE behavior.
    for var in ("ROUTER_HOP0", "ROUTER_HOP0_MAX_DRY", "ROUTER_HOP0_MIN_KNOWN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ROUTER_OUTCOME_FLOOR", "0")
    return tables, str(avg_path)


def _averages_row(provider, model, n, known, completed, cost=None):
    """Averages row the way outcomes_averages writes it: dry rate is derived
    from the KNOWN outcomes (n_success_known, n_completed) M1 stamps —
    never guessed from n_samples."""
    row = {"source_system": "hermes", "provider": provider, "model": model,
           "complexity": None, "n_samples": n, "n_completed": completed,
           "n_success_known": known,
           "success_rate": (completed / known) if known else None}
    if cost is not None:
        row["avg_cost_task_24h"] = cost
    return row


def _dry_lane_record(n=10, known=10, completed=2, cost=0.001):
    """The canonical dry record: dry rate (known-completed)/known = 0.80."""
    return [_averages_row(DRY_P, DRY, n, known, completed, cost=cost)]


def _order(result):
    return [f"{e['provider']}/{e['model']}" for e in result["chain"]]


def _entry(result, model):
    return next(e for e in result["chain"] if e["model"] == model)


def _ex(result, model):
    return next((e for e in result["exclusions"] if e["model"] == model), None)


# ---------------------------------------------------------------------------
# (a) the rule displaces a dry cheap head — and the displacement is VISIBLE
# ---------------------------------------------------------------------------

def test_dry_cheap_head_is_displaced_and_the_displacement_is_visible(
        monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=_dry_lane_record())
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    # the dry lane lost the head to the pricier healthy lane...
    assert _order(r) == [ALT_ID, DRY_ID], _order(r)
    assert r["head"]["model"] == ALT
    # ...but was NOT excluded — displacement only, it stays in the chain
    assert _ex(r, DRY) is None
    # the marker rides on the affected lane entry, naming rule + numbers
    ent = _entry(r, DRY)
    assert ent["hop0_ineligible"] is True
    assert "hop0-ineligible" in ent["hop0_ineligible_reason"]
    assert "0.80" in ent["hop0_ineligible_reason"]
    assert "0.65" in ent["hop0_ineligible_reason"]       # the floor
    assert "TR-357" in ent["hop0_ineligible_reason"]
    assert "8+ known" in ent["hop0_ineligible_reason"]
    # the payload carries the audit block: judged, floors, who moved, why
    blk = r["hop0_eligibility"]
    assert blk["judged"] is True
    assert blk["max_dry"] == 0.65 and blk["min_known"] == 8
    assert blk["rule"] == router_spawn.ROUTER_HOP0_RULE
    assert blk["displaced"] == [{
        "provider": DRY_P, "model": DRY,
        "dry_rate": pytest.approx(0.8),
        "n_success_known": 10, "n_completed": 2,
        "source": "lane", "now_at_chain_position": 1,
        "passed_by": ALT_ID}]
    # the displaced entry keeps its (pre-rule) hop number — one swap, no renumber
    assert ent["hop"] == 1


def test_displacement_is_not_an_exclusion_even_with_tr354_live(
        monkeypatch, tmp_path):
    """The M2 knob window TR-354 does not cover: dry 0.55 (success 0.45)
    passes the TR-354 exclusion floor (0.35) yet exceeds a tightened
    ROUTER_HOP0_MAX_DRY=0.5 — the lane is DISPLACED, never excluded."""
    _wire(monkeypatch, tmp_path,
          averages=_dry_lane_record(n=20, known=20, completed=9))
    monkeypatch.delenv("ROUTER_OUTCOME_FLOOR", raising=False)  # TR-354 LIVE
    monkeypatch.setenv("ROUTER_HOP0_MAX_DRY", "0.5")
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [ALT_ID, DRY_ID], _order(r)
    assert _ex(r, DRY) is None, "displacement must never become an exclusion"
    assert _entry(r, DRY)["hop0_ineligible"] is True
    assert r["hop0_eligibility"]["displaced"][0]["dry_rate"] == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# (b) insufficient evidence never excludes: n < min_known keeps hop 0
# ---------------------------------------------------------------------------

def test_below_min_known_the_dry_lane_keeps_hop0(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path,
          averages=_dry_lane_record(n=7, known=7, completed=0))
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID], _order(r)
    assert r["head"]["model"] == DRY
    assert "hop0_ineligible" not in _entry(r, DRY)
    blk = r["hop0_eligibility"]
    assert blk["judged"] is True
    assert blk["ineligible"] == []
    assert blk["displaced"] == []
    assert blk["reason"] == "head eligible — no displacement"


# ---------------------------------------------------------------------------
# (c) at / under the floor keeps hop 0 (the boundary is STRICT >)
# ---------------------------------------------------------------------------

def test_dry_rate_at_the_floor_keeps_hop0(monkeypatch, tmp_path):
    # 13/20 = exactly the 0.65 floor: "exceeds" is strict, so it holds
    _wire(monkeypatch, tmp_path,
          averages=_dry_lane_record(n=20, known=20, completed=7))
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID]
    assert "hop0_ineligible" not in _entry(r, DRY)
    assert r["hop0_eligibility"]["displaced"] == []


def test_dry_rate_under_the_floor_keeps_hop0(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path,
          averages=_dry_lane_record(n=10, known=10, completed=4))  # dry 0.60
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID]
    assert "hop0_ineligible" not in _entry(r, DRY)


# ---------------------------------------------------------------------------
# (d) the knobs change the behavior (env re-read at call time)
# ---------------------------------------------------------------------------

def test_max_dry_knob_widens_and_narrows_the_rule(monkeypatch, tmp_path):
    rec = _dry_lane_record(n=10, known=10, completed=4)   # dry 0.60
    _wire(monkeypatch, tmp_path, averages=rec)
    # default 0.65: dry 0.60 keeps the head
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID]
    # tightened 0.5: the same record now displaces
    monkeypatch.setenv("ROUTER_HOP0_MAX_DRY", "0.5")
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [ALT_ID, DRY_ID]
    assert r["hop0_eligibility"]["max_dry"] == 0.5
    assert _entry(r, DRY)["hop0_ineligible"] is True


def test_min_known_knob_moves_the_evidence_bar(monkeypatch, tmp_path):
    rec = _dry_lane_record(n=8, known=8, completed=1)     # dry 0.875, 8 known
    _wire(monkeypatch, tmp_path, averages=rec)
    # tightened bar 12: 8 known is thin evidence, hop 0 keeps
    monkeypatch.setenv("ROUTER_HOP0_MIN_KNOWN", "12")
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID]
    assert r["hop0_eligibility"]["min_known"] == 12
    # loosened bar 6: the same record displaces
    monkeypatch.setenv("ROUTER_HOP0_MIN_KNOWN", "6")
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [ALT_ID, DRY_ID]
    assert _entry(r, DRY)["hop0_ineligible"] is True


def test_kill_switch_and_malformed_env(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=_dry_lane_record())
    # ROUTER_HOP0=0 disables the rule entirely — inert WITH the reason
    monkeypatch.setenv("ROUTER_HOP0", "0")
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID, ALT_ID]
    blk = r["hop0_eligibility"]
    assert blk["judged"] is False
    assert "disabled" in blk["reason"]
    assert "hop0_ineligible" not in _entry(r, DRY)
    # malformed values fall back to the DEFAULTS, never crash the resolve
    monkeypatch.delenv("ROUTER_HOP0", raising=False)
    monkeypatch.setenv("ROUTER_HOP0_MAX_DRY", "not-a-float")
    monkeypatch.setenv("ROUTER_HOP0_MIN_KNOWN", "also-bad")
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [ALT_ID, DRY_ID]      # default 0.65 / 8 still in force
    blk = r["hop0_eligibility"]
    assert blk["max_dry"] == 0.65 and blk["min_known"] == 8
    assert router_spawn._hop0_cfg() == (0.65, 8)


# ---------------------------------------------------------------------------
# (e) the rule changes exactly ONE thing: the head swap
# ---------------------------------------------------------------------------

def test_only_the_head_moves_the_rest_of_the_chain_is_unchanged(
        monkeypatch, tmp_path):
    tables = _tables()
    mid_names = ["m1", "m2", "m3"]
    mid = [f"mp{i}/{name}" for i, name in enumerate(mid_names)]
    for i, name in enumerate(mid_names):
        tables["providers"].append({"id": f"mp{i}"})
        tables["models"].append(
            {"provider": f"mp{i}", "model": name,
             "normalized_price": 0.5 + i * 0.1, "data_class": "open",
             "context_limit": 200000})
        tables["model_tier"].append({"model": name, "category": CAT,
                                     "tier": 5})
    _wire(monkeypatch, tmp_path, tables=tables, averages=_dry_lane_record())
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    # pre-rule price order would be [DRY, m1, m2, m3]; the rule swaps the head
    assert _order(r) == [ALT_ID, DRY_ID] + mid, _order(r)
    # every non-head lane kept its exact position
    for i, name in enumerate(mid_names, start=2):
        assert r["chain"][i]["model"] == name
    # ...and the displaced lane sits at position 1 with the marker
    assert _entry(r, DRY)["hop0_ineligible"] is True
    blk = r["hop0_eligibility"]
    assert blk["displaced"][0]["now_at_chain_position"] == 1
    assert blk["displaced"][0]["passed_by"] == ALT_ID


# ---------------------------------------------------------------------------
# the judged record: model-family borrow, held-at-hop-0 fail-open, inert arm
# ---------------------------------------------------------------------------

def test_the_judged_record_borrows_the_model_family_and_says_so(
        monkeypatch, tmp_path):
    """The lane carries no own rows; the sibling provider measured the model.
    The displacement judges on the FAMILY aggregate and the marker names it."""
    tables = _tables()
    tables["providers"][0]["id"] = "xkiro-2"
    tables["models"][0]["provider"] = "xkiro-2"
    family = [_averages_row("xkiro", DRY, 10, 10, 1)]     # dry 0.90, family id
    _wire(monkeypatch, tmp_path, tables=tables, averages=family)
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [ALT_ID, f"xkiro-2/{DRY}"]
    ent = _entry(r, DRY)
    assert ent["hop0_ineligible"] is True
    assert "model-family" in ent["hop0_ineligible_reason"]
    assert r["hop0_eligibility"]["displaced"][0]["source"] == "model-family"


def test_dry_head_with_no_alternative_is_held_and_says_so(
        monkeypatch, tmp_path):
    """Fail-open twin of the rule: a SOLO dry lane cannot be displaced (no
    eligible alternative exists) — it keeps hop 0 but the marker and the
    payload say so instead of passing silently."""
    tables = _tables()
    tables["models"] = [m for m in tables["models"] if m["model"] != ALT]
    _wire(monkeypatch, tmp_path, tables=tables, averages=_dry_lane_record())
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _order(r) == [DRY_ID]
    ent = _entry(r, DRY)
    assert ent["hop0_ineligible"] is True
    assert "HELD" in ent["hop0_ineligible_reason"]
    assert "no eligible alternative" in ent["hop0_ineligible_reason"]
    blk = r["hop0_eligibility"]
    assert blk["displaced"] == []
    assert blk["held_no_alternative"]["model"] == DRY


def test_price_sort_leaves_the_rule_inert_with_the_reason(
        monkeypatch, tmp_path):
    """No outcome stats on a price-sort resolve: the rule does not fire and
    NAMES why (judged False) — never a silent pass, never a changed chain."""
    _wire(monkeypatch, tmp_path, averages=_dry_lane_record())
    r = router_spawn.resolve(project="proj", sort="price")
    assert _order(r) == [DRY_ID, ALT_ID]
    assert "hop0_ineligible" not in _entry(r, DRY)
    blk = r["hop0_eligibility"]
    assert blk["judged"] is False
    assert "price sort" in blk["reason"]
    assert blk["displaced"] == []
