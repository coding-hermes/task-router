"""TR-151/152/153/156 — contracts for the UI data layer.

These exist because a UI is where a number stops being checked: it looks
authoritative, so a fabricated 0 or a silently truncated search would be believed.
Every test here pins an honesty rule, not a formatting detail.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router_ui_data as uid  # noqa: E402


def _ledger(tmp_path, rows, torn_tail=False):
    p = tmp_path / "outcomes.jsonl"
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        if torn_tail:
            f.write('{"ts": 1790399999, "session_id": "router-proxy-torn"')  # no newline, no close
    return p


def _row(sid="router-proxy-1", ts=1000.0, outcome="served", cost=0.01, provider="p", model="m", **kw):
    d = {"ts": ts, "session_id": sid, "route_outcome": outcome, "cost_usd": cost,
         "provider": provider, "model": model, "tokens_in": 100, "tokens_out": 10,
         "steps": 1, "ladder": [{"hop": 1, "status": 200, "outcome": "ok"}],
         "complexity_source": "classifier", "wall_time_s": 1.0}
    d.update(kw)
    return d


# ---- TR-151: ledger search ------------------------------------------------- #

def test_search_filters_and_reports_its_window(tmp_path):
    p = _ledger(tmp_path, [_row(provider="a"), _row(sid="router-proxy-2", provider="b", ts=2000.0)])
    r = uid.ledger_search(provider="a", path=p)
    assert r["total_matched"] == 1 and r["rows_scanned"] == 2
    assert r["source_exists"] is True and r["truncated"] is False


def test_free_text_search_reaches_the_whole_row(tmp_path):
    p = _ledger(tmp_path, [_row(sid="router-proxy-findme"), _row(sid="router-proxy-other", ts=2000.0)])
    r = uid.ledger_search(q="findme", path=p)
    assert [x["session_id"] for x in r["rows"]] == ["router-proxy-findme"]


def test_pagination_says_when_it_is_truncated(tmp_path):
    p = _ledger(tmp_path, [_row(sid=f"router-proxy-{i}", ts=1000.0 + i) for i in range(5)])
    r = uid.ledger_search(limit=2, offset=0, path=p)
    assert r["total_matched"] == 5 and r["returned"] == 2 and r["truncated"] is True
    r2 = uid.ledger_search(limit=2, offset=4, path=p)
    assert r2["truncated"] is False


def test_a_torn_trailing_line_does_not_break_a_search(tmp_path):
    p = _ledger(tmp_path, [_row()], torn_tail=True)
    r = uid.ledger_search(path=p)
    assert r["total_matched"] == 1
    assert r["rows_malformed"] == 1, "the torn line must be counted, not hidden"


def test_missing_source_is_an_explicit_empty_answer(tmp_path):
    r = uid.ledger_search(path=tmp_path / "nope.jsonl")
    assert r["source_exists"] is False and r["total_matched"] == 0
    assert r["note"], "an empty result must explain itself"


def test_empty_result_is_not_reported_as_zero_traffic(tmp_path):
    p = _ledger(tmp_path, [_row(provider="a")])
    r = uid.ledger_search(provider="zzz", path=p)
    assert r["total_matched"] == 0 and "no rows matched" in (r["note"] or "")


# ---- TR-152: traffic + cost series ----------------------------------------- #

def test_series_buckets_and_hop_split(tmp_path):
    now = 10_000_000.0
    rows = [_row(sid="router-proxy-a", ts=now - 60),
            _row(sid="router-proxy-b", ts=now - 120, steps=3,
                 ladder=[{"hop": 1}, {"hop": 2}, {"hop": 3}]),
            _row(sid="router-proxy-old", ts=now - 200_000)]
    p = _ledger(tmp_path, rows)
    s = uid.series(hours=24, bucket="hour", path=p, now=now)
    assert s["requests_total"] == 2, "the row outside the window must not be counted"
    assert len(s["series"]) == 1
    b = s["series"][0]
    assert b["requests"] == 2 and b["one_hop"] == 1 and b["fell_back"] == 1
    assert b["samples"] == 2


def test_series_discloses_how_many_samples_were_priced(tmp_path):
    now = 10_000_000.0
    p = _ledger(tmp_path, [_row(sid="router-proxy-a", ts=now - 60, cost=0.02),
                           _row(sid="router-proxy-b", ts=now - 90, cost=None)])
    s = uid.series(hours=24, path=p, now=now)
    b = s["series"][0]
    assert b["cost_samples"] == 1 and b["samples"] == 2
    assert b["cost_usd_per_task"] == pytest.approx(0.02)


def test_unpriced_bucket_reports_null_with_a_reason_never_zero(tmp_path):
    now = 10_000_000.0
    p = _ledger(tmp_path, [_row(sid="router-proxy-a", ts=now - 60, cost=None)])
    b = uid.series(hours=24, path=p, now=now)["series"][0]
    assert b["cost_usd_total"] is None and b["cost_reason"] == "no priced samples in window"


def test_quiet_window_says_so_instead_of_charting_zeros(tmp_path):
    now = 10_000_000.0
    p = _ledger(tmp_path, [_row(sid="router-proxy-a", ts=now - 999_999)])
    s = uid.series(hours=1, path=p, now=now)
    assert s["series"] == [] and s["note"]


# ---- TR-153: per-request flow ---------------------------------------------- #

def test_flow_returns_the_whole_story(tmp_path):
    p = _ledger(tmp_path, [_row(sid="router-proxy-show", gateway_session_id="gw-1",
                               failure_reason=None, complexity_source="classifier")])
    f = uid.flow("router-proxy-show", path=p)
    assert f["session_id"] == "router-proxy-show"
    assert f["gateway_session_id"] == "gw-1"
    assert f["rating"]["source"] == "classifier"
    assert f["chain"]["steps"] == 1 and len(f["ladder"]) == 1
    assert f["cost"]["usd"] == 0.01


def test_flow_unknown_id_is_a_404_for_the_caller(tmp_path):
    p = _ledger(tmp_path, [_row()])
    f = uid.flow("router-proxy-ghost", path=p)
    assert f["status"] == 404 and f["searched_for"] == "router-proxy-ghost"
    assert "no request found" in f["error"]


def test_flow_falls_back_to_ladder_length_for_steps(tmp_path):
    p = _ledger(tmp_path, [_row(sid="router-proxy-x", steps=None,
                               ladder=[{"hop": 1}, {"hop": 2}])])
    f = uid.flow("router-proxy-x", path=p)
    assert f["chain"]["steps"] == 2


def test_flow_reports_unpriced_cost_with_a_reason(tmp_path):
    p = _ledger(tmp_path, [_row(sid="router-proxy-f", cost=None)])
    f = uid.flow("router-proxy-f", path=p)
    assert f["cost"]["usd"] is None and f["cost"]["reason"] == "not priced"


def test_hermes_session_absent_id_is_explained():
    r = uid.hermes_session("")
    assert r["found"] is None and r["reason"]


def test_hermes_session_missing_db_is_explained(tmp_path):
    r = uid.hermes_session("gw-x", db=tmp_path / "nope.db")
    assert r["found"] is None and "not present" in r["reason"]


# ---- TR-156: board browser ------------------------------------------------- #

def test_board_search_and_id_census(tmp_path):
    p = tmp_path / "tasks.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"id": "TR-1", "title": "one", "status": "pending", "priority": "P1"},
        {"id": "TR-2", "title": "two", "status": "complete", "priority": "P2"},
        {"id": "TR-2", "title": "duplicate!", "status": "pending", "priority": "P1"},
    ]) + "\n")
    r = uid.board(status="pending", path=p)
    assert r["total_matched"] == 2 and r["board_rows"] == 3
    assert r["duplicate_ids"] == {"TR-2": 2}
    assert r["by_status"]["complete"] == 1
    assert "duplicate" in r["note"]


def test_board_search_by_text(tmp_path):
    p = tmp_path / "tasks.jsonl"
    p.write_text(json.dumps({"id": "TR-9", "title": "data command center", "status": "pending"}) + "\n")
    r = uid.board(search="command center", path=p)
    assert r["total_matched"] == 1


# ---- TR-192: every honesty flag is FORCED to flip, not merely seen false ---- #

def test_ledger_forced_scan_past_max_scan_flips_scan_truncated(tmp_path, monkeypatch):
    p = _ledger(tmp_path, [_row(sid=f"router-proxy-{i}", ts=1000.0 + i) for i in range(5)])
    monkeypatch.setattr(uid, "MAX_SCAN", 2)
    r = uid.ledger_search(path=p)
    assert r["rows_scanned"] == 2 and r["scan_limit"] == 2
    assert r["total_matched"] == 2
    assert r["scan_truncated"] is True, "rows were left unread: the response must say so"
    # exactly at the cap nothing was left unread, and the flag must say so too
    monkeypatch.setattr(uid, "MAX_SCAN", 5)
    r2 = uid.ledger_search(path=p)
    assert r2["rows_scanned"] == 5 and r2["scan_truncated"] is False


def test_series_forced_unpriced_sample_flips_priced_samples_below_samples(tmp_path):
    now = 10_000_000.0
    p = _ledger(tmp_path, [_row(sid="router-proxy-a", ts=now - 60, cost=0.02),
                           _row(sid="router-proxy-b", ts=now - 90, cost=None)])
    b = uid.series(hours=24, path=p, now=now)["series"][0]
    assert b["samples"] == 2 and b["priced_samples"] == 1, \
        "a bucket must disclose how many of its samples were priced"
    assert b["cost_usd_per_task"] == pytest.approx(0.02)


def test_series_unknown_bucket_is_refused_with_the_known_set(tmp_path):
    now = 10_000_000.0
    p = _ledger(tmp_path, [_row(sid="router-proxy-a", ts=now - 60)])
    r = uid.series(bucket="fortnight", path=p, now=now)
    assert "error" in r and "fortnight" in r["error"]
    assert r["known_buckets"] == ["hour", "day"], "the refusal must say what the endpoint knows"
    assert "series" not in r and "buckets" not in r, "a refused query must chart nothing"


def test_flow_forced_without_a_gateway_session_id_names_the_missing_artefact(tmp_path):
    p = _ledger(tmp_path, [_row(sid="router-proxy-noart")])
    f = uid.flow("router-proxy-noart", path=p)
    assert f["artefacts_available"] == ["envelope", "ledger_row"]
    assert f["artefacts_missing"] == ["gateway_session"]
    assert f["hermes_session"]["found"] is None and f["hermes_session"]["reason"]


def test_flow_forced_gateway_session_in_the_store_flips_artefacts_available(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, source TEXT, model TEXT)")
    con.execute("INSERT INTO sessions VALUES ('gw-art', 'cli', 'test-model')")
    con.commit()
    con.close()
    monkeypatch.setattr(uid, "STATE_DB", db)
    p = _ledger(tmp_path, [_row(sid="router-proxy-art", gateway_session_id="gw-art")])
    f = uid.flow("router-proxy-art", path=p)
    assert f["artefacts_missing"] == []
    assert f["artefacts_available"] == ["envelope", "gateway_session", "ledger_row"]
    assert f["hermes_session"]["found"] is True


def test_board_forced_tiny_limit_flips_truncated(tmp_path):
    p = tmp_path / "tasks.jsonl"
    p.write_text("".join(json.dumps({"id": f"TR-{i}", "title": "t", "status": "pending"}) + "\n"
                         for i in range(5)))
    r = uid.board(limit=2, path=p)
    assert r["returned"] == 2 and r["total_matched"] == 5
    assert r["truncated"] is True, "a page cut must be visible as a cut"
    r2 = uid.board(limit=10, path=p)
    assert r2["returned"] == 5 and r2["truncated"] is False


def test_board_names_the_filter_it_cannot_offer(tmp_path):
    p = tmp_path / "tasks.jsonl"
    p.write_text(json.dumps({"id": "TR-1", "title": "t", "status": "pending"}) + "\n")
    r = uid.board(path=p)
    assert "owner (the board carries no owner field)" in r["filters_absent"]
    assert "status" in r["filters_available"]
    # an owner filter must not silently pretend to be a real search over real fields
    r2 = uid.board(owner="someone", path=p)
    assert r2["total_matched"] == 0
    assert any("owner" in f for f in r2["filters_absent"])
