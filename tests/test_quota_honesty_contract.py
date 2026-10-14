"""TR-214 — the quota honesty contract, pinned across every layer.

The quota plane is four layers (docs/quota-layers-spec.md): L0 limits table,
L1 header readback (TR-207/208), L2 accounting view over the ledger (TR-209),
L3 pacing gate + burn finder (TR-210/211). Each layer's behavior has its own
battery (test_quota_readback, test_quota_accounting, test_quota_pacing,
test_quota_burn, test_hop_taxonomy, test_quota_gate). What nothing pinned is
the CONTRACT that keeps quota numbers trustworthy when the layers are read
TOGETHER — spec §6 "honesty contract (enforced, not promised)". This file is
that pin, one section per board AC:

  AC1  An estimated remainder can never be serialised as observed. A number
       derived by arithmetic (percent used, utilization, limit-only family,
       unparsed value) never carries confidence 'verified'; a projection
       always carries estimated=True + basis='derived-from-ledger' and never
       an observed_at; a readback observation always carries observed_at; the
       pacing decision's remaining_usd is the L2 number VERBATIM with basis
       'observed-limit' only where the L2 block actually carried a usable
       limit — an unknown remainder serialises NO remaining at all.
  AC2  Unknown/unlimited is NULL + reason everywhere (no 0, no infinity).
       Across L2 blocks, L3 pacing decisions and burn entries: unknown is
       None with a non-empty reason; a MEASURED zero is a number with no
       reason (spent 0 on an empty window; remaining 0 on an exhausted
       window; $0 marginal cash on a declared subscription surplus) — and
       the two are never confused. No inf/nan value can reach any serialised
       quota document (json.dumps(..., allow_nan=False) stays clean).
  AC3  Every pacing refusal and every 429 we still receive is attributed to
       a named window. Window refusals name provider, account and window and
       are counted once each; operator errors are refused but claim NO window
       (they are not window facts); the hop taxonomy has no anonymous 429
       bucket — every 429 lands in '429-capacity' or '429-quota-window',
       each with a named blast radius — and a quota-window 429 round-trips
       into a quota-state gate whose reason names the provider, the body and
       the reset instant (the resolve-side exclusion naming that consumes
       this gate is pinned by test_quota_gate.py).
  AC4  A window with an unknown limit must not silently become a constraint,
       and must not silently become infinite capacity. Pacing answers
       outcome 'no_limit' (its own counter, never accepted/rejected, no
       remaining/basis keys); burn answers verdict 'unknown', burnable False,
       excluded from the surplus total; L2 leaves headroom NULL. Every
       unknown SHAPE (no config, unusable config, unpriceable spend, corrupt
       block) lands the same honest outcome.

Hermetic: pure functions over synthetic blocks/ledgers/configs under
tmp_path; fixed clock T0; no network, no live ledger, no live limit config,
no subprocess. The live machine's quota state is never read.
"""

import datetime
import json
import os
import random
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")

if REPO not in sys.path:
    sys.path.insert(0, REPO)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import router_outcomes  # noqa: E402
import router_hop_taxonomy as tax  # noqa: E402
import router_quota  # noqa: E402
import router_quota_accounting as acc  # noqa: E402
import router_quota_burn as burn  # noqa: E402
import router_quota_pacing as pacing  # noqa: E402
import router_quota_readback as rq  # noqa: E402
import router_spawn  # noqa: E402

UTC = datetime.timezone.utc
T0 = 1780000000.0  # 2026-05-29T07:46:40Z — fixed clock for all tests


# ------------------------------------------------------------------ helpers --


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def _block(
    limit=10.0,
    spent=0.0,
    resets_in=1000.0,
    provider="p1",
    account="default",
    window="rolling_5h",
):
    """A synthetic L2 block in the REAL L2 output shape (see account_provider).

    Corrupt shapes are built by mutating the returned dict — L3 consumes the
    block verbatim, so a corrupt block is a legitimate input to test.
    """
    blk = {
        "provider": provider,
        "window": window,
        "as_of": _iso(T0),
        "n_rows": 3,
        "n_rows_outside_window": 0,
        "tokens_in": 100,
        "tokens_out": 50,
        "tokens_reasoning": 0,
        "tokens_total": 150,
        "spent_usd": spent,
        "unpriced_rows": 0,
        "unpriced_reason": None,
        "limit_usd": limit,
        "remaining_usd": (None if limit is None else max(0.0, limit - spent)),
        "remaining_reason": (
            None
            if limit is not None
            else f"no limit configured for provider {provider}"
        ),
        "resets_at": None,
        "headroom_usd_per_s": None,
    }
    if account is not None:
        blk["account"] = account
    if resets_in is not None:
        blk["resets_at"] = _iso(T0 + resets_in)
    return blk


def _row(provider="p1", model="m1", ts=T0 - 60, cost=0.5, session="s", **extra):
    r = {
        "source_system": "test",
        "session_id": session,
        "provider": provider,
        "model": model,
        "tokens_in": 100,
        "tokens_out": 50,
        "tokens_reasoning": 0,
        "cost_usd": cost,
        "success": None,
        "ts": ts,
    }
    r.update(extra)
    return r


def _limits(cfg_providers):
    return {"default_window": "rolling_5h", "providers": cfg_providers}


#: recorded-response header fixtures (recorded shapes, duplicated on purpose
#: so this contract battery is independent of the sibling batteries' fixtures).
H_BOTH = {
    "x-ratelimit-limit-requests": "30",
    "x-ratelimit-remaining-requests": "27",
    "x-ratelimit-reset-requests": "2s",
}
H_PERCENT = {
    "x-codex-primary-used-percent": "37.5",
    "x-codex-primary-window-minutes": "300",
    "x-codex-primary-reset-at": "1791326400",
}
H_UTILIZATION = {"anthropic-ratelimit-unified-5h-utilization": "0.42"}
H_UNLIMITED = {
    "x-codex-credits-unlimited": "true",
    "x-codex-credits-has-credits": "true",
}
H_LIMIT_ONLY = {"x-ratelimit-limit-tokens-prompt": "21600000"}
H_UNPARSEABLE = {
    "x-ratelimit-limit-requests": "30",
    "x-ratelimit-remaining-requests": "soon",
}
H_NOTHING = {"content-type": "application/json"}

#: the derivation markers a readback observation carries in source_detail
#: when its remaining/limit was NOT read directly from the wire.
_DERIVATION_MARKERS = (
    "used_percent",
    "utilization",
    "limit_only_family",
    "remaining_unparsed",
)


def _read(headers, provider="groq"):
    obs, _next_rung = rq.read_observation(
        provider, headers, now=datetime.datetime.fromtimestamp(T0, UTC)
    )
    return obs


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No test may touch the live ledger, limits or price map."""
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "unused.jsonl"))
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE", str(tmp_path / "unused-limits.json"))
    monkeypatch.setattr(router_outcomes, "_PRICE_MAP", {})


# ==== AC1 — an estimated remainder can never be serialised as observed ======


def test_ac1_a_derived_number_never_carries_verified_confidence():
    """The serialization invariant at L1: whenever the observation's numbers
    were DERIVED (percent used, utilization fraction, limit-only family,
    unparsed value) or throttled unnamed, confidence is never 'verified'.
    Only numbers read directly off the wire may claim it."""
    derived_fixtures = (
        H_PERCENT,
        H_UTILIZATION,
        H_LIMIT_ONLY,
        H_UNPARSEABLE,
        H_NOTHING,
    )
    for headers in derived_fixtures:
        for obs in _read(headers):
            detail = obs["source_detail"]
            derived = (
                any(m in detail for m in _DERIVATION_MARKERS)
                or detail.get("mechanism") == "retry-after"
            )
            if derived:
                assert obs["confidence"] != "verified", (
                    f"derived observation claims verified: {obs}"
                )
    # the honest pole: direct header reads DO earn 'verified'
    for obs in _read(H_BOTH):
        assert obs["confidence"] == "verified"


def test_ac1_percent_derived_remaining_never_invents_a_limit():
    """100 - used is a remainder estimate: it travels with its derivation
    provenance and never fabricates the cap it was divided out of."""
    obs = _read(H_PERCENT)
    assert len(obs) == 1
    o = obs[0]
    assert o["remaining"] == 62.5
    assert o["limit"] is None  # no absolute cap observed
    assert o["unit"] == "percent"  # the unit says derived
    assert o["confidence"] == "partial"
    assert o["source_detail"]["used_percent"] == 37.5  # provenance travels


def test_ac1_utilization_never_becomes_a_remaining_count():
    obs = _read(H_UTILIZATION)
    assert len(obs) == 1
    assert obs[0]["remaining"] is None  # a fraction is not a count
    assert obs[0]["limit"] is None
    assert obs[0]["source_detail"]["utilization"] == H_UTILIZATION


def test_ac1_pacing_claims_observed_only_when_the_l2_limit_was_usable():
    """basis 'observed-limit' exists ONLY where the L2 block carried a usable
    limit; an unknown remainder serialises NO remaining and NO basis claim."""
    g = pacing.PacingGate(counters={})
    known = g.decide(_block(limit=10.0, spent=4.0), 1.0, now_ts=T0)
    assert known["basis"] == "observed-limit"
    assert known["remaining_usd"] == 6.0  # verbatim L2 number

    unknown = g.decide(_block(limit=None), 1.0, now_ts=T0)
    assert unknown["outcome"] == "no_limit"
    assert "remaining_usd" not in unknown  # no number invented
    assert "basis" not in unknown  # no observation claimed


def test_ac1_pacing_carries_the_l2_remaining_verbatim_on_wait_and_refuse():
    g = pacing.PacingGate(counters={}, wait_s=1.0, wait_cap_s=300.0)
    blk = _block(limit=10.0, spent=9.5, resets_in=300.0)  # remaining 0.5
    waited = g.decide(blk, 0.1, now_ts=T0)
    assert waited["outcome"] == "waited"
    assert waited["remaining_usd"] == blk["remaining_usd"] == 0.5
    exhausted = pacing.PacingGate(counters={}).decide(
        _block(limit=1.0, spent=1.0), 0.5, now_ts=T0
    )
    assert exhausted["outcome"] == "refused"
    assert exhausted["remaining_usd"] == 0.0  # MEASURED exhaustion


def test_ac1_every_burn_entry_is_marked_estimated_and_derived():
    """A projection from past spend is never serialised as an observation:
    estimated=True + basis='derived-from-ledger' on EVERY entry, whatever the
    verdict, and no observed_at key at all."""
    blocks = [
        _block(limit=10.0, spent=1.0, provider="p1"),
        _block(limit=None, provider="p2"),
        _block(limit=5.0, spent=9.0, provider="p3"),
    ]
    for e in burn.find_surplus(blocks, T0):
        assert e["estimated"] is True
        assert e["basis"] == "derived-from-ledger"
        assert "observed_at" not in e


def test_ac1_a_projection_never_shares_the_observation_shape():
    """The two number classes are shape-distinguishable in ANY serialised
    document: an observation always carries a tz-aware observed_at and
    source_rung 'header'; a projection never carries either, and a JSON
    round-trip preserves the distinction (nothing melts in transit)."""
    obs = _read(H_BOTH)[0]
    assert obs["source_rung"] == "header"
    stamp = datetime.datetime.fromisoformat(obs["observed_at"])
    assert stamp.utcoffset() is not None
    e = burn.find_surplus([_block(limit=10.0, spent=1.0)], T0)[0]
    assert "observed_at" not in e and "source_rung" not in e
    for doc in (obs, e):
        assert json.loads(json.dumps(doc)) == doc


# ==== AC2 — unknown/unlimited is NULL + reason everywhere (no 0, no inf) ====


def test_ac2_no_limit_is_null_with_reason_in_all_three_layers():
    """The same unknown window read through L2, L3 pacing and L3 burn: None +
    reason everywhere — never 0 remaining, never infinite capacity."""
    blk = _block(limit=None)
    l2 = acc.account_provider("p1", [], {"window": "rolling_5h", "limit_usd": None}, T0)
    assert l2["limit_usd"] is None and l2["remaining_usd"] is None
    assert l2["remaining_reason"] == "no limit configured for provider p1"

    dec = pacing.PacingGate(counters={}).decide(blk, 999.0, now_ts=T0)
    assert dec["outcome"] == "no_limit" and dec["reason"]
    assert "remaining_usd" not in dec

    e = burn.project_window(blk, T0)
    assert e["verdict"] == "unknown"
    assert e["remaining_usd"] is None and e["surplus_usd"] is None
    assert e["reason"] == "no limit configured for provider p1"


@pytest.mark.parametrize("bad", ["ten", True, float("inf"), float("nan")])
def test_ac2_unusable_limit_config_is_null_with_reason_not_zero(bad):
    """Corrupt limit values fail to NULL + a reason that names the value —
    a broken config never serialises as a usable budget."""
    blk = acc.account_provider("p1", [], {"window": "rolling_5h", "limit_usd": bad}, T0)
    assert blk["limit_usd"] is None
    assert blk["remaining_usd"] is None
    assert "unusable limit config" in blk["remaining_reason"]
    assert repr(bad) in blk["remaining_reason"]


def test_ac2_unpriced_spend_nulls_the_sum_and_the_remaining():
    """Rows whose cost cannot be priced make the SUM null (an understated
    budget would fake headroom); the reason names the unpriceable rows."""
    rows = [_row(cost=None), _row(cost=None, session="s2")]
    blk = acc.account_provider(
        "p1", rows, {"window": "rolling_5h", "limit_usd": 10.0}, T0
    )
    assert blk["spent_usd"] is None
    assert blk["remaining_usd"] is None
    assert "no declared price" in blk["remaining_reason"]
    assert blk["unpriced_rows"] == 2
    assert blk["headroom_usd_per_s"] is None  # no headroom invented
    assert blk["tokens_in"] == 200  # token sums stay exact


def test_ac2_measured_zeros_are_numbers_and_unknowns_are_null():
    """The contract cuts BOTH ways: a measured zero is a number with no
    reason; an unknown is None with a reason. The pair is never confused."""
    empty = acc.account_provider(
        "p1", [], {"window": "rolling_5h", "limit_usd": 10.0}, T0
    )
    assert empty["n_rows"] == 0
    assert empty["spent_usd"] == 0.0  # MEASURED empty window
    assert empty["remaining_usd"] == 10.0
    assert empty["remaining_reason"] is None

    exhausted = pacing.PacingGate(counters={}).decide(
        _block(limit=1.0, spent=1.0), 0.5, now_ts=T0
    )
    assert exhausted["remaining_usd"] == 0.0  # MEASURED exhaustion

    unknown = _block(limit=None)
    assert unknown["remaining_usd"] is None
    assert unknown["remaining_reason"]


def test_ac2_burn_cash_zero_is_named_and_unknown_cash_is_not_zero():
    """$0 marginal cash is legitimate ONLY when the subscription basis is
    declared (and says so); an undeclared basis stays NULL + reason — never
    quietly serialised as free money."""
    blocks = [_block(limit=10.0, spent=1.0, resets_in=100.0)]
    cfgs = {"p1": {"window": "rolling_5h", "plan_kind": "subscription"}}
    e = burn.find_surplus(blocks, T0, provider_cfgs=cfgs)[0]
    assert e["verdict"] == "burn"
    assert e["cost_basis"] == "subscription-covered"
    assert e["subscription_covered"] is True
    assert e["marginal_cash_usd"] == 0.0  # the named, earned zero
    assert e["marginal_cash_reason"] is None

    e2 = burn.find_surplus(blocks, T0)[0]  # same window, no plan_kind
    assert e2["verdict"] == "burn"
    assert e2["marginal_cash_usd"] is None
    assert "not $0" in e2["marginal_cash_reason"]


def test_ac2_no_infinity_or_nan_reaches_serialised_quota_documents():
    """inf/nan limits are refused at the L2 boundary, so every document the
    quota plane serialises is standards-clean JSON (allow_nan=False holds —
    no Infinity/NaN token can leak into a consumer)."""
    blocks = [
        acc.account_provider(
            "p1", [_row()], {"window": "rolling_5h", "limit_usd": float("inf")}, T0
        ),
        acc.account_provider(
            "p2",
            [_row(provider="p2")],
            {"window": "rolling_5h", "limit_usd": float("nan")},
            T0,
        ),
        _block(limit=None, provider="p3"),
    ]
    decisions = [
        pacing.PacingGate(counters={}).decide(b, 1.0, now_ts=T0) for b in blocks
    ]
    entries = burn.find_surplus(blocks, T0)
    for b in blocks:
        assert b["limit_usd"] is None and b["remaining_usd"] is None
    docs = blocks + decisions + entries
    s = json.dumps(docs, allow_nan=False)
    assert "Infinity" not in s and "NaN" not in s


# ==== AC3 — every refusal and every 429 is attributed to a named window =====


def test_ac3_every_window_refusal_names_provider_account_and_window():
    """Property over randomized windows: any refusal that is a WINDOW verdict
    (not an operator error) carries provider/account/window matching the
    block and a reason that embeds the canonical window name."""
    rnd = random.Random(20261013)
    g = pacing.PacingGate(counters={}, margin=0.9)
    refused = 0
    for _ in range(150):
        limit = rnd.choice([0.5, 1.0, 10.0])
        blk = _block(
            limit=limit,
            spent=rnd.uniform(0, limit),
            provider="prov-%d" % rnd.randint(1, 3),
            account="acct-%d" % rnd.randint(1, 2),
            window=rnd.choice(["rolling_5h", "weekly"]),
            resets_in=rnd.choice([None, 100.0, 5000.0]),
        )
        dec = g.decide(blk, rnd.uniform(0.0, limit), now_ts=T0)
        if dec["outcome"] != "refused" or dec.get("operator_error"):
            continue
        refused += 1
        assert dec["provider"] == blk["provider"]
        assert dec["account"] == blk["account"]
        assert dec["window"] == blk["window"]
        assert pacing.window_named(blk) in dec["reason"]
    assert refused > 50, "fuzz produced too few refusals to pin the contract"


def test_ac3_window_refusals_are_counted_exactly_once():
    """The refusal counter moves once per window refusal — the 'why did we
    refuse' census stays equal to the refusals themselves."""
    counters = {}
    g = pacing.PacingGate(counters=counters)
    g.decide(_block(limit=1.0, spent=1.0, provider="a"), 0.5, now_ts=T0)
    g.decide(_block(limit=1.0, spent=1.0, provider="b"), 0.5, now_ts=T0)
    g.decide(_block(limit=10.0, spent=0.0, provider="c"), 0.5, now_ts=T0)
    g.decide(_block(limit=None, provider="d"), 0.5, now_ts=T0)
    assert counters["rejected"] == 2
    assert counters["accepted"] == 1
    assert counters["no_limit_contribution"] == 1


def test_ac3_cross_account_dead_end_names_the_preferred_window():
    blk, dec = pacing.PacingGate(counters={}).pick_account(
        "p1",
        {
            "default": _block(limit=10.0, spent=10.0, account="default"),
            "alt": _block(limit=10.0, spent=10.0, account="alt"),
        },
        est_cost=0.5,
        now_ts=T0,
    )
    assert blk is None and dec["outcome"] == "refused"
    assert dec["account"] == "default"
    assert pacing.window_named(_block(account="default")) in dec["reason"]


def test_ac3_operator_errors_are_refused_but_claim_no_window():
    """A non-finite est_cost is a programming/operator fact, not a window
    fact: refused, uncounted, and attributing NO window."""
    counters = {}
    g = pacing.PacingGate(counters=counters)
    for bad in (float("nan"), float("inf"), -1.0):
        dec = g.decide(
            _block(limit=1.0, spent=1.0, provider="p", account="a", window="weekly"),
            bad,
            now_ts=T0,
        )
        assert dec["outcome"] == "refused"
        assert dec["operator_error"] is True
        assert "provider" not in dec and "window" not in dec
        assert "account" not in dec
    assert counters == {}  # not a single window counter moved


def test_ac3_the_taxonomy_has_no_anonymous_429_bucket():
    """Every 429 the ladder still receives lands in one of TWO named window
    classes — never an unnamed 4xx bucket — and each class carries a named
    blast radius and retry policy."""
    codes_429 = [c for c in tax.HOP_FAILURE_CODES if c.startswith("429")]
    assert sorted(codes_429) == ["429-capacity", "429-quota-window"]
    for code in codes_429:
        assert tax.BLAST_RADIUS.get(code) in ("model", "provider")
        assert code in tax.RETRY_POLICY
    # the status fallback for a body-less 429 is still a NAMED class
    assert tax.code_from_status(429) in codes_429


@pytest.mark.parametrize(
    "body",
    [
        "Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-09-20 04:07:32",
        "usage limit has been reached",
        "access_terminated",
        "Account budget exceeded",
        "code=1310: weekly cap reached",
        "quota window: business code 1310, weekly limit",
        "rate limit exceeded, cap: 40, retry after 26s",
        "",  # a silent body STILL lands in a named class
    ],
)
def test_ac3_every_realistic_429_body_lands_in_a_named_window_class(body):
    code = tax.code_from_status(429, body)
    assert code in ("429-capacity", "429-quota-window")
    assert tax.BLAST_RADIUS[code] in ("model", "provider")


def test_ac3_a_quota_window_429_round_trips_into_a_named_window_gate(tmp_path):
    """The attribution chain for the 429s that still happen: taxonomy code ->
    quota gate entry whose reason names the provider, the 429 body and the
    reset instant — the named window every later reader can answer from."""
    body = "quota window: business code 1310, weekly limit"
    assert tax.code_from_status(429, body) == "429-quota-window"

    state = tmp_path / "st"
    state.mkdir()
    rc = router_quota.quota_set(
        "zai-glm", body, _iso(T0 + 6 * 3600), state_dir_arg=str(state)
    )
    assert rc == 0
    doc = json.load(open(state / "quota-state.json"))
    entry = doc["quota_exhausted"]["zai-glm"]
    assert entry["status"] == "gated"
    assert entry["reason"] == body
    reset_at = entry["reset_at"]  # captured once, reused (TR-095)

    gates = router_spawn.load_quota_gates(doc, now=_iso(T0))
    assert gates["zai-glm"]["active"] is True
    # the named window = the provider key + the 429 reason + the reset instant
    assert list(gates) == ["zai-glm"]
    reason = gates["zai-glm"]["gate_reason"]
    assert body in reason and reset_at in reason
    assert reason.startswith("quota exhausted:")


# ==== AC4 — unknown limit: never a constraint, never infinite capacity ======


def test_ac4_unknown_limit_is_never_a_pacing_constraint():
    """Any spend passes — the gate never converts 'unknown' into refused or
    delayed; it answers the dedicated outcome instead."""
    g = pacing.PacingGate(counters={})
    for est in (0.01, 5.0, 999.0):
        dec = g.decide(_block(limit=None), est, now_ts=T0)
        assert dec["outcome"] == "no_limit"
        assert dec["outcome"] not in ("refused", "waited")


def test_ac4_unknown_limit_never_claims_capacity():
    """The other pole: 'no constraint' is not 'infinite capacity' — the
    outcome is never 'admitted' (which would carry a remaining and an
    observed basis), it counts in its OWN counter, and it says why."""
    counters = {}
    g = pacing.PacingGate(counters=counters)
    dec = g.decide(_block(limit=None), 1.0, now_ts=T0)
    assert dec["outcome"] == "no_limit"  # never 'admitted'
    assert dec["no_limit_contribution"] is True
    assert dec["reason"]
    assert "remaining_usd" not in dec and "basis" not in dec
    assert counters["no_limit_contribution"] == 1
    assert counters.get("accepted", 0) == 0
    assert counters.get("rejected", 0) == 0


def test_ac4_a_negative_remainder_is_unknown_not_capacity():
    """remaining_usd < 0 is unusable as headroom: pacing treats it as an
    unknown contribution (recorded), never as admissible capacity."""
    blk = _block(limit=1.0, spent=0.0)
    blk["remaining_usd"] = -0.5
    dec = pacing.PacingGate(counters={}).decide(blk, 1.0, now_ts=T0)
    assert dec["outcome"] == "no_limit"
    assert dec["reason"]


def test_ac4_unknown_limit_burn_is_never_burnable_and_never_in_the_total():
    blocks = [
        _block(limit=None, provider="unknown-p"),
        _block(limit=10.0, spent=9.99, resets_in=3600.0, provider="real-p"),
    ]
    entries = burn.find_surplus(blocks, T0)
    by_p = {e["provider"]: e for e in entries}
    u = by_p["unknown-p"]
    assert u["verdict"] == "unknown"
    assert u["burnable"] is False
    assert u["surplus_usd"] is None
    s = burn.summarize(entries)
    assert s["unknown"] == 1
    assert s["burn"] == 0
    assert s["total_surplus_usd"] == 0.0  # empty sum: no unknown leaked in


def test_ac4_unknown_limit_leaves_l2_headroom_null():
    """No limit -> no remaining -> no headroom rate, even when resets_at is
    known: the projection input stays honestly NULL."""
    blk = acc.account_provider(
        "p1", [_row()], {"window": "rolling_5h", "limit_usd": None}, T0
    )
    assert blk["resets_at"] is not None
    assert blk["remaining_usd"] is None
    assert blk["headroom_usd_per_s"] is None


@pytest.mark.parametrize(
    "mutate,frag",
    [
        (
            lambda b: b.update(
                {
                    "limit_usd": None,
                    "remaining_usd": None,
                    "remaining_reason": "no limit configured",
                }
            ),
            None,
        ),
        (
            lambda b: b.update(
                {
                    "limit_usd": "ten",
                    "remaining_usd": None,
                    "remaining_reason": "unusable limit config",
                }
            ),
            None,
        ),
        (
            lambda b: b.update(
                {"remaining_usd": None, "remaining_reason": "spent could not be priced"}
            ),
            "could not be priced",
        ),
        (
            lambda b: b.update(
                {"remaining_usd": float("nan"), "remaining_reason": None}
            ),
            None,
        ),
    ],
)
def test_ac4_every_unknown_shape_lands_the_same_honest_outcome(mutate, frag):
    """No config, unusable config, unpriceable spend, corrupt value — every
    shape of unknown lands 'no_limit' (pacing) and 'unknown' (burn), with a
    non-empty reason and nothing invented in between."""
    blk = _block(limit=10.0, spent=2.0)
    mutate(blk)
    dec = pacing.PacingGate(counters={}).decide(blk, 1.0, now_ts=T0)
    assert dec["outcome"] == "no_limit"
    assert dec["no_limit_contribution"] is True
    assert dec["reason"]
    if frag:
        assert frag in dec["reason"]
    e = burn.project_window(blk, T0)
    assert e["verdict"] == "unknown"
    assert e["burnable"] is False
    assert e["surplus_usd"] is None
    assert e["reason"]
