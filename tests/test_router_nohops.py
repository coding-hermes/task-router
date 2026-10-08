"""TR-195: the no-hops-rate baseline, threshold, shared UI number, dry-run alert.

Pinned here, from the row:
  * the rate is computed per hour WITH its sample size, and a baseline is
    recorded in-repo (data/nohops-baseline.json);
  * the alert payload names the top blocking category from TR-185, computed
    off the SAME ledger rows the rate came from — never hardcoded;
  * /api/ui/nohops exposes the SAME number the alerter reads (one function,
    hourly_nohops_rate — the UI and the alert cannot disagree);
  * the alert is proven by a dry-run that FORCES the threshold through the
    real knob (ROUTER_NOHOPS_THRESHOLD), not by reading the code;
  * no test touches the network.
"""
import json
import os
import sys
import time
import urllib.parse

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_nohops as rn       # noqa: E402
import router_outcomes as ro     # noqa: E402
import router_server as rsrv     # noqa: E402

NOW = time.time()  # captured once at import — every row/window in this module
# is relative to it, so nothing slides out of a live 24h window as the wall
# clock advances (the old pinned 1_791_360_000.0 expired 2026-10-08 and made
# every real-clock evaluate() path see an empty window).


def row(ts_h_ago, outcome, reqs=None, **kw):
    """One store row. outcome: served | failed | no-hops | rejected | None."""
    r = {"ts": NOW - ts_h_ago * 3600.0,
         "route_outcome": outcome,
         "provider": kw.pop("provider", "zai-glm"),
         "model": kw.pop("model", "glm-5.2"),
         "success": (True if outcome == "served"
                     else False if outcome in ("failed", "rejected") else None)}
    if reqs is not None:
        r["required_categories"] = reqs
    r.update(kw)
    return r


def write_store(tmp_path, rows, name="outcomes.jsonl"):
    p = tmp_path / name
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


@pytest.fixture()
def hermetic(monkeypatch, tmp_path):
    """No committed baseline, no webhook, no real store, no real events log."""
    monkeypatch.setattr(rn, "BASELINE_PATH", str(tmp_path / "no-baseline.json"))
    monkeypatch.setattr(rn, "EVENTS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(rn, "WEBHOOK_URL", "")
    monkeypatch.delenv("ROUTER_NOHOPS_THRESHOLD", raising=False)
    monkeypatch.delenv("ROUTER_NOHOPS_HEADROOM", raising=False)
    monkeypatch.delenv("ROUTER_NOHOPS_MIN_N", raising=False)
    return tmp_path


def store_fixture(monkeypatch, tmp_path, rows):
    p = write_store(tmp_path, rows)
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", p)
    return p


# ---------- the rate + its sample size ----------

def test_hourly_rate_carries_its_sample_size(hermetic):
    import datetime
    def bucket_iso(ts):
        b = int(ts // 3600) * 3600
        return datetime.datetime.fromtimestamp(
            b, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = ([row(0.5 + i * 0.01, "no-hops", reqs={"terminal": 2}) for i in range(3)]
            + [row(1.5 + i * 0.01, "served") for i in range(7)]   # previous hour
            + [row(5.2, "served")])                               # 5h-back hour
    s = rn.hourly_nohops_rate(rows, window_h=24.0, now_s=NOW)
    assert s["n"] == 11 and s["resolves"] == 11
    assert s["nohops"] == 3
    assert s["rate"] == round(3 / 11, 6)
    by_iso = {h["iso"]: h for h in s["hours"]}
    h1 = by_iso[bucket_iso(NOW - 1800)]
    assert h1["n"] == 3 and h1["nohops"] == 3 and h1["rate"] == 1.0
    h2 = by_iso[bucket_iso(NOW - 5400)]
    assert h2["n"] == 7 and h2["nohops"] == 0 and h2["rate"] == 0.0
    h5 = by_iso[bucket_iso(NOW - 18720)]
    assert h5["n"] == 1 and h5["nohops"] == 0 and h5["rate"] == 0.0


def test_quiet_hours_are_visible_not_gaps(hermetic):
    rows = [row(0.5, "served")]
    s = rn.hourly_nohops_rate(rows, window_h=3.0, now_s=NOW)
    assert s["hour_count"] == 4  # the 3 quiet hours are emitted with a reason
    quiet = [h for h in s["hours"] if h["n"] == 0]
    assert len(quiet) == 3
    for h in quiet:
        assert h["rate"] is None
        assert h["rate_reason"] == "no resolves in this hour"


def test_non_resolves_and_rejected_are_excluded_with_reasons(hermetic):
    rows = ([row(1.0, "served"), row(1.1, "no-hops", reqs={"terminal": 1})]
            + [row(1.2 + i * 0.01, None) for i in range(4)]      # importer rows
            + [row(1.3 + i * 0.01, "rejected") for i in range(2)]
            + [row(1.4, "no-hops", ts="not-a-number")])          # untimestamped
    s = rn.hourly_nohops_rate(rows, window_h=24.0, now_s=NOW)
    assert s["n"] == 2 and s["nohops"] == 1 and s["rate"] == 0.5
    assert s["non_resolves_excluded"] == 4
    assert s["rejected_excluded"] == 2
    assert s["untimestamped_excluded"] == 1
    assert s["rows_scanned"] == len(rows)


def test_no_resolves_reports_a_reason_not_a_zero(hermetic):
    s = rn.hourly_nohops_rate([row(1.0, None)], window_h=24.0, now_s=NOW)
    assert s["rate"] is None
    assert s["rate_reason"] == "no resolves in this window"


def test_store_bigger_than_scan_limit_yields_its_newest_rows(monkeypatch, tmp_path):
    rows = [row(400.0, "no-hops", reqs={"guard": 3})] * 5      # oldest, outside cap
    rows += [row(1.0, "served")] * 3                            # newest 3
    p = write_store(tmp_path, rows)
    got, meta = rn.load_store_rows(p, scan_limit=3)
    assert meta["rows_scanned"] == 8
    assert len(got) == 3
    assert all(g["route_outcome"] == "served" for g in got)


# ---------- TR-185 tie-in: the top blocking category ----------

def test_top_blocking_category_comes_from_the_same_rows(hermetic):
    rows = [row(1.0, "no-hops", reqs={"terminal": 2, "guard": 1}),
            row(1.1, "no-hops", reqs={"terminal": 3}),
            row(1.2, "no-hops", reqs={"guard": 2}),
            row(1.3, "served")]
    s = rn.hourly_nohops_rate(rows, window_h=24.0, now_s=NOW)
    assert s["top_blocking_category"] == "terminal"
    assert s["top_blocking_category_count"] == 2  # rows naming it, not mentions


def test_top_blocking_category_null_carries_its_reason(hermetic):
    s = rn.hourly_nohops_rate([row(1.0, "served")], window_h=24.0, now_s=NOW)
    assert s["top_blocking_category"] is None
    assert s["top_blocking_category_reason"] == "no no-hops rows in the window"
    s2 = rn.hourly_nohops_rate([row(1.0, "no-hops")], window_h=24.0, now_s=NOW)
    assert s2["top_blocking_category"] is None
    assert "required_categories" in s2["top_blocking_category_reason"]


# ---------- baseline + threshold ladder ----------

def test_threshold_default_without_baseline_or_override(hermetic):
    t, src = rn.resolve_threshold(None)
    assert t == 0.10 and src == "default (no baseline, no override)"


def test_threshold_from_baseline_rate_plus_headroom(hermetic):
    t, src = rn.resolve_threshold({"rate": 0.08})
    assert t == pytest.approx(0.13) and src == "baseline rate + headroom"


def test_baseline_file_threshold_beats_rate_headroom(hermetic):
    t, src = rn.resolve_threshold({"rate": 0.08, "threshold": 0.2})
    assert t == 0.2 and src == "baseline file threshold"


def test_env_override_wins_and_malformed_env_falls_through(monkeypatch, hermetic):
    monkeypatch.setenv("ROUTER_NOHOPS_THRESHOLD", "0.4")
    t, src = rn.resolve_threshold({"rate": 0.08, "threshold": 0.2})
    assert t == 0.4 and src == "env ROUTER_NOHOPS_THRESHOLD"
    monkeypatch.setenv("ROUTER_NOHOPS_THRESHOLD", "not-a-number")
    t2, src2 = rn.resolve_threshold({"rate": 0.08, "threshold": 0.2})
    assert t2 == 0.2 and src2 == "baseline file threshold"


def test_env_headroom_override(hermetic, monkeypatch):
    monkeypatch.setenv("ROUTER_NOHOPS_HEADROOM", "0.25")
    t, src = rn.resolve_threshold({"rate": 0.08})
    assert t == pytest.approx(0.33)


# ---------- the verdict + alert payload ----------

def verdict(rows, hermetic, **kw):
    # Pin the window to NOW: the row factory stamps ts relative to NOW, and
    # evaluate() defaults to the real clock — a live 24h window slides past
    # the pinned rows within a day (observed 2026-10-08: the oldest row fell
    # out, n=0, breach silently unassertable). The module doctrine is that
    # tests never read the clock; this helper was the one violator.
    kw.setdefault("now_s", NOW)
    return rn.evaluate(rows, baseline=None, **kw)


def test_breach_when_rate_crosses_threshold(hermetic):
    rows = [row(1.0 + i * 0.01, "no-hops", reqs={"terminal": 2})
            for i in range(12)]
    r = verdict(rows, hermetic)
    assert r["payload"]["alert"] is True
    assert r["payload"]["rate"] == 1.0
    assert r["payload"]["threshold"] == 0.10
    assert r["payload"]["top_blocking_category"] == "terminal"
    assert "terminal" in rn.alert_text(r["payload"])


def test_no_breach_below_threshold(hermetic):
    rows = [row(1.0 + i * 0.01, "served") for i in range(20)]
    r = verdict(rows, hermetic)
    assert r["payload"]["alert"] is False
    assert "rate 0.0 <= threshold" in r["payload"]["breach_reason"]


def test_small_sample_suppresses_the_alarm_with_a_reason(hermetic, monkeypatch):
    monkeypatch.setenv("ROUTER_NOHOPS_MIN_N", "10")
    rows = [row(1.0 + i * 0.01, "no-hops", reqs={"terminal": 1}) for i in range(3)]
    r = verdict(rows, hermetic)
    assert r["payload"]["alert"] is False
    assert "below ROUTER_NOHOPS_MIN_N=10" in r["payload"]["breach_reason"]
    assert r["payload"]["n"] == 3


def test_forced_threshold_through_the_real_knob_proves_the_breach(hermetic, monkeypatch):
    """The AC: force a breach via ROUTER_NOHOPS_THRESHOLD — the same env
    production tunes — never by writing a special test path."""
    rows = [row(1.0 + i * 0.01, "served") for i in range(9)]
    rows.append(row(2.0, "no-hops", reqs={"guard": 2}))
    assert verdict(rows, hermetic)["payload"]["alert"] is False  # 0.1 rate, default threshold
    monkeypatch.setenv("ROUTER_NOHOPS_THRESHOLD", "0.05")
    r = verdict(rows, hermetic)
    assert r["payload"]["alert"] is True
    assert r["payload"]["threshold_source"] == "env ROUTER_NOHOPS_THRESHOLD"
    assert r["payload"]["rate"] == 0.1


# ---------- delivery: dry run, events log, webhook ----------

def breaching_result(monkeypatch, hermetic):
    monkeypatch.setenv("ROUTER_NOHOPS_THRESHOLD", "0.05")
    rows = [row(1.0 + i * 0.01, "no-hops", reqs={"terminal": 2})
            for i in range(12)]
    return verdict(rows, hermetic)


def test_dry_run_posts_to_a_stub_and_touches_no_live_state(hermetic, monkeypatch, capsys):
    r = breaching_result(monkeypatch, hermetic)
    posted = []
    rec = rn.deliver(r, dry_run=True, post_fn=lambda u, p: posted.append((u, p)) or 200)
    payload = posted[0][1]
    assert payload["alert"] is True and payload["text"].startswith("NO-HOPS ALERT")
    assert payload["top_blocking_category"] == "terminal"
    assert posted[0][0] == ""  # no webhook configured — the stub is told so
    assert not (hermetic / "events.jsonl").exists()  # no live state in a dry run
    assert rec["delivered"] == ["stub-post (dry-run)"]
    # the CLI's built-in stub poster prints the exact payload (next test drives
    # it end to end); an injected poster receives the identical object


def test_stub_poster_prints_the_exact_payload(hermetic, monkeypatch, capsys):
    r = breaching_result(monkeypatch, hermetic)
    rn.stub_post("", r["payload"])
    out = capsys.readouterr().out
    assert "STUB POST" in out and "(no ROUTER_NOHOPS_WEBHOOK_URL configured)" in out
    doc = json.loads(out[out.index("{"):])
    assert doc["alert"] is True and doc["kind"] == "nohops-alert"


def test_real_delivery_writes_the_events_row_and_skips_the_unset_webhook(
        hermetic, monkeypatch, tmp_path):
    r = breaching_result(monkeypatch, hermetic)
    events = tmp_path / "events.jsonl"
    rec = rn.deliver(r, dry_run=False, events_path=str(events))
    rows = [json.loads(l) for l in events.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "nohops-alert" and rows[0]["alert"] is True
    assert any("events log" in d for d in rows[0]["delivered"])
    assert not any("webhook" in d for d in rows[0]["delivered"])
    assert rec["payload"]["text"].startswith("NO-HOPS ALERT")


def test_configured_webhook_receives_the_exact_payload(hermetic, monkeypatch, tmp_path):
    monkeypatch.setattr(rn, "WEBHOOK_URL", "http://127.0.0.1:1/deliver-thread")
    r = breaching_result(monkeypatch, hermetic)
    calls = []
    events = tmp_path / "events.jsonl"
    rn.deliver(r, dry_run=False, events_path=str(events),
               post_fn=lambda u, p: calls.append((u, p)) or 202)
    assert len(calls) == 1
    assert calls[0][0] == "http://127.0.0.1:1/deliver-thread"
    assert calls[0][1]["alert"] is True


def test_no_breach_delivers_nothing(hermetic):
    rows = [row(1.0 + i * 0.01, "served") for i in range(20)]
    r = verdict(rows, hermetic)
    rec = rn.deliver(r, dry_run=False, events_path=str(hermetic / "events.jsonl"))
    assert rec["delivered"] == ["nothing (no breach)"]
    assert not (hermetic / "events.jsonl").exists()


# ---------- CLI: dry-run end to end (no network) ----------

def test_cli_check_dry_run_prints_the_payload(monkeypatch, hermetic, capsys):
    store_fixture(monkeypatch, hermetic,
                  [row(1.0 + i * 0.01, "no-hops", reqs={"terminal": 1})
                   for i in range(12)])
    monkeypatch.setenv("ROUTER_NOHOPS_THRESHOLD", "0.05")
    rc = rn.main(["check", "--alert-dry-run"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "STUB POST" in out
    assert '"top_blocking_category": "terminal"' in out
    assert '"alert": true' in out
    assert not (hermetic / "events.jsonl").exists()


def test_cli_check_healthy_exit_zero(monkeypatch, hermetic, capsys):
    store_fixture(monkeypatch, hermetic,
                  [row(1.0 + i * 0.01, "served") for i in range(20)])
    rc = rn.main(["check", "--alert-dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would NOT alert" in out


def test_cli_baseline_writes_the_in_repo_record(monkeypatch, hermetic, capsys):
    store_fixture(monkeypatch, hermetic,
                  [row(1.0 + i * 0.01, "no-hops", reqs={"terminal": 2})
                   for i in range(3)]
                  + [row(1.5 + i * 0.01, "served") for i in range(17)])
    baseline = hermetic / "baseline.json"
    rc = rn.main(["baseline", "--window-h", "24", "--write", str(baseline)])
    out = capsys.readouterr().out
    assert rc == 0
    doc = json.loads(baseline.read_text())
    assert doc["kind"] == "nohops-baseline" and doc["task"] == "TR-195"
    assert doc["rate"] == pytest.approx(0.15)
    assert doc["threshold"] == pytest.approx(0.15 + rn.DEFAULT_HEADROOM)
    assert doc["resolves"] == 20 and doc["nohops"] == 3
    loaded, src = rn.load_baseline(str(baseline))
    assert loaded == doc and baseline.name in src
    assert "cannot baseline an empty window" not in out


def test_baseline_subcommand_refuses_an_empty_window(monkeypatch, hermetic, capsys):
    store_fixture(monkeypatch, hermetic, [row(1.0, None)])
    rc = rn.main(["baseline", "--window-h", "24"])
    err = capsys.readouterr().err
    assert rc == 2 and "cannot baseline an empty window" in err


# ---------- the shared number: UI route === alerter ----------

def test_the_ui_route_serves_the_alerter_same_number(monkeypatch, hermetic):
    rows = ([row(1.0 + i * 0.01, "no-hops", reqs={"terminal": 2}) for i in range(4)]
            + [row(1.5 + i * 0.01, "served") for i in range(16)]
            + [row(2.0, "rejected")])
    store_fixture(monkeypatch, hermetic, rows)
    monkeypatch.setenv("ROUTER_NOHOPS_THRESHOLD", "0.15")
    app = rsrv.RouterApplication("read-only", None)
    status, res = app.dispatch("GET", "/api/ui/nohops",
                               query={"window_h": "24"})
    assert status == 200
    expected = rn.hourly_nohops_rate(
        rn.load_store_rows(ro.outcomes_path())[0], window_h=24.0, now_s=None)
    # same function, same store -> the SAME rate, bucket for bucket
    assert res["rate"] == expected["rate"] == 0.2
    assert res["n"] == 20
    assert res["hours"] == expected["hours"]
    assert res["breached"] is True  # 0.2 > forced 0.15
    assert res["threshold"] == 0.15
    assert res["top_blocking_category"] == "terminal"
    assert res["rows_scanned"] == 21


def test_the_route_is_in_the_served_surface_and_openapi():
    assert "/api/ui/nohops" in (rsrv.OPENAPI.get("paths") or [])


def test_an_unreadable_store_is_reported_by_the_route(monkeypatch, hermetic):
    monkeypatch.setenv("ROUTING_OUTCOMES_FILE", str(hermetic / "nope.jsonl"))
    app = rsrv.RouterApplication("read-only", None)
    status, res = app.dispatch("GET", "/api/ui/nohops")
    assert status == 200
    assert "unreadable" in res["error"]
    assert res["hours"] == []


# ---------- status output ----------

def test_status_json_reports_window_and_exclusions(monkeypatch, hermetic, capsys):
    store_fixture(monkeypatch, hermetic,
                  [row(1.0, "no-hops", reqs={"guard": 3}), row(1.1, "served"),
                   row(1.2, None)])
    rc = rn.main(["status", "--json", "--window-h", "24"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["rate"] == 0.5 and doc["n"] == 2
    assert doc["non_resolves_excluded"] == 1
    assert doc["top_blocking_category"] == "guard"
