"""TR-049 components 4+5 — per-backend outcome stats and pluggable sort keys.

Hermetic: a mini registry + a scratch averages table + a scratch state dir, all
under tmp_path. The live fleet state is never read or written.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import router_spawn  # noqa: E402

CAT = "agent_tick"
LANES = [("prov-a", "a-expensive", 9.0),
         ("prov-b", "b-cheap", 1.0),
         ("prov-c", "c-mid", 5.0)]


def _tables():
    return {
        "providers": [{"id": p} for p, _m, _pr in LANES],
        "models": [{"provider": p, "model": m, "normalized_price": pr,
                    "data_class": "open", "context_limit": 200000}
                   for p, m, pr in LANES],
        "model_tier": [{"model": m, "category": CAT, "tier": 5} for _p, m, _pr in LANES],
        "category_levels": [{"category": CAT, "level": 5, "label": "q95", "min_perf": 0.9}],
        "level_defs": [{"level": lvl, "label": str(lvl)} for lvl in range(-5, 6)],
        "fallback_lanes": [],
        "projects": [{"id": "proj", "profile": "TP"}],
        "task_profiles": [{"id": "TP", "title": "test profile"}],
        "task_profile_requirements": [{"task_id": "TP", "category": CAT, "level": 5}],
    }


def _state(tmp_path, tables):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"providers": {r["id"]: {"status": "open"} for r in tables["providers"]}},
              open(d / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(d / "circuit-state.json", "w"))
    return str(d)


def _wire(monkeypatch, tmp_path, tables=None, averages=None):
    """Mini registry + scratch state dir + scratch averages file (env-pinned)."""
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


def _row(provider, model, cost=None, wall=None, turns=None, n=10,
         source="hermes", complexity=None):
    row = {"source_system": source, "provider": provider, "model": model,
           "complexity": complexity, "n_samples": n, "n_completed": 0,
           "n_success_known": 0, "success_rate": None}
    if cost is not None:
        row["avg_cost_task_24h"] = cost
    if wall is not None:
        row["avg_wall_time_24h"] = wall
    if turns is not None:
        row["avg_turns_24h"] = turns
    return row


def _order(result):
    return [f"{e['provider']}/{e['model']}" for e in result["chain"]]


# ---------------------------------------------------- component 4: isolation ---

def test_default_is_merged_across_backends(monkeypatch, tmp_path):
    """No --backend: stats merge every source_system (sample-count weighted)."""
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-a", "a-expensive", cost=1.0, n=1, source="hermes"),
        _row("prov-a", "a-expensive", cost=11.0, n=9, source="opencode"),
    ])
    index, meta = router_spawn.load_outcome_stats()
    assert meta["source"] == "merged" and meta["error"] is None
    assert len(index[("prov-a", "a-expensive")]) == 1
    assert index[("prov-a", "a-expensive")][0]["avg_cost_task_24h"] == pytest.approx(10.0)


def test_backend_flag_isolates_the_lookup(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-a", "a-expensive", cost=1.0, n=1, source="hermes"),
        _row("prov-a", "a-expensive", cost=11.0, n=9, source="opencode"),
    ])
    index, meta = router_spawn.load_outcome_stats(backend="hermes")
    assert meta["source"] == "backend:hermes"
    assert index[("prov-a", "a-expensive")][0]["avg_cost_task_24h"] == pytest.approx(1.0)
    # merge wins over the backend filter (documented precedence)
    merged, meta2 = router_spawn.load_outcome_stats(backend="hermes", merge_backends=True)
    assert meta2["source"] == "merged"
    assert merged[("prov-a", "a-expensive")][0]["avg_cost_task_24h"] == pytest.approx(10.0)


def test_unknown_backend_is_fail_open(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[_row("prov-a", "a-expensive", cost=1.0)])
    index, meta = router_spawn.load_outcome_stats(backend="nope")
    assert index == {} and "no samples" in meta["error"]


def test_missing_averages_file_is_fail_open(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)          # file does not exist at all
    index, meta = router_spawn.load_outcome_stats()
    assert index == {} and meta["rows"] == 0
    assert meta["error"]                     # the gap is reported, not hidden


def test_resolve_reports_stats_provenance(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-b", "b-cheap", cost=0.5, wall=10.0, turns=2),
    ])
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert "error" not in r
    assert r["sort"] == "predicted_cost_per_task"
    assert r["sort_stats"]["source"] == "merged"
    assert r["sort_stats"]["window_h"] == 24
    assert r["sort_stats"]["loaded"] is True
    # the lane with a sample carries its stats on the hop
    hop = next(e for e in r["chain"] if e["provider"] == "prov-b")
    assert hop["outcomes"]["matched"] == "unconditioned"
    assert hop["outcomes"]["predicted_cost_per_task"] == pytest.approx(0.5)
    assert hop["outcomes"]["avg_wall_time_s"] == pytest.approx(10.0)


def test_resolve_without_stats_still_reports_the_gap(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)          # no averages file
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    assert "error" not in r
    assert r["sort_stats"]["problem"]
    assert _order(r) == ["prov-b/b-cheap", "prov-c/c-mid", "prov-a/a-expensive"]


def test_predicted_cost_per_task_reorders_by_stats(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-a", "a-expensive", cost=0.01),   # cheap per TASK, dear per token
    ])
    # The coverage gate is disabled on purpose: the subject here is the reordering RULE.
    # That the gate protects an under-measured chain is test_sort_sufficiency_floor.py's job.
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task:3:0")
    assert _order(r)[0] == "prov-a/a-expensive"
    # a lane with no sample keeps its price-proxy rank instead of sorting free
    assert _order(r)[1:] == ["prov-b/b-cheap", "prov-c/c-mid"]


def test_backend_isolation_changes_the_order(monkeypatch, tmp_path):
    """Isolated stats (hermes only) vs merged stats pick different heads."""
    _wire(monkeypatch, tmp_path, averages=[
        # n must clear the sufficiency floor (TR-183): a ONE-sample row is deliberately not
        # usable as a measurement, which is tested in test_sort_sufficiency_floor.py. This test
        # is about backend isolation, so its rows carry enough samples to be measured at all.
        _row("prov-a", "a-expensive", cost=0.01, n=5, source="hermes"),
        _row("prov-c", "c-mid", cost=0.0001, n=99, source="opencode"),
    ])
    merged = router_spawn.resolve(project="proj", sort="predicted_cost_per_task:3:0")
    assert _order(merged)[0] == "prov-c/c-mid"           # merged: c wins
    isolated = router_spawn.resolve(project="proj", sort="predicted_cost_per_task:3:0",
                                    backend="hermes")
    assert isolated["sort_stats"]["backend"] == "hermes"
    assert _order(isolated)[0] == "prov-a/a-expensive"   # c has no hermes sample


# ------------------------------------------------------ component 5: sorting ---

def test_default_sort_keeps_the_legacy_price_order(monkeypatch, tmp_path):
    """Regression guard: with stats available the DEFAULT ordering must still be
    the historical (plan_tier, effective price) order — router_spawn.py is
    symlinked into the live fleet, so a silent re-rank is not acceptable."""
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-a", "a-expensive", cost=0.01),   # cheapest per task
    ])
    legacy = router_spawn.resolve(project="proj")
    assert legacy["sort"] == "price"
    assert _order(legacy) == ["prov-b/b-cheap", "prov-c/c-mid", "prov-a/a-expensive"]
    explicit = router_spawn.resolve(project="proj", sort="price")
    assert _order(explicit) == _order(legacy)


def test_wall_time_sort_and_unknown_is_worst(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-c", "c-mid", wall=5.0, n=5),
        _row("prov-b", "b-cheap", wall=99.0, n=5),
    ])
    r = router_spawn.resolve(project="proj", sort="wall_time")
    assert _order(r)[:2] == ["prov-c/c-mid", "prov-b/b-cheap"]
    assert _order(r)[2] == "prov-a/a-expensive"   # no sample -> unknown -> last


def test_turns_sort(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-b", "b-cheap", turns=30),
        _row("prov-c", "c-mid", turns=3),
        _row("prov-a", "a-expensive", turns=7),
    ])
    assert _order(router_spawn.resolve(project="proj", sort="turns")) == [
        "prov-c/c-mid", "prov-a/a-expensive", "prov-b/b-cheap"]


def test_ratio_mix_is_caller_supplied_and_normalized(monkeypatch, tmp_path):
    """ratio:<w>*<cost>+<w>*<time> — cost and time are normalized across the
    eligible lanes before blending (dollars vs seconds are not comparable raw)."""
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-a", "a-expensive", cost=0.10, wall=100.0),  # cheap, slow
        _row("prov-b", "b-cheap", cost=10.0, wall=1.0),        # dear, fast
    ])
    cost_heavy = router_spawn.resolve(project="proj", sort="ratio:0.9*cost+0.1*time")
    assert _order(cost_heavy)[0] == "prov-a/a-expensive"
    time_heavy = router_spawn.resolve(project="proj", sort="ratio:0.1*cost+0.9*time")
    assert _order(time_heavy)[0] == "prov-b/b-cheap"


def test_window_h_selects_the_average_window(monkeypatch, tmp_path):
    """--window-h picks which decay window feeds the sort."""
    rows = [_row("prov-a", "a-expensive", cost=0.01)]
    rows[0]["avg_cost_task_72h"] = 0.0     # a different value in the 72h window
    _wire(monkeypatch, tmp_path, averages=rows)
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task",
                             window_h=72)
    assert r["sort_stats"]["window_h"] == 72
    hop = next(e for e in r["chain"] if e["provider"] == "prov-a")
    assert hop["outcomes"]["predicted_cost_per_task"] == 0.0
    # the default window reports the OTHER field's sample (0.01), not the 72h one
    other = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    hop24 = next(e for e in other["chain"] if e["provider"] == "prov-a")
    assert hop24["outcomes"]["predicted_cost_per_task"] == pytest.approx(0.01)


def test_parse_ratio_rejects_bad_specs():
    assert router_spawn._parse_ratio("0.7*cost+0.3*time") == [(0.7, "cost"), (0.3, "wall")]
    assert router_spawn._parse_ratio("1*wall_time") == [(1.0, "wall")]
    assert router_spawn._parse_ratio("1*turns,1*cost") == [(1.0, "turns"), (1.0, "cost")]
    for bad in ("", "0.7", "0.7*carrots", "x*cost"):
        with pytest.raises(ValueError):
            router_spawn._parse_ratio(bad)


def test_unknown_sort_key_fails_open_to_price(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, averages=[_row("prov-a", "a-expensive", cost=0.01)])
    r = router_spawn.resolve(project="proj", sort="cheapest-vibes")
    assert "error" not in r                     # never blocks a resolve
    assert r["sort"] == "price"                 # degraded, visibly
    assert r["sort_stats"]["warning"]
    assert _order(r) == ["prov-b/b-cheap", "prov-c/c-mid", "prov-a/a-expensive"]


def test_sort_dispatch_dict_is_the_only_switch():
    """Pluggability contract: adding a key is one dict entry, never a new branch
    inside the comparator."""
    assert set(router_spawn.SORT_KEYS) == {
        "price", "predicted_cost_per_task", "wall_time", "turns", "ratio"}
    assert router_spawn.DEFAULT_SORT in router_spawn.SORT_KEYS

def test_the_response_says_how_much_of_the_order_rested_on_measurement(monkeypatch, tmp_path):
    """TR-183 precondition: 'ranked by measured cost' must be auditable from the response."""
    _wire(monkeypatch, tmp_path, averages=[
        _row("prov-a", "a-expensive", cost=0.01, n=5),        # clears the floor
        _row("prov-b", "b-cheap", cost=0.9, n=1),             # one sample -> not evidence
    ])
    r = router_spawn.resolve(project="proj", sort="predicted_cost_per_task")
    sup = r["sort_stats"]["sufficiency"]
    assert sup["floor_samples"] == 3
    assert sup["ranked_on_measurement"] == 1 and sup["fell_back_to_price"] == 2
    assert sup["lanes"] == 3
    # 1 of 3 candidates measured is under the coverage bar, so the ordering degrades and SAYS so
    assert sup["effective"] == "price" and sup["reason"] == "below-coverage-floor"
    assert sup["coverage"] == round(1 / 3, 4)
    # and the price sort reports no sufficiency claim at all, rather than a zero
    legacy = router_spawn.resolve(project="proj")
    assert legacy["sort_stats"]["sufficiency"] is None
