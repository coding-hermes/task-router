"""TR-070 regression suite — every piece of pricing feedback from Bane (2026-09-19)
becomes a permanent invariant. These tests read the LIVE tables (data/tables/*.jsonl,
the committed source of truth) so a hand-stamp, a reseed, or an importer run cannot
reintroduce a defect without a deliberate, evidence-carrying update.

Feedback → invariant map:
  F1  "kimi-for-coding/k3 was treating it as 0.00"  → plans are never free
  F2  "give the k3 pricing then do the plan offset" → list then offset, in the data
  F3  "xkiro free models... take away from my 5 hour limit" → free lanes carry a
      window-cost (or an explicit pending tag), never a bare 0
  F4  "models the platform thinks are cheap but are actually not" → no 99.0
      sentinels, no unreconciled offsets, audit inventory consistent
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
MODELS = os.path.join(REPO, "data", "tables", "models.jsonl")
PLANS = os.path.join(REPO, "data", "tables", "plan_terms.jsonl")


def _rows():
    return [json.loads(l) for l in open(MODELS) if l.strip()]


def _active():
    return [r for r in _rows()
            if not r.get("disabled") and not r.get("archive")
            and r.get("normalized_price") is not None]


def _lane(provider, model):
    for r in _rows():
        if r["provider"] == provider and r["model"] == model:
            return r
    raise AssertionError(f"lane missing: {provider}/{model}")


# ---------------------------------------------------------------- F1 + F2: kimi
def test_k3_is_never_reported_free():
    """F1: the plan-coverage stamp made k3 report 0.00. The display guard must
    hold AND the data must never again carry the fake-zero shape."""
    import router_spawn as rs
    row = _lane("kimi-for-coding", "k3")
    pub, _, _ = rs._pub_prices(row)
    assert pub and pub > 0, "k3 must never report a free price"
    # the data itself may not carry the trap shape (public 0 with an effective price)
    assert not (row.get("public_price") == 0 and (row.get("normalized_price") or 0) > 0)


def test_k3_uses_list_then_plan_offset():
    """F2: public = Moonshot PAYG list blended on realized mix (0.4713);
    normalized = list / measured offset. The offset must be RECORDED in
    plan_terms, not folklore."""
    row = _lane("kimi-for-coding", "k3")
    assert abs(row["public_price"] - 0.4713) < 0.001
    assert row["public_in_per_m"] == 3.0 and row["public_out_per_m"] == 15.0
    assert 0.015 < row["normalized_price"] < 0.05, (
        "effective must sit in the measured band (M between ~10x and ~30x); "
        f"got {row['normalized_price']}")
    plans = [json.loads(l) for l in open(PLANS) if l.strip()]
    kimi = [p for p in plans if p["provider"] == "kimi-for-coding"][0]
    off = kimi.get("plan_offset") or {}
    assert off.get("multiplier") == 21.4, "the measured offset must be recorded as data"
    assert off.get("basis") == "realized_usage_value"
    assert "496.20" in str(off.get("measured")), "the derivation must carry the realized numbers"


def test_kimi_effective_price_consistent_with_recorded_offset():
    """normalized must EQUAL public blend / recorded M (self-consistent data)."""
    row = _lane("kimi-for-coding", "k3")
    m = 21.4
    assert abs(row["normalized_price"] - round(0.4713 / m, 4)) < 0.001


# ------------------------------------------------- F3: free lanes draw a meter
def test_free_lanes_carry_window_cost_or_explicit_pending_tag():
    """F3: a :free lane on a plan provider is NOT free — it draws the metered
    window at list-equivalent value. Every active :free lane must either carry
    a window-cost (normalized > 0) or an explicit window-cost-pending tag."""
    violators = []
    for r in _active():
        if ":free" not in r["model"]:
            continue
        ev = str(r.get("price_evidence") or "").lower()
        # The invariant Bane asked for: a free lane must never sit at 0 without
        # a story. Priced > 0 by ANY evidence (window-cost, catalog sticker) is
        # the correct end state - it draws the metered window at that value.
        if r["normalized_price"] > 0:
            if not ev.strip():
                violators.append(f"{r['provider']}/{r['model']} (priced, no evidence)")
        elif "window-cost-pending" not in ev:
            violators.append(f"{r['provider']}/{r['model']} (free with no window story)")
    assert not violators, f"free lanes with no window story: {violators}"


def test_paid_sibling_window_cost_is_not_invented():
    """The window-cost must come from the catalog list of the paid sibling
    (nemotron-3-ultra:free = 1.5, measured from models.dev), not a guess."""
    row = _lane("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free")
    assert abs(row["normalized_price"] - 1.5) < 0.001


# ------------------------------------- F4: no sentinels, offsets self-consistent
def test_no_price_sentinels_anywhere():
    """99.0 was a sentinel that hid deepseek's cheapest PAYG. No active lane may
    carry it (or any obvious sentinel) again."""
    bad = [(r["provider"], r["model"], r["normalized_price"])
           for r in _active() if r["normalized_price"] in (99.0, 999.0, 1e6)]
    assert not bad, f"sentinel prices are live in chains: {bad}"


def test_ollama_offset_is_measured_and_consistent():
    """F4: ollama-cloud offsets came from metered usage (M=30.2x). The recorded
    kimi-k3 lane must equal list/30.2, and its old fake-cheap stamp must stay dead."""
    row = _lane("ollama-cloud", "kimi-k3")
    assert "plan-offset" in str(row.get("price_evidence"))
    assert abs(row["normalized_price"] - round(9.0 / 30.2, 5)) < 0.001
    assert abs(row["normalized_price"] - 0.067) > 0.005, "the under-priced stamp is back"


def test_xkiro_free_lane_rule_recorded_as_data():
    """The rule Bane stated must live in plan_terms, not in this chat."""
    plans = [json.loads(l) for l in open(PLANS) if l.strip()]
    xk = [p for p in plans if p["provider"] == "xkiro"]
    assert xk, "xkiro plan row missing"
    note = str(xk[0].get("note") or "")
    assert "5-hour" in note or "5h" in note, "the free-lane window rule must be recorded"


def test_successor_named_for_retiring_lane():
    """The 2026-09-25 ollama retirement must name its replacement (deepseek-v4.1-flash)."""
    import datetime
    lc = [json.loads(l) for l in open(os.path.join(REPO, "data", "lifecycle.jsonl"))
          if l.strip()]
    row = [o for o in lc
           if o["provider"] == "ollama-cloud" and o["model"] == "deepseek-v4-flash:0731"]
    assert row, "the ollama retirement overlay is missing"
    assert row[0]["valid_to"] == "2026-09-25"
    assert row[0]["replaced_by"] == "ollama-cloud/deepseek-v4.1-flash"
    assert row[0].get("lifecycle_source"), "no anonymous dates (R4)"
    # and the successor lane exists in the registry data
    _lane("ollama-cloud", "deepseek-v4.1-flash")
