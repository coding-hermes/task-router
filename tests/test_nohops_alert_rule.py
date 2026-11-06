"""TR-371 — the repository-owned TaskRouterNoHopsHigh rule contract.

The live fleet-monitoring rule set lives OUTSIDE this repo (host
fleet-monitor, docs/observability-spec.md §5), so the row's deliverable HERE
is the rule artifact plus the tests that pin its contract:

  * exactly one rule, named TaskRouterNoHopsHigh (spec §6 alert table);
  * the expression is the spec §6 V3 no-hops ratio over the TR-377 exposition
    families (task_router_proxy_nohops_total /
    task_router_proxy_requests_total, 1h rate windows), and those families
    really render from the exposition registry;
  * the threshold literal equals data/nohops-baseline.json's `threshold` key —
    the one-computation invariant: the alert, the CLI and the UI share the
    baseline file, so a recomputed baseline without a matching rule edit
    fails here;
  * `for: 30m` per the spec's alert table;
  * the threshold source is documented on the rule itself (annotations) and
    in docs/observability-spec.md §6;
  * promtool (when installed) validates the file as a Prometheus rule file —
    skipped with a named reason where promtool is absent.

No test touches the network, and none claims the fleet-monitor deployment:
that surface is unverified from this repo by construction.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
RULE_FILE = REPO / "monitoring" / "task-router-alerts.yml"
BASELINE_FILE = REPO / "data" / "nohops-baseline.json"
SPEC_FILE = REPO / "docs" / "observability-spec.md"
sys.path.insert(0, str(REPO / "scripts"))

ALERT_NAME = "TaskRouterNoHopsHigh"
NOHOPS_METRIC = "task_router_proxy_nohops_total"
REQUESTS_METRIC = "task_router_proxy_requests_total"


def _rules_doc():
    assert RULE_FILE.exists(), (
        "monitoring/task-router-alerts.yml is missing — the TR-371 rule "
        "contract of record is gone")
    doc = yaml.safe_load(RULE_FILE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and isinstance(doc.get("groups"), list), (
        "rule file must carry a top-level `groups` list")
    rules = [r for g in doc["groups"] for r in (g.get("rules") or [])]
    assert rules, "rule file declares no rules"
    return doc, rules


def _the_rule():
    _doc, rules = _rules_doc()
    named = [r for r in rules if r.get("alert") == ALERT_NAME]
    assert len(named) == 1, (
        f"expected exactly one {ALERT_NAME} rule, found {len(named)}: "
        f"{[r.get('alert') for r in rules]}")
    return named[0]


def test_rule_file_exists_and_parses():
    _doc, rules = _rules_doc()
    assert all(isinstance(r, dict) for r in rules), "malformed rule entries"


def test_exactly_one_taskrouternohopshigh_rule():
    _the_rule()


def test_expression_is_the_spec_v3_ratio():
    expr = str(_the_rule()["expr"])
    for metric in (NOHOPS_METRIC, REQUESTS_METRIC):
        pat = r"sum\(rate\(" + re.escape(metric) + r"\[1h\]\)\)"
        assert re.search(pat, expr), (
            f"rule expression does not aggregate {metric} over a 1h rate "
            f"window (spec §6 V3): {expr!r}")
    assert ">" in expr, "rule expression carries no threshold comparison"


def test_threshold_equals_baseline_file():
    """The one-computation invariant: rule threshold == baseline file.

    data/nohops-baseline.json is the threshold source for BOTH the CLI alert
    (router_nohops.resolve_threshold, "baseline file threshold") and this
    rule. A recomputed baseline committed without the rule edit must fail
    here, not ship a silently recalibrated alert.
    """
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    threshold = baseline.get("threshold")
    assert isinstance(threshold, (int, float)), (
        "data/nohops-baseline.json has no numeric `threshold` key")
    # The file's own documented construction: rate + headroom (tolerance for
    # binary float rounding, e.g. 0.177329 + 0.05).
    rate, headroom = baseline.get("rate"), baseline.get("headroom")
    assert isinstance(rate, (int, float)) and isinstance(headroom, (int, float))
    assert abs(threshold - (rate + headroom)) < 1e-9, (
        "baseline threshold no longer equals rate + headroom — the file's "
        "documented construction broke")
    expr = str(_the_rule()["expr"]).strip()
    m = re.search(r">\s*([0-9]+(?:\.[0-9]+)?)\s*$", expr)
    assert m, f"rule expression carries no parseable threshold: {expr!r}"
    assert float(m.group(1)) == float(threshold), (
        f"rule threshold {m.group(1)} != baseline threshold {threshold} — "
        "data/nohops-baseline.json was recomputed without the rule edit; "
        "update monitoring/task-router-alerts.yml in the same commit")


def test_rule_metrics_render_from_the_exposition():
    """The ratio's inputs must be families the TR-377 registry really emits
    (under both the router_ name and the task_router_ alias the rule uses)."""
    import router_prometheus
    doc = router_prometheus.render_metrics().decode("utf-8")
    for name in (NOHOPS_METRIC, REQUESTS_METRIC,
                 "router_proxy_nohops_total", "router_proxy_requests_total"):
        assert f"# TYPE {name} counter" in doc, (
            f"exposition does not declare {name} — the rule would alert on "
            "a ratio with a missing input")


def test_for_duration_matches_spec_table():
    assert _the_rule().get("for") == "30m"


def test_threshold_source_is_documented_on_the_rule():
    ann = _the_rule().get("annotations") or {}
    src = str(ann.get("threshold_source") or "")
    assert "nohops-baseline.json" in src, (
        f"rule annotations must name data/nohops-baseline.json as the "
        f"threshold source, got: {src!r}")


def test_rule_labels_carry_product_identity():
    labels = _the_rule().get("labels") or {}
    assert labels.get("product") == "task-router"
    assert isinstance(labels.get("severity"), str) and labels["severity"], (
        "rule carries no severity label")


def test_spec_documents_the_artifact_and_threshold_source():
    spec = SPEC_FILE.read_text(encoding="utf-8")
    assert "monitoring/task-router-alerts.yml" in spec, (
        "docs/observability-spec.md §6 must name the rule artifact")
    assert "nohops-baseline.json" in spec, (
        "docs/observability-spec.md §6 must document the baseline threshold "
        "source")


def test_promtool_validates_the_rule_file():
    if shutil.which("promtool") is None:
        pytest.skip("none:promtool not installed on this host — the "
                    "structural checks above are the coverage here")
    r = subprocess.run(
        ["promtool", "check", "rules", str(RULE_FILE)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (
        f"promtool rejected the rule file:\n{r.stdout}\n{r.stderr}")
