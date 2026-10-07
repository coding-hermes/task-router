"""TR-291 regression battery — zero-is-free is never conflated with None-is-unknown.

Found on the Space Bunny lanes (owner 2026-10-03): a lane priced public_price
0.0 / normalized_price 0.0 reached the chain as price=None, effective_price=None
— indistinguishable from an unpriced lane. The law is UNKNOWN IS NOT FREE: a
genuine $0.00 lane must carry 0.0 on the entry, sort ahead of any positive
price, and name the zero in its basis (free-by-promo). A lane with NO declared
price keeps None and never sorts ahead of a measured value.

Fail-open is untouched: no gate behaviour changes, and unpriced lanes keep
their pre-existing (sink) ordering.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import router_spawn  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, "data", "tables")


def _load_tables():
    tables = {}
    for fn in sorted(os.listdir(DATA_DIR)):
        if fn.endswith(".jsonl"):
            name = fn[: -len(".jsonl")]
            rows = [json.loads(l) for l in open(os.path.join(DATA_DIR, fn)) if l.strip()]
            tables[name] = rows
    return tables


def _state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"updated": "test",
                   "providers": {p: {"status": "open"} for p in (
                       "prov-free", "prov-paid", "prov-unknown",
                       "commandcode", "commandcode-2", "opencode-go",
                       "opencode-go-2", "openrouter", "xkiro", "xkiro-2")}},
                  open(d / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(d / "circuit-state.json", "w"))
    return str(d)


def _registry(tmp_path, tables):
    # marker providers must exist in the providers table too (resolve drops
    # lanes whose provider row is absent)
    known = {r["id"] for r in tables.get("providers") or []}
    tables["providers"] = list(tables.get("providers") or []) + [
        {"id": p, "status": "open"} for p in ("prov-free", "prov-paid", "prov-unknown")
        if p not in known]
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": tables}))
    return str(reg)


def _resolve(monkeypatch, tmp_path, tables):
    monkeypatch.setattr(router_spawn, "REGISTRY", _registry(tmp_path, tables))
    monkeypatch.setattr(router_spawn, "MR", _state_dir(tmp_path))
    return router_spawn.resolve(project="coding-hermes-scheduler")


def _free_marker(model="free-marker", provider="prov-free"):
    """A genuine $0 promo lane that clears any tiered requirement."""
    return {"provider": provider, "model": model,
            "public_price": 0.0, "normalized_price": 0.0,
            "public_in_per_m": 0.0, "public_out_per_m": 0.0,
            "plan_tier": 0, "token_factor": 1.0, "data_class": "public"}


def _unpriced_marker(model="unknown-marker", provider="prov-unknown"):
    """Same shape, but NO declared price at all — the None case."""
    return {"provider": provider, "model": model,
            "public_price": None, "normalized_price": None,
            "plan_tier": 0, "token_factor": 1.0, "data_class": "public"}


def _priced_marker(price, model="paid-marker", provider="prov-paid"):
    return {"provider": provider, "model": model,
            "public_price": price, "normalized_price": price,
            "plan_tier": 0, "token_factor": 1.0, "data_class": "public"}


def _with_tiers(tables, *models, tier=5):
    cats = sorted({r["category"] for r in tables["model_tier"]})
    rows = list(tables["model_tier"])
    for m in models:
        rows += [{"model": m, "category": c, "tier": tier} for c in cats]
    tables = dict(tables)
    tables["model_tier"] = rows
    return tables


# ------------------------------------------------------- chain entry carries price

def test_free_chain_entry_carries_zero_not_none(monkeypatch, tmp_path):
    """A public_price 0.0 lane reaches the chain entry as price 0.0 and
    effective_price 0.0 — never None. None is reserved for no declared price."""
    tables = _with_tiers(_load_tables(), "free-marker")
    tables["models"] = list(tables["models"]) + [
        _free_marker(), _priced_marker(0.5)]
    r = _resolve(monkeypatch, tmp_path, tables)
    assert "error" not in r, r.get("error")
    by_model = {c["model"]: c for c in r["chain"]}
    free = by_model["free-marker"]
    assert free["price"] == 0.0 and free["effective_price"] == 0.0, (
        "a genuinely free lane must carry 0.0, not None")


def test_unpriced_lane_never_reaches_the_chain(monkeypatch, tmp_path):
    """UNKNOWN IS NOT FREE, enforced structurally: a lane with NO declared
    price is not eligible at all (the chain only admits priced lanes), so it
    can never take a head position on the strength of an unknown price."""
    tables = _with_tiers(_load_tables(), "unknown-marker")
    tables["models"] = list(tables["models"]) + [_unpriced_marker()]
    r = _resolve(monkeypatch, tmp_path, tables)
    assert "error" not in r, r.get("error")
    assert not [c for c in r["chain"] if c["model"] == "unknown-marker"], (
        "an unpriced lane must never enter the chain")


def test_free_basis_names_the_zero(monkeypatch, tmp_path):
    """The chain entry's basis names the zero (free-by-promo), so the ordering
    is explainable from the row alone; an unpriced lane carries basis None."""
    tables = _with_tiers(_load_tables(), "free-marker", "paid-marker")
    tables["models"] = list(tables["models"]) + [
        _free_marker(), _priced_marker(0.5)]
    r = _resolve(monkeypatch, tmp_path, tables)
    by_model = {c["model"]: c for c in r["chain"]}
    assert by_model["free-marker"]["price_basis"] == "free-by-promo"
    assert by_model["paid-marker"]["price_basis"] == "list"


# ------------------------------------------------------- 0.0 sorts ahead of priced

def test_zero_sorts_first_under_price_sort(monkeypatch, tmp_path):
    """Under the price sort, a genuinely free lane that satisfies the
    requirement takes the HEAD of the chain, ahead of any positive price."""
    tables = _with_tiers(_load_tables(), "free-marker", "paid-marker")
    tables["models"] = list(tables["models"]) + [
        _priced_marker(0.5), _free_marker()]
    r = _resolve(monkeypatch, tmp_path, tables)
    assert "error" not in r, r.get("error")
    head = r["chain"][0]
    assert (head["provider"], head["model"]) == ("prov-free", "free-marker")
    assert head["price"] == 0.0 and head["price_basis"] == "free-by-promo"


def test_zero_sorts_ahead_of_positive_and_none_never_beats_measured():
    """The sort key itself: 0.0 < any positive price, and None never sorts
    ahead of a measured value (TR-176's priced bucket)."""
    free = _free_marker()
    paid = _priced_marker(0.01)
    unknown = _unpriced_marker()
    keys = {m["provider"]: router_spawn._legacy_sort_key(m)
            for m in (free, paid, unknown)}
    assert keys["prov-free"] < keys["prov-paid"], "0.0 must beat a positive price"
    assert keys["prov-paid"] < keys["prov-unknown"], (
        "None (unknown) must never sort ahead of a measured value")


# ------------------------------------------------------- live-registry behaviour

def _declared_price(tables, provider, model):
    for m in tables["models"]:
        if m.get("provider") == provider and m.get("model") == model:
            return m.get("normalized_price")
    return None


def test_live_chain_carries_declared_prices(monkeypatch, tmp_path):
    """On the committed registry, every chain entry's price equals the lane's
    declared normalized_price — a genuine zero included, and a declared price
    is never dropped to None. Data-driven so sibling repricing (e.g.
    opencode-go/space-bunny 0.0 -> 0.168 after the branch was cut) rotates the
    sample instead of rotting a hardcoded expectation (2026-10-06 lesson).
    P1_CODING requires test>=0, which these lanes lack, so an ad-hoc
    requirement set the promos DO clear (mechanical=-3) drives the chain,
    mirroring the live reproduce command."""
    tables = _load_tables()
    monkeypatch.setattr(router_spawn, "REGISTRY", _registry(tmp_path, tables))
    monkeypatch.setattr(router_spawn, "MR", _state_dir(tmp_path))
    r = router_spawn.resolve(project="coding-hermes-scheduler",
                             adhoc=["mechanical=-3"])
    assert "error" not in r, r.get("error")
    chain = r["chain"]
    assert len(chain) > 100, f"expected the full fleet chain, got {len(chain)}"
    zero_entries = [c for c in chain if c.get("price") == 0.0]
    assert zero_entries, "expected at least one genuine zero on the chain"
    for c in zero_entries:
        assert c["price_basis"] == "free-by-promo", (
            f"{c['provider']}/{c['model']} zero must name its basis, "
            f"got {c['price_basis']}")
    dropped = []
    for c in chain:
        declared = _declared_price(tables, c["provider"], c["model"])
        if declared is not None and c["price"] is None:
            dropped.append((c["provider"], c["model"], declared))
    assert not dropped, (
        f"declared prices dropped to None on the chain: {dropped[:5]}")
