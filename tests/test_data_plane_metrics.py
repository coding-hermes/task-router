"""TR-374 — data-plane freshness metrics (docs/observability-spec.md §3.5).

The row exists because /health already computed coverage, deploy-parity and
registry age, and NOTHING consumed them: a live instance served 119 commits
behind its repo with `stale: true` on an endpoint no alert read. These tests
pin the three properties that make the export trustworthy:

1. the numbers come from the SAME functions /health calls (parity, not a
   second derivation) — `router_health.health()` and the collectors must agree;
2. each family renders under its row name AND its `task_router_*` alias, so
   the spec §6 expressions resolve;
3. an uncomputable value yields NO sample instead of a fabricated 0 — a 0 on
   `router_code_stale` would read "deployed and fresh", the exact lie TR-141
   exists to prevent.

Plus the alert-rule contract: TaskRouterCodeStale (§6 table) and
TaskRouterCoverageBelowFloor (row AC) ship as repository-owned rules.
"""
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import router_health  # noqa: E402
import router_prometheus  # noqa: E402

ALERTS = REPO / "monitoring" / "task-router-alerts.yml"
FAMILIES = (
    "router_coverage_measured_ratio",
    "router_code_stale",
    "router_registry_age_days",
    "router_probe_providers_up",
)


def _doc() -> str:
    return router_prometheus.REGISTRY.render().decode("utf-8")


def _series(doc: str, name: str) -> list:
    """Sample lines of one family (never its HELP/TYPE comment lines)."""
    out = []
    for line in doc.splitlines():
        if line.startswith("#") or not line:
            continue
        metric = line.split("{", 1)[0].split(" ", 1)[0]
        if metric == name:
            out.append(line)
    return out


def test_families_render_under_row_name_and_task_router_alias():
    doc = _doc()
    for fam in FAMILIES:
        assert f"# TYPE {fam} gauge" in doc, f"{fam} missing from the exposition"
        alias = "task_router_" + fam[len("router_"):]
        assert f"# TYPE {alias} gauge" in doc, f"{alias} alias missing"


def test_collectors_read_the_same_sources_health_reports():
    """Parity arm: /health and /metrics cannot disagree about one number."""
    health = router_health.health()
    cov = router_health.measurement_coverage()
    assert health["registry"] == router_health.registry_data_block()
    assert health["probe"] == router_health.probe_summary()
    if cov.get("coverage") is not None:
        vals = [v for (_labels, v) in router_prometheus._collect_coverage_measured_ratio()]
        assert vals == [float(cov["coverage"])]
    identity = router_health.code_identity()
    vals = [v for (_labels, v) in router_prometheus._collect_code_stale()]
    if identity.get("stale") is None:
        assert vals == []
    else:
        assert vals == [1 if identity["stale"] else 0]


def test_uncomputable_value_yields_no_sample_never_zero(monkeypatch):
    """The absence policy: unknown is absent, not a healthy-looking 0."""
    monkeypatch.setattr(router_health, "code_identity",
                        lambda: {"stale": None, "error": "loaded commit unknown"})
    assert router_prometheus._collect_code_stale() == []
    doc = _doc()
    assert _series(doc, "router_code_stale") == []
    assert _series(doc, "task_router_code_stale") == []

    monkeypatch.setattr(router_health, "registry_data_block",
                        lambda *a, **k: {"age_days": None, "models": 0})
    assert router_prometheus._collect_registry_age_days() == []

    monkeypatch.setattr(router_health, "probe_summary",
                        lambda *a, **k: {"error": "health-state.json missing"})
    assert router_prometheus._collect_probe_providers_up() == []


def test_code_stale_renders_one_for_a_behind_process(monkeypatch):
    monkeypatch.setattr(router_health, "code_identity",
                        lambda: {"stale": True, "loaded_commit": "aaa",
                                 "repo_commit": "bbb"})
    assert [v for (_l, v) in router_prometheus._collect_code_stale()] == [1]
    monkeypatch.setattr(router_health, "code_identity", lambda: {"stale": False})
    assert [v for (_l, v) in router_prometheus._collect_code_stale()] == [0]


def test_probe_gauge_carries_the_status_label(monkeypatch):
    monkeypatch.setattr(router_health, "probe_summary",
                        lambda *a, **k: {"provider_status_counts":
                                         {"OK": 21, "DOWN": 4, "DISABLED": 3}})
    rows = {(l or {}).get("status"): v
            for (l, v) in router_prometheus._collect_probe_providers_up()}
    assert rows == {"OK": 21, "DOWN": 4, "DISABLED": 3}
    doc = _doc()
    assert 'router_probe_providers_up{status="DOWN"} 4' in doc
    assert 'task_router_probe_providers_up{status="OK"} 21' in doc


def test_alert_rule_contract():
    text = ALERTS.read_text(encoding="utf-8")
    assert "alert: TaskRouterCodeStale" in text
    assert "task_router_code_stale == 1" in text
    assert "alert: TaskRouterCoverageBelowFloor" in text
    assert "task_router_coverage_measured_ratio < 0.5" in text
    # Spec §6 table pins the deploy-parity rule's hold time.
    block = text.split("alert: TaskRouterCodeStale", 1)[1]
    assert re.search(r"^        for: 2h$", block.split("alert:", 1)[0], re.M)
