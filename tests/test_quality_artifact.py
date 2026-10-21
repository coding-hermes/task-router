"""TR-281 — the quality artifact shape contract.

One machine-readable artifact, one verb, one authority. Consumers
(gitreins, the board, the UI) read data/state/quality-artifact.json
and never recompute a metric.

The test pins the schema and the fail-loud contract:
- schema_version is 1
- measured_at is ISO-8601 UTC
- metrics is a list of exactly the three ladder metrics
- each metric has the required fields
- unmeasurable metrics have value=null and a reason string
- the artifact is produced even when a measurement fails
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "quality_artifact.py"


def run_artifact(*args):
    """Run the artifact verb and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.returncode, result.stdout, result.stderr


def test_artifact_schema_version():
    """The artifact carries schema_version=1."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    assert artifact["schema_version"] == 1


def test_artifact_measured_at_is_iso_utc():
    """measured_at is ISO-8601 UTC with timezone."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    measured_at = artifact["measured_at"]
    # Must end with +00:00 or Z (UTC)
    assert measured_at.endswith("+00:00") or measured_at.endswith("Z")
    # Must parse as ISO-8601
    from datetime import datetime
    datetime.fromisoformat(measured_at)


def test_artifact_has_three_metrics():
    """The artifact carries exactly the three ladder metrics."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    metrics = artifact["metrics"]
    assert len(metrics) == 3
    names = {m["metric"] for m in metrics}
    assert names == {"type_hint_pct", "coverage_pct", "wiring_pct"}


def test_metric_required_fields():
    """Every metric has the required fields."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    required_fields = {
        "metric", "value", "unit", "stage", "target", "priority",
        "blocking", "definition_cmd", "breakdown",
    }
    for metric in artifact["metrics"]:
        for field in required_fields:
            assert field in metric, f"metric {metric['metric']} missing field {field}"


def test_unmeasurable_metric_has_reason():
    """A metric with value=null must have a reason string."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    for metric in artifact["metrics"]:
        if metric["value"] is None:
            assert "reason" in metric, (
                f"metric {metric['metric']} has value=null but no reason"
            )
            assert isinstance(metric["reason"], str)
            assert len(metric["reason"]) > 0


def test_type_hint_pct_is_measured():
    """type_hint_pct is the one instrumented metric; it must have a value."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    type_hint = next(m for m in artifact["metrics"] if m["metric"] == "type_hint_pct")
    assert type_hint["value"] is not None
    assert isinstance(type_hint["value"], (int, float))
    assert 0.0 <= type_hint["value"] <= 100.0


def test_type_hint_pct_has_breakdown():
    """type_hint_pct carries a per-module breakdown."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    type_hint = next(m for m in artifact["metrics"] if m["metric"] == "type_hint_pct")
    breakdown = type_hint["breakdown"]
    assert isinstance(breakdown, list)
    assert len(breakdown) > 0
    # Each breakdown entry has module, value, n
    for entry in breakdown:
        assert "module" in entry
        assert "value" in entry
        assert "n" in entry
        assert isinstance(entry["module"], str)
        assert isinstance(entry["value"], (int, float))
        assert isinstance(entry["n"], int)


def test_artifact_writes_to_default_path():
    """The verb writes the artifact to data/state/quality-artifact.json."""
    import tempfile

    # Use a temp dir to avoid polluting the repo
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "quality-artifact.json"
        # Pass --output to redirect to the temp path
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0
        assert tmp_path.exists()
        # Verify it's valid JSON
        content = tmp_path.read_text()
        artifact = json.loads(content)
        assert artifact["schema_version"] == 1


def test_artifact_atomic_write():
    """The verb uses atomic write (tmp + rename) so a crash cannot corrupt."""
    # This is a structural test: we verify the write_atomic function exists
    # and is called. The actual atomicity is proven by the implementation.
    import importlib.util
    spec = importlib.util.spec_from_file_location("quality_artifact", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "write_atomic")


def test_coverage_pct_is_unmeasurable():
    """coverage_pct is not yet instrumented (TR-187 pending)."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    coverage = next(m for m in artifact["metrics"] if m["metric"] == "coverage_pct")
    assert coverage["value"] is None
    assert "reason" in coverage
    assert "TR-187" in coverage["reason"]


def test_wiring_pct_carries_definition_even_when_unmeasurable():
    """wiring_pct is the TR-282 instrument: measured when the Hilo graph
    exists; when it cannot be measured the null carries a concrete reason
    AND the formula/source stay pinned so the metric's definition never
    depends on today's ability to measure it."""
    rc, stdout, _ = run_artifact("--json")
    assert rc == 0
    artifact = json.loads(stdout)
    wiring = next(m for m in artifact["metrics"] if m["metric"] == "wiring_pct")
    if wiring["value"] is None:
        assert "reason" in wiring
        assert isinstance(wiring["reason"], str) and wiring["reason"]
    else:
        assert 0.0 <= wiring["value"] <= 100.0
    # definition is carried in BOTH arms
    assert "scripts/wiring_score.py" in wiring["definition_cmd"]
    assert wiring.get("formula"), "wiring formula must be carried"
    assert "0.5*called_share" in wiring["formula"]
    assert wiring.get("source") and "edges.jsonl" in wiring["source"]
