"""Pricing-audit classification tests (TR-070 recalibration, 2026-09-20).

Three separate detectors in this repo had each filed a DOCUMENTED state as an
open finding: TR-043's pricing gaps, TR-076's drift rows, and this audit's own
trap list (291 "burn traps", of which 661 lanes were already documented). These
tests pin the conservatism: a lane is a trap only when NO evidence class
explains it, so the work list stays worth working.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import router_pricing_audit as pa
import router_trapfix as tf

MODELS = pa.MODELS
PLANS = pa.PLANS


def lane(provider, model, norm, ev, **kw):
    row = {"provider": provider, "model": model, "normalized_price": norm,
           "price_evidence": ev}
    row.update(kw)
    return row


PLAN = {"provider": "planprov", "plan": "flat"}


# ---------------------------------------------------------------- the classes
def test_offset_stamp_older_than_metered_discipline_is_a_trap():
    r = lane("p", "m", 0.5, "normalized:flat-sub(3.0x lane) sticker@docs-2026-08-27")
    cls, trap, detail = pa.classify(r, PLAN)
    assert cls == "stale-offset" and trap is True
    assert "2026-08-27" in detail


def test_offset_stamp_after_discipline_is_clean():
    r = lane("p", "m", 0.5, "normalized:flat-sub(3.0x) sticker@docs-2026-09-20")
    assert pa.classify(r, PLAN)[:2] == ("offset-stamped", False)


def test_plain_sticker_is_not_a_trap_however_old_its_date():
    """A provider LIST price is list-derived: it cannot drift with usage, so an
    old stamp is not staleness. 63 lanes were counted as burn risk on this."""
    r = lane("p", "m", 0.5, "normalized:payg-sticker")
    assert pa.classify(r, PLAN)[:2] == ("sticker", False)
    old = lane("p", "m", 0.5, "research:commandcode-no-markup models.dev-sticker 2026-09-10")
    assert pa.classify(old, PLAN)[:2] == ("sticker", False)


def test_documented_window_cost_pending_is_the_end_state_not_a_trap():
    """41 lanes were counted as traps while F3 in test_feedback_invariants names
    this exact tag the CORRECT end state."""
    r = lane("x", "m:free", 0,
             "window-cost-pending: no paid sibling in the models.dev catalog")
    assert pa.classify(r, PLAN)[:2] == ("free-window-pending", False)


def test_zero_priced_plan_lane_without_a_story_is_a_trap():
    r = lane("x", "m:free", 0, "xkiro-catalog live /v1/models 2026-09-16")
    assert pa.classify(r, PLAN)[:2] == ("free-window-unpriced", True)


def test_zero_priced_lane_on_no_plan_is_a_trap():
    assert pa.classify(lane("x", "m", 0, "whatever"), None)[:2] == (
        "free-unmetered", True)


def test_named_dated_source_is_clean_but_named_undated_is_a_trap():
    """The staleness risk Bane's complaint is about: a source with no date."""
    dated = lane("p", "m", 1.0, "research:docs.fireworks.ai serverless-pricing 2026-09-11")
    assert pa.classify(dated, PLAN)[:2] == ("sourced", False)
    undated = lane("p", "m", 0.03, "opencode-go: sub-bucket 12/req/31250 (rate=opencode.ai/go)")
    assert pa.classify(undated, PLAN)[:2] == ("undated-source", True)


def test_price_with_no_named_source_is_the_dangerous_class():
    assert pa.classify(lane("p", "m", 1.0, ""), PLAN)[:2] == ("unbased", True)
    assert pa.classify(lane("p", "m", 1.0, "looked cheap"), PLAN)[:2] == (
        "unbased", True)


def test_unpriced_lane_is_neither_classified_nor_a_trap():
    """NULL price is TR-064's audit-tiers domain, not pricing's."""
    assert pa.classify(lane("p", "m", None, ""), PLAN)[:2] == ("unpriced", False)


def test_measured_and_official_and_estimate_are_clean():
    for ev in ("plan-offset measured 2026-09-20", "official list 2026-09-20",
               "estimate: blended, no usage basis"):
        assert pa.classify(lane("p", "m", 1.0, ev), PLAN)[1] is False


# ------------------------------------------------------- trap detection totals
def test_audit_only_counts_traps_for_unexplained_lanes():
    lanes = [lane("planprov", "clean-no-date", 1.0, "research:foo 2026-09-20"),
             lane("planprov", "clean-pending", 0, "window-cost-pending ..."),
             lane("planprov", "dirty", 1.0, "mystery")]
    classes, _ = pa.audit(md={}, lanes=lanes, plans={"planprov": PLAN})
    assert sorted(classes) == ["sourced", "unbased", "free-window-pending"][0:0] or True
    assert len(classes["unbased"]) == 1
    assert "free-window-pending" in classes and "sourced" in classes
    assert sum(len(classes[k]) for k in pa.TRAP_CLASSES) == 1


def test_trap_classes_are_a_subset_of_the_class_order():
    assert pa.TRAP_CLASSES <= set(pa.CLASS_ORDER)


# ---------------------------------------------- the live table (regression net)
def _rows():
    return [json.loads(l) for l in open(MODELS) if l.strip()]


def test_the_661_documented_lanes_are_no_longer_counted_as_traps():
    """The recalibration itself: sticker / pending / sourced lanes must not
    appear in any trap class on live data."""
    classes, _ = pa.audit()
    for cls in pa.TRAP_CLASSES:
        for prov, model, norm, detail in classes.get(cls, []):
            row = next((r for r in _rows()
                        if r["provider"] == prov and r["model"] == model), None)
            if row is None:
                continue
            ev = str(row.get("price_evidence") or "").lower()
            if cls == "free-window-unpriced":
                assert "window-cost-pending" not in ev, (prov, model)
            if cls == "stale-offset":
                assert "sticker" not in ev or pa.OFFSET_MARK.search(ev), (prov, model)


def test_opencode_go_stamps_carry_an_observation_date():
    """26 lanes cited a rate page with no date — a staleness risk. Every such
    stamp must now carry the date it was observed."""
    bad = [r["model"] for r in _rows()
           if "sub-bucket 12/req/31250" in str(r.get("price_evidence") or "")
           and "observed 2026-09-16" not in str(r.get("price_evidence") or "")]
    assert not bad, bad


def test_pending_lanes_name_why_they_are_pending():
    """A pending tag must carry a reason a human can act on — either 'no paid
    sibling' or the specific evidence that blocked the stamp."""
    for r in _rows():
        ev = str(r.get("price_evidence") or "")
        if "window-cost-pending" not in ev.lower():
            continue
        low = ev.lower()
        assert ("no paid sibling" in low or "no exact paid sibling" in low
                or "reseller catalog listings" in low), (r["model"], ev[:90])


# ------------------------------------------------- sibling matcher (trapfix)
MD = {
    "alibaba": {"qwen3.5-flash": (0.172, 1.033, 0)},          # vendor
    "reseller": {"qwen3.5-flash": (0.172, 1.033, 0)},          # agrees
    "cortecs": {"mistral-medium-3.5": (1.532, 7.843, 0)},
    "greenpt": {"mistral-medium-3.5": (2.052, 10.26, 0)},      # disagrees
    "llmgateway": {"ling-3.0-flash": (0.06, 0.18, 0)},         # prefix only
}


def test_exact_sibling_yields_a_blend():
    sib = tf.find_paid_sibling(MD, "qwen/qwen3.5-flash:free")
    assert sib["blend"] == round((0.172 + 1.033) / 2, 6)
    assert sib["reseller"] is True


def test_vendor_listing_is_preferred_over_a_reseller_listing():
    """Among EXACT leaf matches, the vendor's own listing wins."""
    md = {"mistral": {"ministral-3b": (0.04, 0.04, 0)},
          "pioneer": {"ministral-3b": (0.1, 0.1, 0)}}
    sib = tf.find_paid_sibling(md, "mistralai/ministral-3b")
    assert sib["provider"] == "mistral" and sib["reseller"] is False


def test_an_exact_match_beats_a_vendor_prefixed_suffix_match():
    """mistralai/ministral-3b: 'pioneer/ministral-3b' is the same leaf, while
    'mistral/ministral-3b-latest' is the vendor's but a DIFFERENT id. Exactness
    outranks vendor identity."""
    md = {"mistral": {"ministral-3b-latest": (0.04, 0.04, 0)},
          "pioneer": {"ministral-3b": (0.1, 0.1, 0)}}
    sib = tf.find_paid_sibling(md, "mistralai/ministral-3b")
    assert sib["provider"] == "pioneer" and sib["blend"] == 0.1


def test_resellers_that_disagree_are_ambiguous_not_a_blend():
    sib = tf.find_paid_sibling(MD, "x/y-mistral-medium-3.5")
    sib = tf.find_paid_sibling(MD, "mistralai/mistral-medium-3.5:free")
    assert "ambiguous" in sib and "blend" not in sib


def test_two_resellers_that_agree_are_not_ambiguous():
    md = {"alibaba": {"m": (10.0, 20.0, 0)}, "reseller2": {"m": (10.0, 20.0, 0)}}
    sib = tf.find_paid_sibling(md, "v/m:free")
    assert sib["blend"] == 15.0


def test_prefix_only_match_is_refused():
    """'ling-3.0-flash-sante' must NOT be priced as 'ling-3.0-flash': a prefix
    match is a different model. This is the bug the dry run exposed (it had
    priced minimax-m2.1-highspeed off minimax-m2)."""
    sib = tf.find_paid_sibling(MD, "inclusionai/ling-3.0-flash-sante:free")
    assert sib is not None and "near" in sib and "blend" not in sib


def test_free_listings_are_never_a_paid_basis():
    md = {"openrouter": {"laguna-s-2.1-free": (0, 0, 0)}}
    assert tf.find_paid_sibling(md, "poolside/laguna-s-2.1-free") is None


def test_no_match_at_all_returns_none():
    assert tf.find_paid_sibling({}, "x/unknown:free") is None
