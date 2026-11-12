"""TR-401 — effort-aware resolver: pair provider + model + reasoning effort.

What is pinned here (the row's acceptance criteria, one test each):

  * a ranked effort request narrows the chain to lanes that can actually be RUN
    at it (thinking evidence), and every excluded lane is reported by code —
    never silently dropped;
  * a lane MEASURED at the requested effort beats a lane carrying only a
    default-effort proxy, and between two measured lanes the EFFORT tier
    decides — a higher DEFAULT tier can no longer win (the named defect);
  * the effort is recorded on the resolution and on every hop, and on the
    metrics (outcome) row, so attribution is per effort;
  * an absent/default effort leaves today's chain byte-identical;
  * inference: debug → high, mechanical → low, hard wins a mixed text;
  * junk input degrades fail-open to the effort-agnostic resolve.

Fast, read-only: the chain tests build their own tables (no registry read).
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
TABLES = os.path.join(REPO, "data", "tables")
sys.path.insert(0, SCRIPTS)

import router_spawn as rs  # noqa: E402


# --------------------------------------------------------------- fixtures ---
def _rows(name):
    with open(os.path.join(TABLES, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _lane(provider, model, price=1.0, thinking=True, **extra):
    row = {"provider": provider, "model": model, "normalized_price": price,
           "plan_tier": 1, "data_class": "public", "archive": False}
    if thinking is not None:
        row["thinking"] = thinking
    row.update(extra)
    return row


def _tier(model, category, tier, effort="default"):
    return {"model": model, "category": category, "tier": tier,
            "reasoning_effort": effort}


def _tables(models, tiers):
    return {"models": models, "model_tier": tiers,
            "cost_effort": [], "category_levels": _rows("category_levels.jsonl"),
            "level_defs": _rows("level_defs.jsonl")}


def _ctx(effort="high", ranked=True):
    return {"requested": effort, "ranked": ranked, "promoted": [],
            "measured_lanes": 0, "proxy_lanes": 0}


# ------------------------------------------------------------- the gate ----
def test_ranked_effort_narrows_to_lanes_that_can_take_the_parameter(capsys):
    tables = _tables(
        [_lane("p", "thinker"), _lane("p", "no-think", thinking=False),
         _lane("p", "unchecked", thinking=None)],
        [_tier("thinker", "debug", 3), _tier("no-think", "debug", 3),
         _tier("unchecked", "debug", 3)],
    )
    misses = []
    rows = rs._build_chain(tables, [("debug", 0)], effort_ctx=_ctx("high"),
                           misses_out=misses)
    assert [r[2] for r in rows] == ["thinker"], "non-thinking lanes must not route"
    codes = {m["model"]: m["codes"] for m in misses}
    assert codes["no-think"] == ["effort-unsupported"]
    assert codes["unchecked"] == ["effort-unsupported"]


def test_no_effort_request_keeps_every_lane_and_adds_no_keys():
    tables = _tables([_lane("p", "a"), _lane("p", "b", thinking=False)],
                     [_tier("a", "debug", 1), _tier("b", "debug", 1)])
    plain = rs._build_chain(tables, [("debug", 0)])
    assert [r[2] for r in plain] == ["a", "b"]
    # 'default' is effort-agnostic: identical set, and no effort stamps ride
    for _h, _p, _m, _pr, _dc, mrow in plain:
        assert "_effort_tier" not in mrow


def test_default_and_off_efforts_never_gate_on_thinking():
    tables = _tables([_lane("p", "a", thinking=False)],
                     [_tier("a", "debug", 1)])
    for level in ("default", "off"):
        rows = rs._build_chain(tables, [("debug", 0)],
                               effort_ctx=_ctx(level))
        assert len(rows) == 1, f"{level} must not require a reasoning parameter"


# ------------------------------------------------- the acceptance criterion -
def test_measured_effort_evidence_beats_a_higher_default_tier():
    """The named defect: a lane whose measured tier AT the effort is worse must
    not outrank another measured-at-effort lane because its DEFAULT tier is
    higher — and a default-only lane sorts behind both."""
    tables = _tables(
        # same price on purpose: price cannot decide, only the effort tier can
        [_lane("p", "measured-strong", price=1.0),
         _lane("p", "measured-weak-default-king", price=1.0),
         _lane("p", "proxy-only", price=1.0)],
        [
            # measured at the requested effort
            _tier("measured-strong", "debug", 4, effort="high"),
            _tier("measured-weak-default-king", "debug", 1, effort="high"),
            # their DEFAULT rows: the proxy lane's default tier is the best of
            # all three — under the old index it would have led the chain
            _tier("measured-strong", "debug", 0),
            _tier("measured-weak-default-king", "debug", 5),
            _tier("proxy-only", "debug", 5),
        ],
    )
    order = [r[2] for r in rs._build_chain(tables, [("debug", 1)],
                                           effort_ctx=_ctx("high"))]
    assert order == ["measured-strong", "measured-weak-default-king",
                     "proxy-only"], order
    # and the evidence that produced that order rides on each lane
    rows = rs._build_chain(tables, [("debug", 1)], effort_ctx=_ctx("high"))
    ev = {r[2]: r[5]["_effort_evidence"]["debug"] for r in rows}
    assert ev == {"measured-strong": "measured",
                  "measured-weak-default-king": "measured",
                  "proxy-only": "default-proxy"}


def test_proxy_lane_still_routes_but_says_it_is_a_proxy():
    """Backfill is TR-403's job: the resolver must not empty the fleet for
    lacking effort rows — it demotes and LABELS instead."""
    tables = _tables([_lane("p", "a")], [_tier("a", "debug", 3)])
    ctx = _ctx("high")
    rows = rs._build_chain(tables, [("debug", 1)], effort_ctx=ctx)
    assert len(rows) == 1
    assert rows[0][5]["_effort_evidence"]["debug"] == "default-proxy"
    assert ctx["measured_lanes"] == 0 and ctx["proxy_lanes"] == 1


def test_proxy_order_keep_is_the_auditable_rollback(monkeypatch):
    tables = _tables(
        [_lane("p", "measured-weak"), _lane("p", "proxy-strong", price=0.5)],
        [_tier("measured-weak", "debug", 1, effort="high"),
         _tier("measured-weak", "debug", 0),
         _tier("proxy-strong", "debug", 5)],
    )
    monkeypatch.setenv("ROUTER_EFFORT_PROXY_ORDER", "keep")
    order = [r[2] for r in rs._build_chain(tables, [("debug", 1)],
                                           effort_ctx=_ctx("high"))]
    assert order == ["proxy-strong", "measured-weak"]  # price order restored


# ------------------------------------------------------- effort recording ----
def test_resolve_payload_and_metrics_carry_the_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    result = {
        "project": "X", "profile": "P1", "gates_loaded": {}, "source": "s",
        "effort": {"requested": "high", "source": "flag"},
        "chain": [{"hop": 1, "provider": "p", "model": "m", "usd_1m": 1.0,
                   "tier_evidence": {"debug": "measured"}}],
        "exclusions": [],
    }
    rows = [(1, "p", "m", 1.0, "public", {"model": "m"})]
    rs._append_metrics(rows, result)
    written = [json.loads(l) for l in
               open(tmp_path / "metrics.jsonl", encoding="utf-8") if l.strip()]
    assert written, "no metrics row written"
    row = written[-1]
    assert row["reasoning_effort"] == "high"
    assert row["effort_evidence"] == {"debug": "measured"}
    assert row["config_snapshot"]["reasoning_effort"] == "high"


def test_effort_absent_metrics_row_is_null_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ROUTER_HOME", str(tmp_path))
    rs._append_metrics(
        [(1, "p", "m", 1.0, "public", {})],
        {"project": "X", "profile": "P1", "gates_loaded": {}, "source": "s",
         "chain": [{"hop": 1, "provider": "p", "model": "m", "usd_1m": 1.0}],
         "exclusions": []})
    row = [json.loads(l) for l in
           open(tmp_path / "metrics.jsonl", encoding="utf-8") if l.strip()][-1]
    assert row["reasoning_effort"] is None


# ------------------------------------------------------------ inference -----
@pytest.mark.parametrize("text,want", [
    ("Debug the flaky scheduler test", "high"),
    ("Investigate the root cause of the CI red", "high"),
    ("rename the helper and fix typos", "low"),
    ("reformat the docs", "low"),
    ("add a --json flag to the CLI", "default"),
    ("", "default"),
    (None, "default"),
])
def test_infer_effort(text, want):
    assert rs.infer_effort(text)[0] == want


def test_hard_signal_wins_a_mixed_text():
    effort, hint = rs.infer_effort("debug the rename regression")
    assert effort == "high" and hint == "debug"


@pytest.mark.parametrize("value,want_effort,want_status", [
    (None, None, "absent"),
    ("auto", rs.EFFORT_AUTO, "auto"),
    ("high", "high", "canonical"),
    ("xhigh", "x-high", "alias"),
    ("minimal", "low", "alias"),
    ("default", "default", "canonical"),
])
def test_normalize_effort_request(value, want_effort, want_status):
    effort, status, _note = rs.normalize_effort_request(value)
    assert (effort, status) == (want_effort, want_status)


def test_junk_effort_degrades_with_a_note_never_blocks():
    effort, status, note = rs.normalize_effort_request("ultracode")
    assert (effort, status) == ("default", "invalid")
    assert note and "ultracode" in note


# --------------------------------------------------------------- corpus -----
def test_corpus_is_effort_honest_today():
    """The committed corpus has no measured non-default cells yet (TR-403 is the
    backfill). Pin that so an unbackfilled claim cannot quietly pass: at 'high'
    every lane is a PROXY, none 'measured'."""
    tiers = _rows("model_tier.jsonl")
    idx, ev = rs._effort_tier_index(tiers, "high")
    assert idx, "effort index built nothing at all"
    assert not [k for k, v in ev.items() if v == "measured"]
    # the default view is unchanged by the effort work: same keys as before
    base = rs._tier_index(tiers)
    assert base and rs._tier_index(tiers, effort="default") == base
