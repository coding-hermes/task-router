"""TR-213 — Quota: chain preference integration.

The L3 policy consumer #2 from docs/quota-layers-spec.md §5: lane ordering
gains ONE term — prefer lanes whose window has headroom, deprioritise lanes
near their ceiling — while keeping every existing rule intact. Quota is a
tie-breaker and a guard, NEVER a silent re-rank of the fleet:

AC1  headroom influences ordering only among otherwise-comparable lanes
     (price ties broken by headroom; unequal prices still decide by price)
AC2  no silent re-rank: with no quota limits configured the chain is
     byte-identical to today, and with a demotion the legacy
     (PAYG-bucket, plan_tier, price) key stays non-decreasing
AC3  PAYG-last and plan-before-PAYG unchanged even when the PAYG lane has
     the better headroom
AC4  the resolve output states when a lane was deprioritised for low
     headroom (hop flag + quota_ordering block), and unknown headroom is
     never treated as low (NULL with a reason, never 0)

Hermetic: scratch limits config + scratch outcome ledger via the documented
env overrides (ROUTER_QUOTA_LIMITS_FILE / ROUTING_OUTCOMES_FILE); the live
fleet state is never read or written.
"""
import json
import os
import sys
import time

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import router_spawn  # noqa: E402
import router_quota_accounting as rqa  # noqa: E402

CAT = "agent_tick"
#: two same-price same-tier plan lanes (the AC1 tie), one distinct-price lane
LANES = [("prov-a", "a-lane", 1.0, 0),
         ("prov-b", "b-lane", 1.0, 0),
         ("prov-c", "c-lane", 5.0, 0)]


def _tables(payg=None):
    payg = payg or set()
    return {
        "providers": [{"id": p, "plan": ("PAYG" if p in payg else "subscription")}
                      for p, _m, _pr, _pt in LANES],
        "models": [{"provider": p, "model": m, "normalized_price": pr,
                    "token_factor": 1.0, "plan_tier": pt, "data_class": "zdr",
                    "valid_to": None, "archive": False, "context_limit": 200000}
                   for p, m, pr, pt in LANES],
        "model_tier": [{"model": m, "category": CAT, "tier": 5}
                       for p, m, _pr, _pt in LANES],
        "task_profiles": [{"id": "TP", "title": "t"}],
        "task_profile_requirements": [{"task_id": "TP", "category": CAT, "level": 5}],
        "projects": [{"id": "proj", "profile": "TP"}],
        "fallback_lanes": [],
    }


def _limits(path, providers):
    """A scratch L2 limit config (data/quota_limits.json shape)."""
    doc = {"default_window": "rolling_5h", "providers": providers}
    with open(path, "w") as f:
        json.dump(doc, f)
    return str(path)


def _ledger(path, rows, now=None):
    """A scratch outcome ledger; rows: (provider, spent_usd)."""
    now = now if now is not None else time.time()
    with open(path, "w") as f:
        for prov, spent in rows:
            f.write(json.dumps({
                "source_system": "hermes", "session_id": f"s-{prov}",
                "provider": prov, "model": "x", "ts": now - 60,
                "tokens_in": 100, "tokens_out": 100, "cost_usd": spent,
            }) + "\n")
    return str(path)


def _wire(monkeypatch, tmp_path, tables=None, limits=None, ledger_rows=None,
          guard=None):
    """Mini registry + scratch state + scratch quota inputs, all env-pinned."""
    tables = tables or _tables()
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    monkeypatch.setattr(router_spawn, "REGISTRY", str(reg))
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"providers": {r["id"]: {"status": "open"} for r in tables["providers"]}},
              open(d / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(d / "circuit-state.json", "w"))
    monkeypatch.setattr(router_spawn, "MR", str(d))
    monkeypatch.setenv("ROUTING_AVERAGES_FILE", str(tmp_path / "averages.jsonl"))
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "outcomes.jsonl"))
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE", str(tmp_path / "quota_limits.json"))
    if limits is not None:
        _limits(tmp_path / "quota_limits.json", limits)
    if ledger_rows is not None:
        _ledger(tmp_path / "outcomes.jsonl", ledger_rows)
    if guard is not None:
        monkeypatch.setenv("ROUTER_QUOTA_ORDER_GUARD", str(guard))
    return tables


def _resolve(monkeypatch, tmp_path, tables, **kw):
    return router_spawn.resolve(project="proj", sort="price", **kw)


def _order(r):
    return [f"{e['provider']}/{e['model']}" for e in r["chain"]]


def _hop(r, pair):
    return next(e for e in r["chain"] if f"{e['provider']}/{e['model']}" == pair)


def _keys(tables, r):
    """The legacy no-silent-re-rank key recomputed from the registry data."""
    payg = {p["id"] for p in tables["providers"]
            if str(p.get("plan") or "").upper() == "PAYG"}
    price = {f'{m["provider"]}/{m["model"]}': m["normalized_price"]
             for m in tables["models"]}
    out, prev = [], None
    for e in r["chain"]:
        pair = f'{e["provider"]}/{e["model"]}'
        k = (1 if e["provider"] in payg else 0, 0, price[pair])
        out.append(k)
        if prev is not None:
            assert k >= prev, f"order violation {prev} -> {k} at {pair}"
        prev = k
    return out


# ---------------------------------------------------------------- AC1: ties ---

def test_price_tie_broken_by_headroom(monkeypatch, tmp_path):
    """AC1: two same-price same-tier lanes — the one NEAR ITS CEILING loses
    the tie to the lane with headroom. Never fires without the config."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0},
                  "prov-b": {"window": "rolling_5h", "limit_usd": 1000.0}},
          ledger_rows=[("prov-a", 9.999),   # headroom ~1e-4/18e3 ≈ 5.6e-9 < guard
                       ("prov-b", 1.0)])    # headroom 999/18e3 ≈ 0.055 ≥ guard
    r = _resolve(monkeypatch, tmp_path, None)
    assert "error" not in r, r.get("error")
    assert _order(r)[0] == "prov-b/b-lane", _order(r)
    assert _order(r)[1] == "prov-a/a-lane"
    # ...and without the ledger spend prov-a is not near its ceiling: tie keeps
    # the historical model-name order (a before b).


def test_price_tie_unbroken_when_all_healthy(monkeypatch, tmp_path):
    """AC1 scope: a healthy (non-demoted) lane never moves — the tie-break is
    headroom vs the GUARD, not 'more headroom wins'."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-a": {"window": "rolling_5h", "limit_usd": 1000.0},
                  "prov-b": {"window": "rolling_5h", "limit_usd": 2000.0}},
          ledger_rows=[("prov-a", 1.0), ("prov-b", 1.0)])
    r = _resolve(monkeypatch, tmp_path, None)
    # both healthy -> historical order stands (a before b)
    assert _order(r) == ["prov-a/a-lane", "prov-b/b-lane", "prov-c/c-lane"]
    assert r["quota_ordering"]["deprioritised"] == []


def test_unequal_prices_still_decide_not_headroom(monkeypatch, tmp_path):
    """AC1 boundary: headroom is a tie-breaker ONLY — an expensive lane near
    its ceiling still loses to a healthy cheaper lane (no fleet re-rank)."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-c": {"window": "rolling_5h", "limit_usd": 1000.0},
                  "prov-a": {"window": "rolling_5h", "limit_usd": 10.0}},
          ledger_rows=[("prov-c", 1.0), ("prov-a", 9.999)])
    r = _resolve(monkeypatch, tmp_path, None)
    assert _order(r) == ["prov-a/a-lane", "prov-b/b-lane", "prov-c/c-lane"]


# ------------------------------------------------------- AC2: no silent re-rank

def test_no_limits_configured_is_inert_and_says_so(monkeypatch, tmp_path):
    """AC2: with no limit config the resolve ordering is EXACTLY the historical
    one and quota_ordering.enabled is false with the reason on the block."""
    _wire(monkeypatch, tmp_path, limits={}, ledger_rows=[("prov-a", 9.999)])
    r = _resolve(monkeypatch, tmp_path, None)
    assert _order(r) == ["prov-a/a-lane", "prov-b/b-lane", "prov-c/c-lane"]
    qo = r["quota_ordering"]
    assert qo["enabled"] is False
    assert qo["reason"]
    assert qo["deprioritised"] == []
    for e in r["chain"]:
        assert "deprioritised_for_low_headroom" not in e
        assert "headroom_usd_per_s" not in e


def test_demotion_keeps_legacy_key_non_decreasing(monkeypatch, tmp_path):
    """AC2 (the guard itself, mini): with a demotion ACTIVE the recomputed
    (PAYG-bucket, plan_tier, price) chain key stays non-decreasing — the same
    invariant test_regression pins fleet-wide."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0},
                  "prov-b": {"window": "rolling_5h", "limit_usd": 1000.0}},
          ledger_rows=[("prov-a", 9.999), ("prov-b", 1.0)])
    tables = _tables()
    r = _resolve(monkeypatch, tmp_path, tables)
    _keys(tables, r)   # asserts internally


def test_guard_env_zero_disables_demotion(monkeypatch, tmp_path):
    """The guard is CONFIGURATION, not a baked-in constant: 0 restores the
    pre-TR-213 ordering exactly (and says so)."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0},
                  "prov-b": {"window": "rolling_5h", "limit_usd": 1000.0}},
          ledger_rows=[("prov-a", 9.999), ("prov-b", 1.0)],
          guard=0)
    r = _resolve(monkeypatch, tmp_path, None)
    assert _order(r) == ["prov-a/a-lane", "prov-b/b-lane", "prov-c/c-lane"]
    assert r["quota_ordering"]["deprioritised"] == []


# ---------------------------------------------------- AC3: PAYG-last unchanged

def test_payg_last_unchanged_even_with_better_headroom(monkeypatch, tmp_path):
    """AC3: a PAYG lane with PERFECT headroom still sorts after every plan
    lane; a plan lane near its ceiling does not fall behind PAYG."""
    tables = _tables(payg={"prov-c"})
    _wire(monkeypatch, tmp_path, tables=tables,
          limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0},
                  "prov-b": {"window": "rolling_5h", "limit_usd": 1000.0},
                  "prov-c": {"window": "rolling_5h", "limit_usd": 5000.0}},
          ledger_rows=[("prov-a", 9.999), ("prov-b", 1.0), ("prov-c", 0.0)])
    r = _resolve(monkeypatch, tmp_path, tables)
    assert "error" not in r, r.get("error")
    order = _order(r)
    assert order[0] == "prov-b/b-lane"          # healthy plan lane first
    assert order[-1] == "prov-c/c-lane"         # PAYG LAST, headroom or not
    assert order[1] == "prov-a/a-lane"          # demoted plan lane stays pre-PAYG
    _keys(tables, r)


# ---------------------------------------------- AC4: the resolve states itself

def test_deprioritised_lane_is_flagged_on_hop_and_summary(monkeypatch, tmp_path):
    """AC4: a lane that ACTUALLY lost positions carries the hop flag + reason,
    the summary names it, and the healthy winner carries its headroom."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0},
                  "prov-b": {"window": "rolling_5h", "limit_usd": 1000.0}},
          ledger_rows=[("prov-a", 9.999), ("prov-b", 1.0)])
    r = _resolve(monkeypatch, tmp_path, None)
    a = _hop(r, "prov-a/a-lane")
    b = _hop(r, "prov-b/b-lane")
    assert a["deprioritised_for_low_headroom"] is True
    assert a["headroom_usd_per_s"] is not None
    assert a["headroom_usd_per_s"] < b["headroom_usd_per_s"]
    assert "prov-b/b-lane" in a["deprioritised_reason"]   # names who passed it
    assert "headroom" in a["deprioritised_reason"]
    assert "deprioritised_for_low_headroom" not in b
    assert r["quota_ordering"]["deprioritised"] == ["prov-a/a-lane"]
    assert r["quota_ordering"]["enabled"] is True
    assert r["quota_ordering"]["guard_usd_per_s"] > 0
    assert r["quota_ordering"]["limits_source"].endswith("quota_limits.json")
    assert r["quota_ordering"]["providers_with_limits"] == 2


def test_demoted_but_not_reordered_is_not_flagged(monkeypatch, tmp_path):
    """AC4 precision: a near-ceiling lane that did NOT lose a position (it kept
    its price rank) is not 'deprioritised' — its headroom value states the
    ceiling instead, and the summary does not claim a reorder."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-c": {"window": "rolling_5h", "limit_usd": 10.0}},
          ledger_rows=[("prov-c", 9.999)])   # last lane anyway; nobody to jump it
    r = _resolve(monkeypatch, tmp_path, None)
    c = _hop(r, "prov-c/c-lane")
    assert c["headroom_usd_per_s"] is not None
    assert "deprioritised_for_low_headroom" not in c
    assert r["quota_ordering"]["deprioritised"] == []


def test_unknown_headroom_never_demoted_or_flagged(monkeypatch, tmp_path):
    """AC4 / null law: a provider with NO limit config has UNKNOWN headroom —
    never 0, never 'low'. It keeps its historical rank and carries no flag."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-b": {"window": "rolling_5h", "limit_usd": 1000.0}},
          ledger_rows=[("prov-a", 9.999), ("prov-b", 1.0)])   # prov-a: no limit cfg
    r = _resolve(monkeypatch, tmp_path, None)
    assert _order(r) == ["prov-a/a-lane", "prov-b/b-lane", "prov-c/c-lane"]
    assert "deprioritised_for_low_headroom" not in _hop(r, "prov-a/a-lane")
    assert "headroom_usd_per_s" not in _hop(r, "prov-a/a-lane")
    assert r["quota_ordering"]["deprioritised"] == []


def test_unpriceable_spend_is_unknown_not_low_headroom(monkeypatch, tmp_path):
    """L2 contract at the chain level: a window whose rows cannot be priced has
    UNKNOWN headroom (spent SUM is null) — the lane is never demoted for it."""
    now = time.time()
    _wire(monkeypatch, tmp_path,
          limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0}})
    with open(tmp_path / "outcomes.jsonl", "w") as f:
        f.write(json.dumps({"source_system": "hermes", "session_id": "s1",
                            "provider": "prov-a", "model": "no-price-anywhere",
                            "ts": now - 60, "tokens_in": 500, "tokens_out": 500})
                + "\n")
    r = _resolve(monkeypatch, tmp_path, None)
    assert _order(r) == ["prov-a/a-lane", "prov-b/b-lane", "prov-c/c-lane"]
    assert "deprioritised_for_low_headroom" not in _hop(r, "prov-a/a-lane")
    assert "headroom_usd_per_s" not in _hop(r, "prov-a/a-lane")


# ------------------------------------------------------------------- fail-open

def test_corrupt_limits_config_fails_open(monkeypatch, tmp_path):
    """A broken limit config never blocks a resolve: historical order stands,
    and the block names the gap (visible, not silent)."""
    _wire(monkeypatch, tmp_path)
    with open(tmp_path / "quota_limits.json", "w") as f:
        f.write("{not json")
    r = _resolve(monkeypatch, tmp_path, None)
    assert "error" not in r
    assert _order(r) == ["prov-a/a-lane", "prov-b/b-lane", "prov-c/c-lane"]
    assert r["quota_ordering"]["enabled"] is False
    assert r["quota_ordering"]["reason"]


def test_missing_ledger_is_inert(monkeypatch, tmp_path):
    """No outcome store: limits configured but no spend measured -> headroom
    unknown for everyone (remaining can't be computed from an empty window...
    actually an empty window is a measured $0 spend -> headroom KNOWN and
    healthy). Either way: no demotion, historical order."""
    _wire(monkeypatch, tmp_path,
          limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0},
                  "prov-b": {"window": "rolling_5h", "limit_usd": 1000.0}})
    (tmp_path / "outcomes.jsonl").unlink(missing_ok=True)
    r = _resolve(monkeypatch, tmp_path, None)
    assert _order(r) == ["prov-a/a-lane", "prov-b/b-lane", "prov-c/c-lane"]
    assert r["quota_ordering"]["deprioritised"] == []


# ------------------------------------------------- comparator unit (key shape)

def test_legacy_sort_key_headroom_slot_only_breaks_true_ties():
    """The key itself: headroom term sits AFTER the price slot, so it can
    never reorder lanes with different prices; unknown == healthy."""
    lane = {"provider": "p", "model": "m", "normalized_price": 1.0,
            "plan_tier": 0, "_payg": False}
    demoted = dict(lane, _quota_headroom=1e-9)
    healthy = dict(lane, _quota_headroom=1.0)
    unknown = dict(lane)
    assert router_spawn._legacy_sort_key(healthy) == \
        router_spawn._legacy_sort_key(unknown)
    assert router_spawn._legacy_sort_key(demoted) > \
        router_spawn._legacy_sort_key(healthy)
    dearer = dict(lane, model="z", normalized_price=2.0, _quota_headroom=1e-9)
    assert router_spawn._legacy_sort_key(healthy) < \
        router_spawn._legacy_sort_key(dearer)
    # PAYG bucket still outranks everything, headroom included
    payg = dict(lane, _payg=True, _quota_headroom=1.0)
    assert router_spawn._legacy_sort_key(demoted) < \
        router_spawn._legacy_sort_key(payg)
