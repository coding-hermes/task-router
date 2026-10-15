"""TR-212 — the quota remaining panel cannot lie.

Pins the row's acceptance criteria at the data-plane level (the panel doc) and
at the page level (the render contract):

AC1  the rung travels with EVERY number; an estimate can never render as a
     reading — derived rows are observed=False with basis derived-from-ledger,
     and only a rung-2 endpoint answer sets observed=True.
AC2  NULL limits render "unknown (<reason>)" — never 0, never blank. The
     declared reason travels verbatim from the L0 table.
AC3  burn-surplus candidates carry their reset countdown (delegated verbatim
     to the TR-211 projection over the same L2 view).
AC4  the panel runs over live files and states what it scanned (bounded
     tail-scan, rows_scanned / scan_limit / scan_truncated).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_quota_panel as qp  # noqa: E402
import router_server as rs  # noqa: E402
import router_ui_page as ui  # noqa: E402

NOW = 1_800_000_000.0
RUNG4 = "rung-4: derivation from the L2 ledger"
RUNG2 = "rung-2: usage/balance endpoint"


def _quota_row(pid="p1", window="rolling_5h", unit="tokens", limit=1000,
               reason=None, **kw):
    r = {"provider_id": pid, "account": "default", "scope": "account",
         "window_kind": window, "unit": unit, "limit": limit,
         "reset_kind": "rolling" if "rolling" in window else "calendar",
         "confidence": "verified", "source": {"kind": "docs", "url": "https://x"},
         "valid_from": "2026-09-27", "archive": False}
    if reason is not None:
        r["reason"] = reason
    r.update(kw)
    return r


def _quota_table(tmp_path, rows):
    p = tmp_path / "provider_quota.jsonl"
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write('{"provider_id": "torn"')  # live-append torn tail
    return p


def _ledger(tmp_path, rows):
    p = tmp_path / "outcomes.jsonl"
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def _ledger_row(provider="p1", ts=None, cost=0.5, tin=100, tout=10, **kw):
    r = {"ts": NOW - 3600 if ts is None else ts, "provider": provider,
         "model": "m", "route_outcome": "served", "cost_usd": cost,
         "tokens_in": tin, "tokens_out": tout, "tokens_reasoning": 0,
         "session_id": "router-proxy-t", "source_system": "router-proxy"}
    r.update(kw)
    return r


def _limits_file(tmp_path, providers):
    p = tmp_path / "quota_limits.json"
    p.write_text(json.dumps({"default_window": "rolling_5h",
                             "providers": providers}))
    return p


def _panel(tmp_path, quota_rows, ledger_rows=None, limits=None, cache=None,
           now=NOW, scan_limit=None, query=None):
    qt = _quota_table(tmp_path, quota_rows)
    led = _ledger(tmp_path, ledger_rows or [])
    lim = _limits_file(tmp_path, limits or {}) if limits is not None else None
    # ALWAYS pin the readback cache — the ambient ROUTER_STATE_DIR cache (a
    # real poller run on this box) must never leak into a hermetic test
    cache_p = tmp_path / "no-cache-here.json"
    if cache is not None:
        cache_p = tmp_path / "usage-cache.json"
        cache_p.write_text(json.dumps(
            {"version": 1, "updated": "2026-10-14T00:00:00+00:00",
             "providers": cache}))
    return qp.panel_view(query or {}, ledger_path=led, now=now,
                         quota_table_path=qt, limits_path=lim,
                         readback_cache=cache_p, scan_limit=scan_limit)


# ---- AC1: the rung is on every number; estimate never renders as reading ----

def test_derived_remaining_is_estimate_only_with_its_rung(tmp_path):
    d = _panel(tmp_path, [_quota_row(limit=1000, unit="tokens")],
               [_ledger_row(tin=100, tout=10)])
    w = d["windows"][0]
    assert w["observed"] is False                       # an estimate, stamped
    assert w["source_rung"] == RUNG4
    assert w["basis"] == "derived-from-ledger"
    assert w["remaining"] == 890.0                      # limit - (100+10)


def test_rung2_endpoint_answer_is_the_only_observed_row(tmp_path):
    cache = {"deepseek": {"endpoint_id": "balance", "status": "ok",
                          "fields": {"balance": "12.5"},
                          "fetched_at": "2026-10-14T00:00:00+00:00"}}
    d = _panel(tmp_path, [_quota_row(pid="deepseek", window="none",
                                    unit="usd", limit=None,
                                    reason="no-readback")], cache=cache)
    rows = [w for w in d["windows"] if w["source_rung"] == RUNG2]
    assert len(rows) == 1
    w = rows[0]
    assert w["observed"] is True
    assert w["remaining"] == 12.5
    assert w["remaining_field"] == "balance"            # the field is named
    # the L0 row itself still renders as its own (estimated-free) NULL row:
    nulls = [x for x in d["windows"] if x["window"] == "none"]
    assert nulls[0]["remaining_reason"] == "unknown (no-readback)"


def test_rung2_field_preference_and_honest_no_field(tmp_path):
    # preference: *remaining* > total_balance > balance > credits — and an
    # endpoint with none of them says so instead of inventing a number
    cache = {"a": {"endpoint_id": "x", "status": "ok",
                   "fields": {"total_balance": "7.25", "currency": "USD"},
                   "fetched_at": "x"},
             "b": {"endpoint_id": "y", "status": "ok",
                   "fields": {"is_available": True}, "fetched_at": "x"}}
    d = _panel(tmp_path, [], cache=cache)
    rows = {w["provider"]: w for w in d["windows"]}
    assert rows["a"]["remaining"] == 7.25
    assert rows["a"]["remaining_field"] == "total_balance"
    assert rows["b"]["remaining"] is None
    assert "no numeric remaining/balance/credits" in rows["b"]["remaining_reason"]
    assert rows["b"]["observed"] is True   # the endpoint DID answer


def test_degraded_rung2_row_never_produces_a_number(tmp_path):
    cache = {"deepseek": {"endpoint_id": "balance", "status": "degraded",
                          "basis": "http-401",
                          "detail": "401 unauthorized"}}
    d = _panel(tmp_path, [], cache=cache)
    w = d["windows"][0]
    assert w["observed"] is False
    assert w["remaining"] is None
    assert "401" in w["remaining_reason"] and "rung-2 degraded" in w["remaining_reason"]


def test_every_numberless_row_still_names_its_rung(tmp_path):
    d = _panel(tmp_path, [
        _quota_row(pid="a", window="per_minute", unit="requests", limit=30),
        _quota_row(pid="b", window="weekly", unit="credits", limit=None,
                   reason="not-published"),
    ])
    for w in d["windows"]:
        assert w["source_rung"].startswith("rung-"), w
    a = next(w for w in d["windows"] if w["provider"] == "a")
    assert a["remaining"] is None and "per_minute" in a["remaining_reason"]
    # a numeric limit is still SHOWN as declared — just not turned into a
    # fake remainder
    assert a["limit"] == 30.0


# ---- AC2: NULL is "unknown (<reason>)", never 0, never blank ----------------

def test_null_limit_carries_the_declared_reason_verbatim(tmp_path):
    d = _panel(tmp_path, [_quota_row(limit=None, reason="plan-not-disclosed")])
    w = d["windows"][0]
    assert w["remaining"] is None
    assert w["remaining_reason"] == "unknown (plan-not-disclosed)"
    assert w["limit"] is None


def test_null_limit_without_declared_reason_falls_to_not_published(tmp_path):
    d = _panel(tmp_path, [_quota_row(limit=None, reason=None)])
    w = d["windows"][0]
    assert w["remaining"] is None
    assert w["remaining_reason"] == "unknown (not-published)"


def test_no_window_row_renders_zero_when_its_limit_is_unknown(tmp_path):
    d = _panel(tmp_path, [
        _quota_row(pid="a", limit=None, reason="no-readback"),
        _quota_row(pid="b", limit=None, reason="not-published"),
        _quota_row(pid="c", window="none", unit="credits", limit=None,
                   reason="plan-not-disclosed"),
    ])
    for w in d["windows"]:
        if w["limit"] is None:
            assert w["remaining"] is None
            assert w["remaining_reason"] and w["remaining_reason"].startswith("unknown (")


def test_unknown_unit_has_no_ledger_equivalent_and_stays_null(tmp_path):
    d = _panel(tmp_path, [_quota_row(unit="credits", limit=500)])
    w = d["windows"][0]
    assert w["remaining"] is None
    assert "no in-ledger equivalent" in w["remaining_reason"]
    assert w["limit"] == 500.0    # the declared limit is still shown


# ---- AC3: burn surplus with the reset countdown -----------------------------

def test_burn_candidates_carry_reset_countdown(tmp_path):
    # limit 100, spent 1 -> huge surplus, verdict burn, countdown present
    d = _panel(tmp_path, [_quota_row(unit="usd", window="rolling_5h", limit=100)],
               [_ledger_row(cost=1.0)],
               limits={"p1": {"window": "rolling_5h", "limit_usd": 100.0}})
    cands = d["burn"]["candidates"]
    assert len(cands) == 1
    c = cands[0]
    assert c["verdict"] == "burn"
    assert c["time_to_reset_s"] is not None and c["time_to_reset_s"] > 0
    assert c["resets_at"] is not None
    assert c["estimated"] is True and c["basis"] == "derived-from-ledger"
    assert d["burn"]["summary"]["burn"] == 1


def test_l0_usd_rows_fill_the_dollar_view_when_config_is_silent(tmp_path):
    d = _panel(tmp_path, [_quota_row(unit="usd", window="rolling_5h", limit=100)],
               [_ledger_row(cost=1.0)])
    # NO operator config: the L0 row's own usd limit feeds the view, named
    src = d["generated_from"]["limits_sources"]
    assert src.get("p1", "").startswith("L0 usd row")
    assert d["burn"]["candidates"][0]["limit_usd"] == 100.0


def test_operator_config_wins_over_the_l0_row(tmp_path):
    d = _panel(tmp_path, [_quota_row(unit="usd", window="rolling_5h", limit=100)],
               [_ledger_row(cost=1.0)],
               limits={"p1": {"window": "rolling_5h", "limit_usd": 40.0}})
    assert d["burn"]["candidates"][0]["limit_usd"] == 40.0
    assert "operator config" in d["generated_from"]["limits_sources"]["p1"]


def test_a_token_limit_is_never_read_as_a_dollar_limit(tmp_path):
    # regression: a 500M-tokens weekly row must not become a $500M budget
    d = _panel(tmp_path, [_quota_row(unit="tokens", window="weekly",
                                    limit=500_000_000)],
               [_ledger_row(tin=1_000)])
    assert "p1" not in (d["generated_from"]["limits_sources"] or {})
    assert d["burn"]["summary"]["burn"] == 0


def test_unknown_burn_windows_are_counted_never_treated_as_infinite(tmp_path):
    # the provider must HAVE spend for the L2 view to see it at all: no
    # spend + no limit = nothing to project (honest absence, not a 0)
    d = _panel(tmp_path, [_quota_row(unit="usd", limit=None,
                                    reason="plan-not-disclosed")],
               [_ledger_row(cost=2.0)])
    s = d["burn"]["summary"]
    assert s["unknown"] == 1 and s["burn"] == 0
    assert d["burn"]["candidates"] == []


# ---- AC4: live files, bounded scan, honesty fields --------------------------

def test_scan_is_bounded_and_reports_what_it_read(tmp_path):
    rows = [_ledger_row(ts=NOW - i * 60) for i in range(5)]
    d = _panel(tmp_path, [_quota_row(unit="tokens", limit=1000)], rows,
               scan_limit=3)
    gf = d["generated_from"]
    assert gf["rows_scanned"] == 5
    assert gf["scan_truncated"] is True
    # the derivation used only the KEPT (newest) 3 rows
    w = d["windows"][0]
    assert w["n_rows"] == 3 and w["remaining"] == 670.0


def test_missing_ledger_is_reported_not_fabricated(tmp_path):
    d = qp.panel_view({}, ledger_path=tmp_path / "nope.jsonl", now=NOW,
                      quota_table_path=_quota_table(tmp_path, [_quota_row()]),
                      limits_path=_limits_file(tmp_path, {}),
                      readback_cache=tmp_path / "no-cache.json")
    gf = d["generated_from"]
    assert gf["ledger_exists"] is False and gf["rows_scanned"] == 0
    # an empty window is a MEASURED zero, so remaining == limit — honest
    assert d["windows"][0]["remaining"] == 1000.0


def test_torn_tail_line_does_not_break_the_panel(tmp_path):
    qt = _quota_table(tmp_path, [_quota_row()])
    led = tmp_path / "outcomes.jsonl"
    led.write_text(json.dumps(_ledger_row()) + "\n" + '{"ts": 12, "prov')  # torn
    d = qp.panel_view({}, ledger_path=led, now=NOW, quota_table_path=qt,
                      limits_path=_limits_file(tmp_path, {}),
                      readback_cache=tmp_path / "no-cache.json")
    assert d["windows"][0]["n_rows"] == 1
    assert d["generated_from"]["rows_malformed"] == 1


def test_provider_filter_narrows_both_halves(tmp_path):
    cache = {"deepseek": {"endpoint_id": "balance", "status": "ok",
                          "fields": {"balance": 5}, "fetched_at": "x"}}
    d = _panel(tmp_path, [_quota_row(pid="p1"), _quota_row(pid="p2")],
               cache=cache, query={"provider": "p1"})
    assert {w["provider"] for w in d["windows"]} == {"p1"}
    # and a filter that names a readback-only provider still shows its row
    d2 = _panel(tmp_path, [], cache=cache, query={"provider": "deepseek"})
    assert [w["source_rung"] for w in d2["windows"]] == [RUNG2]


# ---- page + route contracts --------------------------------------------------

def test_the_route_is_served_by_this_service():
    assert "/api/ui/quota" in (rs.OPENAPI.get("paths") or [])


def test_the_page_renders_the_rung_and_the_unknown_form():
    page = ui.page_html()
    # the badge is decided by the observed FLAG, not by prose
    assert "w.observed === true" in page
    # NULL renders the reason verbatim, never a bare 0/blank
    assert "w.remaining_reason" in page
    assert "unknown (no reason recorded)" in page
    # the burn table carries the countdown column
    assert "countdown(e.time_to_reset_s)" in page
    # the panel is on the boot path and refreshes
    assert "quotaPanel();" in page and "setInterval(quotaPanel" in page


def test_rung1_is_named_in_the_vocabulary_but_never_emitted(tmp_path):
    d = _panel(tmp_path, [_quota_row()])
    vocab = " | ".join(d["generated_from"]["rung_vocabulary"])
    assert "rung-1" in vocab
    for w in d["windows"]:
        assert w["source_rung"] != "rung-1: response headers"
