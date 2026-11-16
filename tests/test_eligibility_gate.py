"""ELIGIBILITY GATE unit tests (owner ruling 2026-11-11).

Board acceptance — "no model appears on a list it cannot act on": a live lane
is on a resolver chain only if it carries a normalized_price AND at least one
measured capability row (model_perf.perf or benchmarks.score). Every lane that
cannot act leaves the chain with a NAMED reason (`ineligible:unpriced` /
`ineligible:no-evidence`), and the payload's `eligibility` block counts the
same verdicts the chain was built from, so the census and the chain can never
disagree.

Pinned here:
  (a) a priced lane with NO measured row leaves the chain, named
  (b) a lane with a perf row stays; a bench-only lane stays too
  (c) an unpriced lane is named ineligible:unpriced (and still excluded)
  (d) ROUTER_ELIGIBILITY=0 restores the legacy chain byte-for-byte
  (e) an alias variant inherits its base's evidence (a rename is not a gap)
  (f) the census counts LIVE lanes only, and names the gaps (no silent zeros)
  (g) fail-open: a census that raises leaves the gate inert, the resolve alive
  (h) the eligibility block is on EVERY payload, incl. the disabled case
  (i) the gate never moves a MEASURED hop (order + hop numbers unchanged)

Hermetic: mini registry + scratch state dir under tmp_path. Live fleet state is
never read or written.
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import router_spawn  # noqa: E402

CAT = "agent_tick"
EVID = "measured-model"       # priced + a perf row
BENCH = "bench-model"         # priced + only a bench row
NOEV = "silent-model"         # priced, NEITHER perf nor bench
FREE = "unpriced-model"       # measured, but no price
ALIAS_V = "vendor/alias-model"  # a variant id that maps to EVID's base
ALIAS_BASE = "alias-model"
P = "prov"
P2 = "prov2"


def _tables():
    models = [
        {"provider": P, "model": EVID, "normalized_price": 0.02,
         "data_class": "open", "context_limit": 200000},
        {"provider": P, "model": BENCH, "normalized_price": 0.03,
         "data_class": "open", "context_limit": 200000},
        {"provider": P, "model": NOEV, "normalized_price": 0.01,
         "data_class": "open", "context_limit": 200000},
        {"provider": P2, "model": FREE, "normalized_price": None,
         "data_class": "open", "context_limit": 200000},
        {"provider": P2, "model": ALIAS_V, "normalized_price": 0.04,
         "data_class": "open", "context_limit": 200000},
        # out of service: neither must reach the census
        {"provider": P, "model": "retired-model", "normalized_price": 0.05,
         "valid_to": "2020-01-01", "data_class": "open",
         "context_limit": 200000},
        {"provider": P, "model": "disabled-model", "normalized_price": 0.05,
         "disabled": True, "data_class": "open", "context_limit": 200000},
    ]
    return {
        "providers": [{"id": P}, {"id": P2}],
        "models": models,
        "model_tier": [{"model": m, "category": CAT, "tier": 5}
                       for m in (EVID, BENCH, NOEV, FREE, ALIAS_V,
                                 "retired-model", "disabled-model")],
        "model_perf": [
            {"model": EVID, "category": CAT, "perf": 0.9, "source": "probe"},
            {"model": FREE, "category": CAT, "perf": 0.8, "source": "probe"},
            {"model": ALIAS_BASE, "category": CAT, "perf": 0.7,
             "source": "probe"},
            {"model": NOEV, "category": CAT, "perf": None, "source": "probe"},
        ],
        "benchmarks": [
            {"model": BENCH, "category": CAT, "score": 0.5, "max_score": 1.0,
             "source": "bench"},
            {"model": NOEV, "category": CAT, "score": None, "max_score": 1.0,
             "source": "bench"},
        ],
        "model_aliases": [],
        "category_levels": [{"category": CAT, "level": 5, "label": "q95",
                             "min_perf": 0.9}],
        "level_defs": [{"level": lvl, "label": str(lvl)} for lvl in range(-5, 6)],
        "fallback_lanes": [],
        "projects": [{"id": "proj", "profile": "TP"}],
        "task_profiles": [{"id": "TP", "title": "test profile"}],
        "task_profile_requirements": [{"task_id": "TP", "category": CAT,
                                       "level": 5}],
    }


def _wire(monkeypatch, tmp_path, tables=None):
    tables = tables or _tables()
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    monkeypatch.setattr(router_spawn, "REGISTRY", str(reg))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    json.dump({"providers": {r["id"]: {"status": "open"}
                             for r in tables["providers"]}},
              open(state / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(state / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(state / "circuit-state.json", "w"))
    monkeypatch.setattr(router_spawn, "MR", str(state))
    monkeypatch.setenv("ROUTING_AVERAGES_FILE", str(tmp_path / "avg.jsonl"))
    for var in ("ROUTER_ELIGIBILITY", "ROUTER_HOP0", "ROUTER_OUTCOME_FLOOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ROUTER_OUTCOME_FLOOR", "0")
    return tables


def _resolve(**kw):
    return router_spawn.resolve(project="proj", **kw)


def _lanes(result):
    return [(h["provider"], h["model"]) for h in result.get("chain") or []]


def test_priced_lane_without_evidence_leaves_the_chain(monkeypatch, tmp_path):
    """(a) NOEV is priced and tiered — it used to sit on the chain anyway."""
    _wire(monkeypatch, tmp_path)
    r = _resolve()
    assert (P, NOEV) not in _lanes(r)
    assert r["eligibility"]["enabled"] is True
    assert r["eligibility"]["counts"][router_spawn.ELIG_NO_EVIDENCE] >= 1
    named = {(n["provider"], n["model"]) for n in r["eligibility"]["named"]}
    assert (P, NOEV) in named


def test_measured_and_bench_only_lanes_stay(monkeypatch, tmp_path):
    """(b) evidence means perf OR bench — neither is a second-class citizen."""
    _wire(monkeypatch, tmp_path)
    lanes = _lanes(_resolve())
    assert (P, EVID) in lanes and (P, BENCH) in lanes


def test_unpriced_lane_is_named_ineligible_unpriced(monkeypatch, tmp_path):
    """(c) the unpriced lane is excluded AND carries the eligibility name."""
    _wire(monkeypatch, tmp_path)
    r = _resolve(why=True)
    assert (P2, FREE) not in _lanes(r)
    assert r["eligibility"]["counts"][router_spawn.ELIG_UNPRICED] >= 1
    codes = {c for e in (r.get("why") or {}).get("excluded") or []
             for c in e.get("codes") or []}
    assert router_spawn.ELIG_UNPRICED in codes
    assert router_spawn.ELIG_NO_EVIDENCE in codes


def test_kill_switch_restores_the_legacy_chain(monkeypatch, tmp_path):
    """(d) ROUTER_ELIGIBILITY=0 is the documented escape hatch, and it says so."""
    _wire(monkeypatch, tmp_path)
    on = _resolve()
    monkeypatch.setenv("ROUTER_ELIGIBILITY", "0")
    off = _resolve()
    assert (P, NOEV) in _lanes(off)
    assert (P, NOEV) not in _lanes(on)
    assert off["eligibility"]["enabled"] is False
    assert "0" in off["eligibility"]["reason"]
    # malformed value keeps the documented default (ON) — a typo must not undo
    # an owner ruling silently
    monkeypatch.setenv("ROUTER_ELIGIBILITY", "banana")
    typo = _resolve()
    assert typo["eligibility"]["enabled"] is True


def test_alias_variant_inherits_its_base_evidence(monkeypatch, tmp_path):
    """(e) a rename is not a measurement gap."""
    tables = _tables()
    tables["model_aliases"] = [{"model": ALIAS_V, "inherits": ALIAS_BASE}]
    _wire(monkeypatch, tmp_path, tables)
    assert (P2, ALIAS_V) in _lanes(_resolve())


def test_census_counts_live_lanes_only_and_names_the_gaps(monkeypatch, tmp_path):
    """(f) out-of-service rows are not measurement gaps (TR-363 doctrine)."""
    tables = _wire(monkeypatch, tmp_path)
    census = router_spawn.eligibility_census(tables)
    live = {m["model"] for m in tables["models"]
            if not m.get("disabled") and not router_spawn.row_is_retired(m)}
    assert census["live_lanes"] == len(live)
    assert "retired-model" not in json.dumps(census["named"])
    assert "disabled-model" not in json.dumps(census["named"])
    # NOEV and the (here unmapped) ALIAS_V variant are the two no-evidence
    # lanes; the perf/bench rows exist for exactly three models.
    assert census["counts"][router_spawn.ELIG_NO_EVIDENCE] == 2
    assert census["counts"][router_spawn.ELIG_UNPRICED] == 1
    assert census["evidence_models"] == 4  # EVID, BENCH, FREE, ALIAS_BASE


def test_census_error_leaves_the_gate_inert_and_the_resolve_alive(
        monkeypatch, tmp_path):
    """(g) fail-open is sacred: a broken census never blocks a resolve."""
    _wire(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("census exploded")

    monkeypatch.setattr(router_spawn, "eligibility_census", _boom)
    r = _resolve()
    assert not r.get("error")
    assert r["eligibility"]["enabled"] is False
    assert "census-error" in r["eligibility"]["reason"]
    assert (P, NOEV) in _lanes(r)          # nothing was judged by a broken read


def test_eligibility_block_is_on_every_payload(monkeypatch, tmp_path):
    """(h) the rule is never off the record, on or off."""
    _wire(monkeypatch, tmp_path)
    for env in (None, "0"):
        if env is None:
            monkeypatch.delenv("ROUTER_ELIGIBILITY", raising=False)
        else:
            monkeypatch.setenv("ROUTER_ELIGIBILITY", env)
        block = _resolve()["eligibility"]
        assert block["rule"] == router_spawn.ELIGIBILITY_RULE
        assert "enabled" in block and "reason" in block


def test_profile_tag_missing_model_tier_is_excluded_with_named_reason():
    """A true capability flag cannot bypass the model_tier tag gate."""
    tables = _tables()
    tables["models"][0]["vision"] = True
    misses = []
    rows = router_spawn._build_chain(
        tables, [("vision", 1)], misses_out=misses,
        eligibility_ctx={"enabled": True})
    assert not any((p, m) == (P, EVID) for _h, p, m, *_rest in rows)
    rec = next(e for e in misses if e["provider"] == P and e["model"] == EVID)
    assert "capability-flag-tier" in rec["codes"]
    assert any("vision=1" in reason and "tier=-1" in reason
               for reason in rec["reasons"])


def test_gate_never_moves_a_measured_hop(monkeypatch, tmp_path):
    """(i) eligibility REORDERS nothing: measured lanes keep hop + order."""
    _wire(monkeypatch, tmp_path)
    tables = _tables()
    ev = router_spawn.evidence_index(tables)
    on = _lanes(_resolve())
    monkeypatch.setenv("ROUTER_ELIGIBILITY", "0")
    off = _lanes(_resolve())
    assert on == [lane for lane in off
                  if router_spawn._has_evidence(lane[1], ev, {m.lower() for m in ev})]
    assert (P2, ALIAS_V) not in on   # unmapped here: no rows, no claim
    monkeypatch.delenv("ROUTER_ELIGIBILITY", raising=False)
    assert [h["hop"] for h in _resolve()["chain"]] == list(
        range(1, len(on) + 1))
