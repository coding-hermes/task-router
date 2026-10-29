"""TR-354 — the outcomes coverage floor and provider-variant stats attribution.

Three contracts, all born from the live finding (xkiro-2/openai/gpt-5.6-terra
heading P1_CODING while 303 of 417 known outcomes for the SAME MODEL sit under
the sibling provider id `xkiro`):

1. Stats attribution survives provider-variant ids: a lane with no rows of its
   own consults the MODEL FAMILY's aggregate and SAYS SO
   (`stats_fallback: model-family`), so a reader can tell a measured lane from
   a borrowed record.
2. The outcome-floor gate: a lane whose own or family record is catastrophic
   (>=8 known outcomes AND completion rate <= 0.35) is EXCLUDED with a named
   `outcome-floor` code — never a silent re-rank — whenever the chain has an
   alternative that clears the same profile.
3. The floors are knobs (spec position + env), and a fully-gated chain still
   serves the always-run fallback (fail-open is sacred).

Hermetic: mini registry + scratch state dir + scratch averages, all under
tmp_path. The live fleet state is never read or written.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import router_spawn  # noqa: E402

CAT = "agent_tick"
TERRA = "openai/gpt-5.6-terra"          # model family shared by two provider ids
FAMILY = "xkiro"                          # where the measurements live
VARIANT = "xkiro-2"                       # the lane the chain actually carries
ALT = ("altprov", "alt-model")            # the healthy alternative


def _tables():
    tables = {
        "providers": [{"id": VARIANT}, {"id": ALT[0]}, {"id": "fb"}],
        "models": [
            {"provider": VARIANT, "model": TERRA, "normalized_price": 0.01,
             "data_class": "open", "context_limit": 200000},
            {"provider": ALT[0], "model": ALT[1], "normalized_price": 0.50,
             "data_class": "open", "context_limit": 200000},
        ],
        "model_tier": [{"model": m, "category": CAT, "tier": 5}
                       for m in (TERRA, ALT[1], "always-run")],
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
    # the always-run lane must ALSO exist in `models` — _resolve_fallback
    # skips fallback_lanes rows whose (provider, model) is not a registry
    # model ('a gap, not a fabrication').
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
    return tables, str(avg_path)


def _family_row(provider, model, n, known, completed, cost=None):
    """Averages row the way outcomes_averages writes it: the completion
    record is n_completed / n_success_known, NEVER guessed from n_samples."""
    row = {"source_system": "hermes", "provider": provider, "model": model,
           "complexity": None, "n_samples": n, "n_completed": completed,
           "n_success_known": known,
           "success_rate": (completed / known) if known else None}
    if cost is not None:
        row["avg_cost_task_24h"] = cost
    return row


#: the live shape from the finding: 738 samples, 417 known, 114 completed.
BAD_FAMILY = [_family_row(FAMILY, TERRA, 738, 417, 114, cost=0.9)]


def _order(result):
    return [f"{e['provider']}/{e['model']}" for e in result["chain"]]


def _ex(result, model):
    return next((e for e in result["exclusions"] if e["model"] == model), None)


# ------------------------------------------- 1. family stats attribution ---

def test_family_stats_aggregates_every_provider_measuring_the_model():
    index = {(FAMILY, TERRA): [_family_row(FAMILY, TERRA, 738, 417, 114)],
             (VARIANT, TERRA): [_family_row(VARIANT, TERRA, 17, 17, 3)],
             (FAMILY, "other-model"): [_family_row(FAMILY, "other-model", 5, 5, 5)]}
    ctx = {"index": index}
    fam = router_spawn._model_family_stats(ctx, TERRA)
    assert fam["provider"] == FAMILY           # the heaviest witness
    assert fam["n_success_known"] == 434       # 417 + 17, the WHOLE family
    assert fam["n_completed"] == 117
    assert fam["success_rate"] == pytest.approx(117 / 434)
    # a model nobody measured has no family: None-honest, never zeros
    assert router_spawn._model_family_stats(ctx, "never-run") is None


def test_outcome_note_borrows_the_family_and_says_so(monkeypatch, tmp_path):
    """AC3: a variant lane with no rows of its own consults the family
    aggregate and the outcomes block LABELS the borrow. The gate is disabled
    here (its exclusion would drop the lane from the chain entirely — that
    is AC2's business); this pins the provenance ALONE."""
    _wire(monkeypatch, tmp_path, averages=BAD_FAMILY)
    # spec positions: floor:cov:ceil:weight:explore:min_known (explore 0,
    # min_known 0 — the gate OFF, so the provenance is pinned alone)
    r = router_spawn.resolve(project="proj",
                             sort="predicted_cost_per_task:3:0:9:5:0:0")
    hop = next(e for e in r["chain"] if e["model"] == TERRA)
    note = hop["outcomes"]
    assert note["matched"] is None              # the lane has NO own row
    assert note["stats_fallback"] == "model-family"
    assert note["stats_fallback_family"] == FAMILY
    assert note["family_n_success_known"] == 417
    assert note["family_n_completed"] == 114
    assert note["family_success_rate"] == pytest.approx(114 / 417)


def test_a_lane_with_its_own_rows_never_borrows(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[
        _family_row(VARIANT, TERRA, 100, 100, 90, cost=0.2)])
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    hop = next(e for e in r["chain"] if e["model"] == TERRA)
    assert hop["outcomes"]["stats_fallback"] == "unconditioned"
    assert "stats_fallback_family" not in hop["outcomes"]


# ------------------------------------------------- 2. the exclusion gate ---

def test_the_gate_excludes_the_catastrophic_family_lane_and_heads_the_alternative(
        monkeypatch, tmp_path):
    """AC2+AC5: terra (27.3% over 417 known via its family) may NOT head a
    chain that has an alternative clearing the same profile; the exclusion is
    NAMED with code outcome-floor and the reason names the model."""
    _wire(monkeypatch, tmp_path, averages=BAD_FAMILY)
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert r["head"]["model"] == ALT[1], "the catastrophic lane must not head"
    ex = _ex(r, TERRA)
    assert ex is not None, "the exclusion must be visible, never a silent re-rank"
    assert "outcome-floor" in ex["codes"]
    assert any(TERRA in w for w in ex["why"])
    # the sort basis carries the gate's own audit block
    of = r["sort_stats"]["sufficiency"]["outcome_floor"]
    assert of["excluded"][0]["model"] == TERRA
    assert of["excluded"][0]["source"] == "model-family"
    assert of["excluded"][0]["n_success_known"] == 417
    assert of["min_known"] == 8 and of["floor_rate"] == 0.35


def test_the_gate_uses_the_lanes_own_rows_first(monkeypatch, tmp_path):
    """A lane with its OWN catastrophic record is excluded even when no
    sibling measured the model (source 'lane', no family involved)."""
    _wire(monkeypatch, tmp_path, averages=[
        _family_row(VARIANT, TERRA, 50, 40, 4, cost=0.01)])
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert r["head"]["model"] == ALT[1]
    ex = _ex(r, TERRA)
    assert "outcome-floor" in ex["codes"]
    of = r["sort_stats"]["sufficiency"]["outcome_floor"]
    assert of["excluded"][0]["source"] == "lane"
    assert of["excluded"][0]["n_success_known"] == 40


def test_a_healthy_lane_passes_and_nothing_is_excluded(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[
        _family_row(VARIANT, TERRA, 100, 100, 90, cost=0.2)])
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _ex(r, TERRA) is None
    assert r["head"]["model"] == TERRA
    # the gate ran and judged nothing exclusionary — visible, never silent
    of = r["sort_stats"]["sufficiency"]["outcome_floor"]
    assert of["excluded"] == []
    assert of["min_known"] == 8 and of["floor_rate"] == 0.35


def test_below_the_known_floor_a_poor_record_is_not_exclusionary(
        monkeypatch, tmp_path):
    """Thin evidence is not a verdict: 7 known outcomes never gate, whatever
    the rate — same doctrine as the TR-183 sample floor."""
    _wire(monkeypatch, tmp_path, averages=[
        _family_row(FAMILY, TERRA, 700, 7, 0)])
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _ex(r, TERRA) is None
    assert r["head"]["model"] == TERRA


def test_floor_knobs_are_settable_per_spec(monkeypatch, tmp_path):
    """Spec positions 6/7: predicted_cost_per_task:<floor>:<cov>:<ceil>:<w>:<explore>:<min_known>:<rate>.

    The fixture (9 known, rate 0.78) PASSES the default floors (9 >= 8 but
    0.78 > 0.35); the spec tightens to min_known 9 / rate 0.8 and the lane
    gates — both spec positions are live and land in the audit block."""
    _wire(monkeypatch, tmp_path, averages=[
        _family_row(VARIANT, TERRA, 50, 9, 7, cost=0.01)])   # rate 0.78
    r = router_spawn.resolve(project="proj",
                             sort="predicted_cost_per_task:3:0:9:5:0:9:0.8")
    ex = _ex(r, TERRA)
    assert ex is not None, "9 >= spec min_known 9 AND 0.78 <= spec rate 0.8: gated"
    assert "outcome-floor" in ex["codes"]
    of = r["sort_stats"]["sufficiency"]["outcome_floor"]
    assert of["min_known"] == 9 and of["floor_rate"] == 0.8
    # the same data under the DEFAULT floors passes (0.78 > 0.35)
    r2 = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _ex(r2, TERRA) is None


def test_env_overrides_and_the_zero_switch_disables_the_gate(
        monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=BAD_FAMILY)
    monkeypatch.setenv("ROUTER_OUTCOME_FLOOR_RATE", "0.2")
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _ex(r, TERRA) is None, "0.27 > the 0.2 env floor: passes"
    monkeypatch.setenv("ROUTER_OUTCOME_FLOOR", "0")
    r2 = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert _ex(r2, TERRA) is None
    assert "outcome_floor" not in r2["sort_stats"]["sufficiency"]


def test_a_fully_gated_chain_still_serves_the_fallback(monkeypatch, tmp_path):
    """Fail-open is sacred: every primary lane gated on outcomes still
    resolves — the 'fb' always-run lane wins the chain, both gated lanes are
    NAMED with the outcome-floor code, and the resolve is never an error."""
    _wire(monkeypatch, tmp_path, averages=[
        _family_row(VARIANT, TERRA, 50, 40, 4),
        _family_row(ALT[0], ALT[1], 50, 40, 4)])
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert "error" not in r
    assert r["head"]["model"] == "always-run"
    assert {e["model"] for e in r["exclusions"]} == {TERRA, ALT[1]}
    assert all("outcome-floor" in e["codes"] for e in r["exclusions"])


def test_early_gate_exclusions_still_carry_the_outcome_floor_code(
        monkeypatch, tmp_path):
    """TR-354 shadowing fix: a lane an EARLIER gate excluded (model SLOW)
    still carries the outcome-floor verdict on its exclusion row — the
    outcome gate is consulted for every eligible lane, whatever else gated
    it, so the code is never silenced by another gate's prose."""
    _wire(monkeypatch, tmp_path, averages=BAD_FAMILY)
    d = tmp_path / "state"
    hs = json.load(open(d / "health-state.json"))
    hs["providers"][VARIANT] = {"status": "SLOW", "latency_ms": 9000,
                                "ts": "2026-10-29T00:00:00+00:00"}
    json.dump(hs, open(d / "health-state.json", "w"))
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    ex = _ex(r, TERRA)
    assert ex is not None
    assert "outcome-floor" in ex["codes"], (
        f"the outcome verdict must not be shadowed by the earlier gate: {ex}")
    assert any("health SLOW" in w for w in ex["why"]), ex
    assert any(TERRA in w and "417 known outcomes" in w for w in ex["why"])


def test_the_exclusion_code_is_in_the_vocabulary():
    assert "outcome-floor" in router_spawn.EXCLUSION_REASON_CODES
    assert router_spawn.exclusion_codes(
        ["outcome-floor: openai/gpt-5.6-terra family xkiro has 417 known "
         "outcomes with completion rate 0.27"]) == ["outcome-floor"]


# --------------------------- proxy-stats surface: the family grouping ----

def _proxy_row(provider, model, success=True, ts=None):
    import router_proxy_stats as rps
    return {"source_system": "router-proxy", "provider": provider,
            "model": model, "required_categories": {"code_gen": 2},
            "success": success, "cost_usd": 0.02, "wall_time_s": 1.0,
            "ts": ts if ts is not None else (rps and 1_800_000_000.0 - 60)}


def test_proxy_stats_model_family_grouping_merges_provider_variants(tmp_path):
    """TR-354 family aggregation on the proxy-stats surface: xkiro and
    xkiro-2 collapse into one family:<model> bucket that DISCLOSES the
    merge (who contributed what), with the heaviest witness as `provider`."""
    import router_proxy_stats as rps
    rows = [_proxy_row("xkiro", TERRA, success=True),
            _proxy_row("xkiro", TERRA, success=False),
            _proxy_row("xkiro", TERRA, success=False),
            _proxy_row(VARIANT, TERRA, success=False),
            _proxy_row("openrouter", TERRA, success=True)]
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    data = rps.get_rollup(str(path), windows=(24,), now=1_800_000_000.0,
                          grouping="model_family")
    groups = data["24h"]["groups"]
    assert set(groups) == {"family:" + TERRA}
    fam = groups["family:" + TERRA]
    assert fam["samples"] == 5
    assert fam["success_rate"] == pytest.approx(0.4)
    assert fam["family_providers"] == ["openrouter", "xkiro", VARIANT]
    assert fam["samples_by_provider"] == {"openrouter": 1, VARIANT: 1,
                                          "xkiro": 3}
    assert fam["provider"] == "xkiro"          # the heaviest witness


def test_proxy_stats_model_family_is_a_distinct_opt_in_grouping(tmp_path):
    import router_proxy_stats as rps
    rows = [_proxy_row("xkiro", TERRA), _proxy_row(VARIANT, TERRA)]
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    lane = rps.get_rollup(str(path), windows=(24,), now=1_800_000_000.0,
                          grouping="model")["24h"]["groups"]
    assert set(lane) == {f"xkiro/{TERRA}", f"{VARIANT}/{TERRA}"}  # unchanged
    assert "family:" + TERRA not in lane
