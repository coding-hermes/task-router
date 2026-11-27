"""TR-414 — the spawn hot path must not re-parse the whole outcome ledger.

_quota_order_ctx() (scripts/router_spawn.py) builds the TR-213 headroom term — a
per-provider aggregate over the CURRENT quota window — by scanning
data/state/outcomes.jsonl. On 2026-11-25 that store was 540,471,694 bytes /
496,884 rows, so every resolve did ~497k json.loads for a number that only the
last few hundred rows can change.

AC1  bounded work proven WITHOUT wall-clock: an in-process counter shows a
     resolve over a >=200k-row synthetic store parses only the window (plus the
     settle run that proves the boundary), and the PRE-CHANGE read shape parses
     the whole file (red-proof, both arms).
AC2  byte-identical resolve JSON (head, chain order, exclusions, gate_reasons,
     quota_gates, settings) bounded vs whole-store, on >=3 representative
     projects including an alias-inherited provider.
AC3  identical headroom values on a fixture that exercises a spent window, an
     expired window, an unpriceable model and a provider with no limits
     (unknown stays NULL, never 0).
AC4  (measured out-of-band on a quiet host — see the row note; never graded on
     a loaded box.)

Hermetic: scratch ledger/limits/env only (ROUTING_OUTCOMES_FILE,
ROUTER_QUOTA_LIMITS_FILE, ROUTER_PROVIDER_QUOTA_FILE). The live fleet state is
never read or written.
"""
import json
import os
import sys
import time

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import router_outcomes as ro  # noqa: E402
import router_quota_accounting as rqa  # noqa: E402
import router_spawn  # noqa: E402

CAT = "agent_tick"
#: two same-price plan lanes, one pricier lane, one monthly-limit lane
LANES = [("prov-a", "a-lane", 1.0, 0),
         ("prov-b", "b-lane", 1.0, 0),
         ("prov-c", "c-lane", 5.0, 0)]


# ------------------------------------------------------------------ fixtures ---

def _tables():
    return {
        "providers": [{"id": p, "plan": "subscription"}
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


def _wire(monkeypatch, tmp_path, limits=None, quota_table=None, rollup=False):
    """Mini registry + scratch state/env-pinned ledger + limits config."""
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": _tables()}))
    monkeypatch.setattr(router_spawn, "REGISTRY", str(reg))
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    json.dump({"providers": {"prov-a": {"status": "open"},
                             "prov-b": {"status": "open"},
                             "prov-c": {"status": "open"}}},
              open(d / "quota-state.json", "w"))
    json.dump({"providers": {}}, open(d / "health-state.json", "w"))
    json.dump({"pairs": {}}, open(d / "circuit-state.json", "w"))
    monkeypatch.setattr(router_spawn, "MR", str(d))
    monkeypatch.setenv("ROUTING_AVERAGES_FILE", str(tmp_path / "averages.jsonl"))
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(tmp_path / "outcomes.jsonl"))
    monkeypatch.setenv("ROUTER_QUOTA_LIMITS_FILE", str(tmp_path / "quota_limits.json"))
    monkeypatch.setenv("ROUTER_QUOTA_ORDER_GUARD", "0.0001")
    # TR-415: these tests grade the BOUNDED-SCAN leg — the documented fallback
    # authority — with parse-count instruments that only exist on that leg, so
    # the persisted rollup is pinned OFF here. The rollup's own rule is graded
    # in tests/test_quota_rollup.py; the cross-leg decision identity (rollup vs
    # scan) is graded by test_ac2_rollup_leg_is_decision_identical_to_the_scan.
    monkeypatch.setenv("ROUTER_QUOTA_ROLLUP", "1" if rollup else "0")
    if limits is not None:
        with open(tmp_path / "quota_limits.json", "w") as f:
            json.dump({"default_window": "rolling_5h", "providers": limits}, f)
    pq = tmp_path / "provider_quota.jsonl"
    with open(pq, "w") as f:
        for row in (quota_table or []):
            f.write(json.dumps(row) + "\n")
    monkeypatch.setenv("ROUTER_PROVIDER_QUOTA_FILE", str(pq))
    return str(tmp_path / "outcomes.jsonl")


def _write_rows(path, rows):
    """rows: list of dicts (written as-is, in list order)."""
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _row(provider, ts, cost=None, tin=100, tout=100, model="x", **kw):
    r = {"source_system": "hermes", "session_id": f"s-{provider}", "provider": provider,
         "model": model, "ts": ts, "tokens_in": tin, "tokens_out": tout}
    if cost is not None:
        r["cost_usd"] = cost
    r.update(kw)
    return r


class _Counter:
    """In-process json.loads counter (AC1's instrument — never wall-clock)."""

    def __init__(self):
        self.n = 0
        self._real = json.loads

    def __enter__(self):
        counter = self

        def counting(*a, **kw):
            counter.n += 1
            return counter._real(*a, **kw)

        json.loads = counting
        return self

    def __exit__(self, *exc):
        json.loads = self._real
        return False


# ------------------------------------------------- ts parser: one authority ---

def test_parse_row_ts_is_the_single_authority():
    """router_quota_accounting.parse_ts must stay a delegate — the window scan
    and the accounting view cannot disagree about which rows a window owns."""
    samples = [None, True, False, 0, 1.5, float("inf"), float("nan"), "",
               "  1712345678.25 ", "not-a-ts", "2026-11-25T10:00:00Z",
               "2026-11-25T10:00:00", "2026-11-25T10:00:00+00:00", [], {}, 123]
    for v in samples:
        assert rqa.parse_ts(v) == ro.parse_row_ts(v), v


# ------------------------------------------------------ the cutoff (the rule) ---

def test_cutoff_covers_every_window_kind():
    now = 1795644000.0
    cases = {
        "rolling_5h": 5 * 3600,
        "rolling_daily": 86400,
        "daily": 86400,
        "rolling": 86400,          # window_hours = 24 below
        "weekly": None,            # calendar — asserted as a bound only
        "monthly": None,
    }
    for kind, secs in cases.items():
        cfg = {"window": kind}
        if kind == "rolling":
            cfg["window_hours"] = 24
        cut = rqa.widest_window_cutoff({"providers": {"p": cfg}}, now,
                                       margin_seconds=0)
        assert cut is not None, kind
        if secs is not None:
            assert cut == pytest.approx(now - secs), kind
        assert cut <= now, kind
    # the widest window wins: monthly start <= weekly start <= rolling_5h start
    cut_all = rqa.widest_window_cutoff(
        {"default_window": "rolling_5h",
         "providers": {"a": {"window": "rolling_5h"},
                       "b": {"window": "weekly"},
                       "c": {"window": "monthly"}}}, now, margin_seconds=0)
    assert cut_all <= rqa.widest_window_cutoff(
        {"providers": {"b": {"window": "weekly"}}}, now, margin_seconds=0)


def test_cutoff_includes_the_default_window_and_the_margin():
    now = 1795644000.0
    doc = {"default_window": "rolling_daily", "providers": {"p": {}}}
    assert rqa.widest_window_cutoff(doc, now, margin_seconds=0) == \
        pytest.approx(now - 86400)
    assert rqa.widest_window_cutoff(doc, now, margin_seconds=60) == \
        pytest.approx(now - 86400 - 60)
    # an entry with NO window falls back to default_window even when a sibling
    # declares a narrower one
    doc2 = {"default_window": "rolling_daily",
            "providers": {"narrow": {"window": "rolling_5h"}, "wide": {}}}
    assert rqa.widest_window_cutoff(doc2, now, margin_seconds=0) == \
        pytest.approx(now - 86400)


def test_cutoff_is_none_when_no_bound_can_be_derived():
    now = 1795644000.0
    assert rqa.widest_window_cutoff({"providers": {}}, now) is None
    assert rqa.widest_window_cutoff({}, now) is None
    assert rqa.widest_window_cutoff(None, now) is None
    # every window unusable -> no bound (the caller keeps the whole-store scan)
    assert rqa.widest_window_cutoff(
        {"default_window": "nonsense",
         "providers": {"p": {"window": "rolling", "window_hours": None}}},
        now) is None


# --------------------------------------------------------- the bounded scan ---

def test_bounded_scan_keeps_every_in_window_row(tmp_path):
    """Torn line, unparsable ts, future row: same treatment as the whole-store
    scan — nothing in-window is ever dropped."""
    now = 1795644000.0
    path = tmp_path / "ledger.jsonl"
    rows = [_row("prov-a", now - 100000, cost=1.0),          # out of window
            _row("prov-a", now - 7200, cost=1.0),            # in (5h)
            _row("prov-a", "2026-11-25T09:00:00Z", cost=2.0),  # ISO, in window
            _row("prov-b", now - 60, cost=3.0),              # in
            _row("prov-b", now + 60, cost=4.0),              # future: kept, dropped later
            {"provider": "prov-c", "ts": "not-a-ts"},        # unparsable ts
            _row("prov-c", now - 30, cost=5.0)]
    _write_rows(path, rows)
    with open(path, "a") as f:
        f.write('{"provider": "prov-a", "ts": 1, "cost_u')   # torn final line
    got, stats = ro.load_outcome_rows_since(path, now - 5 * 3600, settle_rows=64)
    assert stats["bounded"] is True and stats["malformed"] == 1
    kept = {json.dumps(r, sort_keys=True) for r in got}
    assert json.dumps(rows[1], sort_keys=True) in kept
    assert json.dumps(rows[3], sort_keys=True) in kept
    assert json.dumps(rows[4], sort_keys=True) in kept       # future row kept
    assert json.dumps(rows[0], sort_keys=True) not in kept   # far past dropped
    # oldest-first, i.e. the file's own order for the rows it kept
    assert [r["provider"] for r in got] == [r["provider"] for r in got]


def test_bounded_scan_handles_rows_spanning_chunk_edges(tmp_path):
    """A backwards chunk read must never split a row in half."""
    now = 1795644000.0
    path = tmp_path / "ledger.jsonl"
    rows = [_row("prov-a", now - 10, cost=0.5, session_id=f"edge-{i}" * 40)
            for i in range(200)]
    _write_rows(path, rows)
    got, stats = ro.load_outcome_rows_since(path, now - 3600, chunk_bytes=997)
    assert stats["malformed"] == 0
    assert len(got) == len(rows)
    assert [r["session_id"] for r in got] == [r["session_id"] for r in rows]


def test_bounded_scan_fails_over_when_the_cap_is_hit(tmp_path):
    """The byte cap is a fallback trigger, never a silent truncation."""
    now = 1795644000.0
    path = tmp_path / "ledger.jsonl"
    _write_rows(path, [_row("prov-a", now - i, cost=0.001) for i in range(5000)])
    rows, stats = ro.load_outcome_rows_since(path, now - 86400, max_bytes=4096)
    assert rows is None and stats["stop"] == "cap" and stats["bounded"] is False
    assert "whole-store" in (stats["error"] or "")


def test_bounded_scan_missing_store_is_an_empty_read(tmp_path):
    rows, stats = ro.load_outcome_rows_since(tmp_path / "nope.jsonl", 1.0)
    assert rows == [] and stats["stop"] == "no-store"


# ------------------------------- AC1: the counter (no wall-clock anywhere) ----

def _big_store(path, n_in=200_000, n_old=50_000, now=None):
    """>=200k in-window rows PLUS an out-of-window block at the head."""
    now = now or time.time()
    old_ts = now - 40 * 86400
    in_ts = now - 60
    with open(path, "w") as f:
        f.write("\n".join(
            '{"source_system": "hermes", "session_id": "old-%d", '
            '"provider": "prov-a", "model": "a-lane", "ts": %r, '
            '"tokens_in": 10, "tokens_out": 10, "cost_usd": 0.0001}'
            % (i, old_ts) for i in range(n_old)))
        f.write("\n")
        f.write("\n".join(
            '{"source_system": "hermes", "session_id": "new-%d", '
            '"provider": "prov-a", "model": "a-lane", "ts": %r, '
            '"tokens_in": 10, "tokens_out": 10, "cost_usd": 0.0001}'
            % (i, in_ts) for i in range(n_in)))
        f.write("\n")
    return path, n_in, n_old


def test_ac1_bounded_resolve_parses_only_the_window(monkeypatch, tmp_path):
    """AC1 (bounded arm): a resolve over a 250k-row store parses the window, not
    the store; the out-of-window block at the head is never parsed."""
    ledger = _wire(monkeypatch, tmp_path,
                   limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0}})
    _big_store(ledger, n_in=200_000, n_old=50_000)
    total_lines = 200_000 + 50_000
    with _Counter() as c:
        ctx = router_spawn._quota_order_ctx()
    assert ctx["enabled"] is True
    assert ctx["scan"]["mode"] == "bounded", ctx["scan"]
    # the window rule allows: every in-window row + the settle run that proves
    # the boundary — nothing else
    allowed = 200_000 + ctx["scan"]["settle_rows"]
    assert ctx["scan"]["rows_parsed"] <= allowed, ctx["scan"]
    assert c.n <= ctx["scan"]["rows_parsed"] + 64, c.n   # registry reads only
    # the 50k-row out-of-window block at the head was never parsed
    assert c.n < total_lines - 40_000, f"parsed {c.n} of {total_lines} rows"
    assert len(ctx["rows_by_provider"]["prov-a"]) == 200_000


def test_ac1_pre_change_shape_parses_the_whole_store(monkeypatch, tmp_path):
    """AC1 (red arm): with the bound disabled (cutoff None = the pre-change read
    shape) the SAME resolve parses every line in the file — so the counter above
    is measuring a real difference and not a fixture artifact."""
    ledger = _wire(monkeypatch, tmp_path,
                   limits={"prov-a": {"window": "rolling_5h", "limit_usd": 10.0}})
    _big_store(ledger, n_in=200_000, n_old=50_000)
    total_lines = 200_000 + 50_000
    monkeypatch.setattr(rqa, "widest_window_cutoff", lambda *a, **k: None)
    with _Counter() as c:
        ctx = router_spawn._quota_order_ctx()
    assert ctx["scan"]["mode"] == "whole-store"
    assert c.n >= total_lines, f"pre-change shape parsed only {c.n} of {total_lines}"
    assert len(ctx["rows_by_provider"]["prov-a"]) == total_lines


# ------------------------------------ AC2/AC3: identical decisions, both paths ---

def _seeded_ledger(path, now):
    """A fixture that exercises: a spent window, an expired window, an
    UNPRICEABLE model, an alias-inherited provider, a provider with no limits."""
    rows = [
        _row("prov-a", now - 120, cost=9.6),                 # spent, 5h window
        _row("prov-b", now - 120, cost=0.2),                 # plenty of headroom
        _row("prov-c", now - 60, tin=500, tout=500,
             model="unpriced-lane"),                         # no cost, no price
        _row("gw-prov-c", now - 30, cost=0.5),               # alias -> prov-c
        _row("morph", now - 3600, cost=4.0),                 # monthly window
        _row("prov-a", now - 20 * 3600, cost=5.0),           # expired 5h window
        _row("prov-a", now - 40 * 86400, cost=7.0),          # long expired
    ]
    return _write_rows(path, rows)


ALIAS_TABLE = [
    {"provider_id": "prov-c", "account": "default", "window_kind": "rolling_5h",
     "unit": "usd", "limit": 6.0},
    {"provider_id": "gw-prov-c", "account": "default", "window_kind": "none",
     "unit": "usd", "alias_of": "prov-c"},
]

AC_LIMITS = {"prov-a": {"window": "rolling_5h", "limit_usd": 10.0},
             "prov-c": {"window": "rolling_5h", "limit_usd": 6.0},
             "morph": {"window": "monthly", "limit_usd": 25.0}}


def _ctx_both(monkeypatch, tmp_path, ledger, now):
    """(bounded ctx, whole-store ctx) built over the SAME state and `now`."""
    monkeypatch.setattr(router_spawn.time, "time", lambda: now)
    bounded = router_spawn._quota_order_ctx()
    real = rqa.widest_window_cutoff
    monkeypatch.setattr(rqa, "widest_window_cutoff", lambda *a, **k: None)
    try:
        whole = router_spawn._quota_order_ctx()
    finally:
        monkeypatch.setattr(rqa, "widest_window_cutoff", real)
    return bounded, whole


def _block(ctx, prov):
    """The L2 accounting block the spawn path actually consumes, for `prov`."""
    key = str(prov)
    key = (ctx.get("alias_target") or {}).get(key, key)
    limits = ctx.get("limits") or {}
    return rqa.account_provider(
        key, (ctx.get("rows_by_provider") or {}).get(key, []),
        (limits.get("providers") or {}).get(key) or {}, ctx.get("now"),
        default_window=limits.get("default_window") or rqa.DEFAULT_WINDOW)


#: the fields a bounded read must not be able to move
DECISION_FIELDS = ("window", "window_start", "as_of", "n_rows", "tokens_in",
                   "tokens_out", "tokens_reasoning", "tokens_total",
                   "spent_usd", "unpriced_rows", "unpriced_reason",
                   "limit_usd", "remaining_usd", "remaining_reason",
                   "resets_at", "headroom_usd_per_s")


def test_ac2_ac3_same_rows_same_headroom_both_paths(monkeypatch, tmp_path):
    now = 1795644000.0
    ledger = _wire(monkeypatch, tmp_path, limits=AC_LIMITS, quota_table=ALIAS_TABLE)
    _seeded_ledger(ledger, now)
    bounded, whole = _ctx_both(monkeypatch, tmp_path, ledger, now)
    assert bounded["scan"]["mode"] == "bounded", bounded["scan"]
    assert whole["scan"]["mode"] == "whole-store"
    # AC3: every DECISION field identical, provider by provider (the bounded
    # read may legitimately drop rows that are outside EVERY window — those
    # only ever moved n_rows_outside_window, which the spawn path never reads)
    for prov in ("prov-a", "prov-b", "prov-c", "morph"):
        b, w = _block(bounded, prov), _block(whole, prov)
        assert {k: b[k] for k in DECISION_FIELDS} == \
            {k: w[k] for k in DECISION_FIELDS}, prov
        assert b["n_rows_outside_window"] <= w["n_rows_outside_window"], prov
    for prov in ("prov-a", "prov-b", "prov-c", "morph"):
        assert router_spawn._lane_headroom(bounded, prov) == \
            router_spawn._lane_headroom(whole, prov), prov
    # AC3 shape: unknown stays NULL, never 0
    assert router_spawn._lane_headroom(bounded, "prov-b") is None   # no limits cfg
    # the alias draws on its parent's window, identically on both paths
    assert router_spawn._lane_headroom(bounded, "gw-prov-c") == \
        router_spawn._lane_headroom(bounded, "prov-c")
    # the spent 5h window is genuinely tight and the monthly one is not
    h_a = router_spawn._lane_headroom(bounded, "prov-a")
    h_m = router_spawn._lane_headroom(bounded, "morph")
    assert h_a is not None and h_m is not None
    assert h_a < h_m


def test_ac2_resolve_json_identical_bounded_vs_whole_store(monkeypatch, tmp_path):
    """AC2: the full resolve document is byte-identical with the bound on and
    off — head/chain/exclusions/gate_reasons/quota_gates/settings."""
    now = 1795644000.0
    ledger = _wire(monkeypatch, tmp_path, limits=AC_LIMITS, quota_table=ALIAS_TABLE)
    _seeded_ledger(ledger, now)
    monkeypatch.setattr(router_spawn.time, "time", lambda: now)
    bounded = router_spawn.resolve(project="proj", sort="price")
    real = rqa.widest_window_cutoff
    monkeypatch.setattr(rqa, "widest_window_cutoff", lambda *a, **k: None)
    try:
        whole = router_spawn.resolve(project="proj", sort="price")
    finally:
        monkeypatch.setattr(rqa, "widest_window_cutoff", real)
    assert json.dumps(bounded, sort_keys=True) == json.dumps(whole, sort_keys=True)
    # and the term actually fired (a comparison over two inert runs proves little)
    assert bounded["quota_ordering"]["enabled"] is True
    assert [f"{e['provider']}/{e['model']}" for e in bounded["chain"]] == \
        [f"{e['provider']}/{e['model']}" for e in whole["chain"]]


def test_ac2_json_identical_with_sorts_and_profiles(monkeypatch, tmp_path):
    """AC2: not just the default sort — the bound cannot interact with the
    other ordering modes either."""
    now = 1795644000.0
    ledger = _wire(monkeypatch, tmp_path, limits=AC_LIMITS, quota_table=ALIAS_TABLE)
    _seeded_ledger(ledger, now)
    monkeypatch.setattr(router_spawn.time, "time", lambda: now)
    out = {}
    for sort in ("price", "health", "quality"):
        out[sort] = router_spawn.resolve(project="proj", sort=sort)
    real = rqa.widest_window_cutoff
    monkeypatch.setattr(rqa, "widest_window_cutoff", lambda *a, **k: None)
    try:
        for sort in ("price", "health", "quality"):
            assert json.dumps(router_spawn.resolve(project="proj", sort=sort),
                              sort_keys=True) == \
                json.dumps(out[sort], sort_keys=True), sort
    finally:
        monkeypatch.setattr(rqa, "widest_window_cutoff", real)


def test_zero_limits_configured_keeps_the_whole_store_path(monkeypatch, tmp_path):
    """Fail-open: with no configured provider the cutoff is None, the scan is
    the whole-store one, and the resolve is the historical inert ordering."""
    now = 1795644000.0
    ledger = _wire(monkeypatch, tmp_path, limits={})
    _seeded_ledger(ledger, now)
    monkeypatch.setattr(router_spawn.time, "time", lambda: now)
    ctx = router_spawn._quota_order_ctx()
    assert ctx["enabled"] is False
    # no provider entries -> the ctx never reads the ledger at all
    assert ctx["scan"]["mode"] == "inert"
    assert ctx["scan"]["cutoff_ts"] is None
    r = router_spawn.resolve(project="proj", sort="price")
    assert r["quota_ordering"]["enabled"] is False


def test_a_broken_bounded_scan_falls_open_without_disabling_ordering(
        monkeypatch, tmp_path):
    """Fail-open is sacred: an exception inside the bounded read must fall back
    to the whole-store scan, NOT turn the quota term off."""
    now = 1795644000.0
    ledger = _wire(monkeypatch, tmp_path, limits=AC_LIMITS, quota_table=ALIAS_TABLE)
    _seeded_ledger(ledger, now)
    monkeypatch.setattr(router_spawn.time, "time", lambda: now)

    def boom(*a, **kw):
        raise RuntimeError("simulated scan failure")

    monkeypatch.setattr(ro, "load_outcome_rows_since", boom)
    ctx = router_spawn._quota_order_ctx()
    assert ctx["enabled"] is True, ctx.get("reason")
    assert ctx["scan"]["mode"] == "whole-store-fallback"
    assert "simulated scan failure" in ctx["scan"]["error"]
    assert router_spawn._lane_headroom(ctx, "prov-a") is not None


# ------------------------------- TR-415: the rollup leg is not a second opinion ---

def test_ac2_rollup_leg_is_decision_identical_to_the_scan(monkeypatch, tmp_path):
    """TR-415 AC2: the persisted per-provider rollup (the DEFAULT leg) and the
    bounded-scan leg are the SAME rule over the same rows — identical headroom
    per provider (alias included) and a byte-identical resolve document. A cache
    that moved a single decision would be a routing change, not a speedup."""
    now = 1795644000.0
    monkeypatch.setattr(router_spawn.time, "time", lambda: now)
    ledger = _wire(monkeypatch, tmp_path, limits=AC_LIMITS, quota_table=ALIAS_TABLE,
                   rollup=True)
    _seeded_ledger(ledger, now)
    rollup_ctx = router_spawn._quota_order_ctx()
    assert rollup_ctx["enabled"] is True, rollup_ctx.get("reason")
    assert rollup_ctx["scan"]["mode"].startswith("rollup-"), rollup_ctx["scan"]
    assert rollup_ctx["rows_by_provider"] == {}          # totals leg carries no rows
    rollup_doc = router_spawn.resolve(project="proj", sort="price")

    monkeypatch.setenv("ROUTER_QUOTA_ROLLUP", "0")
    scan_ctx = router_spawn._quota_order_ctx()
    assert scan_ctx["scan"]["mode"] == "bounded", scan_ctx["scan"]
    scan_doc = router_spawn.resolve(project="proj", sort="price")

    for prov in ("prov-a", "prov-b", "prov-c", "morph", "gw-prov-c"):
        assert router_spawn._lane_headroom(rollup_ctx, prov) == \
            router_spawn._lane_headroom(scan_ctx, prov), prov
    # the alias draws on its parent's window on BOTH legs (TR-216 rule)
    assert router_spawn._lane_headroom(rollup_ctx, "gw-prov-c") == \
        router_spawn._lane_headroom(rollup_ctx, "prov-c")
    # AC2: the whole document — chain order, gate reasons, quota gates, settings
    assert json.dumps(rollup_doc, sort_keys=True) == \
        json.dumps(scan_doc, sort_keys=True)
    assert rollup_doc["quota_ordering"]["enabled"] is True   # the term really fired
