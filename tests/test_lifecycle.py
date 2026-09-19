"""Model lifecycle: a retirement date must not retire a lane early.

TR-069's ground truth: `valid_to` on models.jsonl is a model's retirement
date. The pre-existing eligibility test was `valid_to is not None`, so the
FIRST future-dated retirement stamp would have hidden that lane immediately —
announced decommissions would silently shrink chains weeks before the date.
These tests pin the date semantics at the boundary (day-of counts) and prove
the helper is the single rule both models.jsonl consumers use.
"""
import datetime
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import router_spawn as rs  # noqa: E402


def test_no_valid_to_means_live():
    assert rs.row_is_retired({"valid_to": None}) is False
    assert rs.row_is_retired({}) is False


def test_future_retirement_stays_live():
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    assert rs.row_is_retired({"valid_to": tomorrow}) is False


def test_day_of_retirement_counts_as_retired():
    today = datetime.date.today().isoformat()
    assert rs.row_is_retired({"valid_to": today}) is True


def test_past_retirement_is_retired():
    last_week = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    assert rs.row_is_retired({"valid_to": last_week}) is True


def test_timestamp_forms_compare_on_the_date_only():
    """Registry rows carry plain dates, but a stamp must not flip the answer."""
    yesterday = (datetime.date.today()
                 - datetime.timedelta(days=1)).isoformat() + "T23:59:59Z"
    tomorrow = (datetime.date.today()
                + datetime.timedelta(days=1)).isoformat() + "T00:00:00Z"
    assert rs.row_is_retired({"valid_to": yesterday}) is True
    assert rs.row_is_retired({"valid_to": tomorrow}) is False


def test_explicit_today_argument_is_honoured():
    """Callers/tests can freeze the clock instead of reading the wall clock."""
    assert rs.row_is_retired({"valid_to": "2026-12-01"}, today="2026-09-19") is False
    assert rs.row_is_retired({"valid_to": "2026-12-01"}, today="2027-01-05") is True


def test_both_models_jsonl_consumers_use_the_helper():
    """The rule lives in exactly one place — no `valid_to is not None` left."""
    spawn = open(os.path.join(REPO, "scripts", "router_spawn.py")).read()
    gaps = open(os.path.join(REPO, "scripts", "router_gaps.py")).read()
    assert "valid_to') is not None" not in spawn, "presence-test retires lanes early"
    assert "row_is_retired(m)" in spawn
    assert "row_is_retired(m)" in gaps
