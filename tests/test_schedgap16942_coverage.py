"""SCHED-GAP-16942 AC2/AC3/AC4: coverage is a NUMBER on the status surfaces.

The defect: the resolver decides whether a chain may be ordered by measured
cost-per-task using a coverage fraction (router_spawn.MEASURED_MIN_COVERAGE),
but that fraction exists only inside a resolve's response — an operator asking
"how much of this chain is measured?" has to read outcomes JSONL by hand. And a
lane with no samples was indistinguishable, on /model_status, from a lane that
had been measured and failed: no n at all, so nobody could tell "unmeasured"
from "0% completion".

These tests pin the three claims the row demands:
  * every lane reports its measurement, and a null lane reports n=0 with an
    UNKNOWN rate (None) — never a 0% failure (AC2);
  * the surfaces expose coverage as a number with a named denominator (AC3);
  * below the floor the basis says price-first AND names the measured fraction;
    at or above it the basis says predicted_cost_per_task (AC4).

The coverage numbers here are computed by the RESOLVER's own machinery
(router_spawn.load_outcome_stats + lane_expected_value), never by a second
definition invented for the status page.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router_health  # noqa: E402


def _write(p: Path, rows) -> None:
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _lane(provider: str, model: str, **kw) -> dict:
    row = {"provider": provider, "model": model, "normalized_price": 1.0,
           "disabled": False, "valid_from": "2026-10-01"}
    row.update(kw)
    return row


def _sample(provider: str, model: str, n: int, completed: int, cost=None,
            **kw) -> dict:
    """One rolling-averages row: the shape router_outcomes.compute_averages writes."""
    row = {"source_system": "router-proxy", "provider": provider, "model": model,
           "complexity_sig": None, "required_categories": None,
           "n_samples": n, "n_completed": completed,
           "n_success_known": n, "n_passed": completed,
           "success_rate": (round(completed / n, 4) if n else None),
           "avg_cost_task_24h": cost}
    row.update(kw)
    return row


@pytest.fixture
def twelve_lanes(tmp_path, monkeypatch):
    """12 enabled registry lanes + a caller-supplied averages file.

    Deterministic arithmetic: coverage is measured/12, so 1 sample -> 0.0833 and
    12 -> 1.0. The live tables (2054 models) would make the number a moving
    target and the test would prove nothing about the Code that computes it.
    """
    data_dir = tmp_path / "tables"
    data_dir.mkdir()
    lanes = [_lane("prov", f"m{i}") for i in range(12)]
    _write(data_dir / "models.jsonl", lanes)
    _write(data_dir / "providers.jsonl", [{"id": "prov", "status": "OK"}])
    avgs = tmp_path / "outcomes-averages.jsonl"
    monkeypatch.setenv("ROUTING_AVERAGES_FILE", str(avgs))
    monkeypatch.delenv("ROUTER_SORT_MIN_COVERAGE", raising=False)
    return {"data_dir": str(data_dir), "avgs": avgs, "tmp": tmp_path}


def test_null_lane_reports_n_zero_and_an_unknown_rate(twelve_lanes):
    """AC2 — unmeasured is n=0 with rate None; it is never a 0% failure."""
    _write(twelve_lanes["avgs"], [
        _sample("prov", "m0", 5, 4, cost=0.5),
        # a lane the store HAS seen but with no completed/success-known sample
        _sample("prov", "m1", 2, 0, cost=None, n_success_known=0, success_rate=None),
    ])
    payload = router_health.model_status(data_dir=twelve_lanes["data_dir"])
    lanes = {ln["model"]: ln for ln in payload["lanes"]}
    assert len(lanes) == 12
    for ln in payload["lanes"]:
        assert "measurement" in ln, f"{ln['model']} exposes no measurement block"

    measured = lanes["m0"]["measurement"]
    # The basis ladder is the resolver's (blended-completed at n<blend ceiling);
    # what matters for AC2 is that a sampled lane IS measured and carries its n.
    assert measured["measured"] is True
    assert measured["basis"] in {"measured", "measured-completed",
                                 "blended", "blended-completed"}
    assert measured["n_samples"] == 5
    assert measured["success_rate"] == pytest.approx(0.8)

    # no sample at all
    absent = lanes["m7"]["measurement"]
    assert absent["n_samples"] == 0 and isinstance(absent["n_samples"], int)
    assert absent["basis"] == "no-sample"
    assert absent["success_rate"] is None, "an unmeasured lane must not read as 0%"
    assert absent["measured"] is False

    # seen, but completion unknown: still not a failure
    unknown = lanes["m1"]["measurement"]
    assert unknown["success_rate"] is None
    assert unknown["n_completed"] == 0


def test_coverage_is_a_number_below_and_above_the_floor(twelve_lanes):
    """AC3 + AC4 — the fraction is exposed and the basis names it."""
    _write(twelve_lanes["avgs"], [_sample("prov", "m0", 5, 5, cost=0.5)])
    low = router_health.measurement_coverage(data_dir=twelve_lanes["data_dir"])
    assert low["lanes"] == 12
    assert low["measured"] == 1
    assert low["coverage"] == pytest.approx(1 / 12, abs=1e-3)
    assert low["floor"] == pytest.approx(0.5)
    assert low["basis"] == "price-first"
    assert low["denominator"] == "enabled-registry-lanes"
    assert "1 of 12" in low["reason"] and "price-first" in low["reason"]

    # every lane measured -> the measured ordering is the basis, and says so
    _write(twelve_lanes["avgs"],
           [_sample("prov", f"m{i}", 5, 5, cost=0.5) for i in range(12)])
    high = router_health.measurement_coverage(data_dir=twelve_lanes["data_dir"])
    assert high["measured"] == 12
    assert high["coverage"] == pytest.approx(1.0)
    assert high["basis"] == "predicted_cost_per_task"
    assert "12 of 12" in high["reason"]


def test_disabled_lanes_leave_the_denominator(twelve_lanes):
    """AC2/AC3 — a lane that cannot serve is not 'unmeasured capacity'."""
    data_dir = Path(twelve_lanes["data_dir"])
    rows = [json.loads(l) for l in (data_dir / "models.jsonl").read_text().splitlines()]
    rows[0]["disabled"] = True
    _write(data_dir / "models.jsonl", rows)
    _write(twelve_lanes["avgs"], [_sample("prov", "m1", 5, 5, cost=0.5)])
    cov = router_health.measurement_coverage(data_dir=twelve_lanes["data_dir"])
    assert cov["lanes"] == 11
    assert cov["measured"] == 1
    assert cov["coverage"] == pytest.approx(1 / 11, abs=1e-3)


def test_model_status_carries_coverage_for_the_filtered_chain(twelve_lanes):
    """AC3 — the lookup answers 'how much of THIS provider's chain is measured'."""
    _write(twelve_lanes["avgs"], [_sample("prov", "m0", 5, 5, cost=0.5)])
    one = router_health.model_status(provider="prov", data_dir=twelve_lanes["data_dir"])
    assert one["coverage"]["lanes"] == 12
    assert one["coverage"]["measured"] == 1
    none = router_health.model_status(provider="nobody", data_dir=twelve_lanes["data_dir"])
    assert none["count"] == 0
    assert none["coverage"]["coverage"] == 0.0
    assert none["coverage"]["basis"] == "price-first"


def test_health_carries_coverage_and_survives_a_missing_store(twelve_lanes):
    """AC3 + fail-open: the store absent is a NAMED error, never a 500."""
    payload = router_health.health(mode="read-only", data_dir=twelve_lanes["data_dir"])
    assert payload["status"] == "ok"
    cov = payload["coverage"]
    assert cov["lanes"] == 12 and cov["measured"] == 0
    assert cov["basis"] == "price-first"
    assert cov["error"], "a missing averages store must be named, not silently zero"
    assert "0 of 12" in cov["reason"]


def test_coverage_honours_the_resolver_floor(monkeypatch, twelve_lanes):
    """AC4 — same knob as the resolver: ROUTER_SORT_MIN_COVERAGE."""
    _write(twelve_lanes["avgs"], [_sample("prov", f"m{i}", 5, 5, cost=0.5)
                                  for i in range(3)])
    monkeypatch.setenv("ROUTER_SORT_MIN_COVERAGE", "0.2")
    cov = router_health.measurement_coverage(data_dir=twelve_lanes["data_dir"])
    assert cov["floor"] == pytest.approx(0.2)
    assert cov["coverage"] == pytest.approx(0.25)
    assert cov["basis"] == "predicted_cost_per_task"
