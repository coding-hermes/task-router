"""TR-1000976681059241 — quality guard tests.

Verifies the quality guard as a consumer of the artifact:
- Reads the artifact and checks type_hint_pct against a regression floor
- Passes when value >= floor (or first measurement)
- Fails when value regresses below floor
- Handles missing artifact gracefully
- Handles invalid JSON gracefully
- Handles metric with value=null
- Floor is updated (monotone non-decreasing) after each pass
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "quality_guard.py"


def _run_guard(*args, tmp_dir: Path | None = None) -> tuple[int, str, str]:
    """Run the quality guard via subprocess.

    If tmp_dir is given, copies the artifact/floor there and runs with
    env vars pointing at tmp_dir. Always uses --json for clean parsing.
    """
    cmd = [sys.executable, str(SCRIPT), "--json"] + list(args)
    env = dict(__import__("os").environ)
    if tmp_dir is not None:
        env["QUALITY_ARTIFACT_PATH"] = str(tmp_dir / "data" / "state" / "quality-artifact.json")
        env["QUALITY_FLOOR_PATH"] = str(tmp_dir / "data" / "state" / ".quality-floors.json")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, env=env)
    return result.returncode, result.stdout, result.stderr


def _run_guard_raw(*args, tmp_dir: Path | None = None) -> tuple[int, str, str]:
    """Run the quality guard WITHOUT --json (human-readable mode)."""
    cmd = [sys.executable, str(SCRIPT)] + list(args)
    env = dict(__import__("os").environ)
    if tmp_dir is not None:
        env["QUALITY_ARTIFACT_PATH"] = str(tmp_dir / "data" / "state" / "quality-artifact.json")
        env["QUALITY_FLOOR_PATH"] = str(tmp_dir / "data" / "state" / ".quality-floors.json")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, env=env)
    return result.returncode, result.stdout, result.stderr


def _make_artifact(value: float, tmp_dir: Path) -> Path:
    """Create a valid artifact JSON at tmp_dir and return the artifact file path."""
    artifact = {
        "schema_version": 1,
        "measured_at": "2026-10-21T00:00:00+00:00",
        "metrics": [
            {
                "metric": "type_hint_pct",
                "value": value,
                "unit": "percent",
                "stage": 1,
                "target": 40.0,
                "priority": "P1",
                "blocking": False,
                "definition_cmd": "python3 scripts/quality_score.py --json --metric type_hint_pct",
                "breakdown": [],
            },
            {
                "metric": "coverage_pct",
                "value": None,
                "unit": "percent",
                "stage": 0,
                "target": 0.0,
                "priority": "P0",
                "blocking": False,
                "definition_cmd": "python3 scripts/quality_score.py --json --metric coverage_pct",
                "breakdown": [],
                "reason": "no coverage runner instrumented yet (TR-187 pending)",
            },
            {
                "metric": "wiring_pct",
                "value": None,
                "unit": "percent",
                "stage": 0,
                "target": 0.0,
                "priority": "P1",
                "blocking": False,
                "definition_cmd": "python3 scripts/wiring_score.py --json",
                "breakdown": [],
                "reason": "no wiring metric exists yet (TR-282 pending)",
            },
        ],
    }
    (tmp_dir / "data" / "state").mkdir(parents=True, exist_ok=True)
    artifact_file = tmp_dir / "data" / "state" / "quality-artifact.json"
    artifact_file.write_text(json.dumps(artifact, indent=2))
    return artifact_file


class TestQualityGuardFirstMeasurement:
    """First measurement: no floor exists yet."""

    def test_first_measurement_passes(self, tmp_path):
        """When no floor exists, first measurement passes and establishes floor."""
        _make_artifact(52.0, tmp_path)
        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 0
        result = json.loads(stdout)
        assert result["pass"] is True
        assert result["current"] == 52.0
        assert result["floor"] is None

    def test_first_measurement_establishes_floor(self, tmp_path):
        """First measurement writes the floor file."""
        _make_artifact(52.0, tmp_path)
        _run_guard(tmp_dir=tmp_path)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        assert floors_file.exists()
        floors = json.loads(floors_file.read_text())
        assert floors["type_hint_pct"] == 52.0


class TestQualityGuardRegression:
    """Regression detection: value below floor fails."""

    def test_regression_fails(self, tmp_path):
        """When current < floor, guard fails."""
        _make_artifact(40.0, tmp_path)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 1  # exit code 1 on regression
        result = json.loads(stdout)
        assert result["pass"] is False
        assert result["current"] == 40.0
        assert result["floor"] == 50.0

    def test_regression_passes_with_tolerance(self, tmp_path):
        """Within MEASUREMENT_ERROR tolerance (0.5pp), still passes."""
        _make_artifact(49.6, tmp_path)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 0
        result = json.loads(stdout)
        assert result["pass"] is True

    def test_regression_no_floor_update_on_fail(self, tmp_path):
        """Floor is NOT updated when regression detected."""
        _make_artifact(40.0, tmp_path)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))
        original_floor = 50.0

        _run_guard(tmp_dir=tmp_path)
        updated_floor = json.loads(floors_file.read_text())
        assert updated_floor["type_hint_pct"] == original_floor


class TestQualityGuardFloorUpdate:
    """Floor update: monotone non-decreasing."""

    def test_floor_rises_on_improvement(self, tmp_path):
        """When value > floor, floor is raised to current."""
        _make_artifact(55.0, tmp_path)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 0
        result = json.loads(stdout)
        assert result["pass"] is True
        floors = json.loads(floors_file.read_text())
        assert floors["type_hint_pct"] == 55.0

    def test_floor_unchanged_on_same_value(self, tmp_path):
        """When value == floor, floor stays (no regression)."""
        _make_artifact(50.0, tmp_path)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))
        original_floor = 50.0

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 0
        floors = json.loads(floors_file.read_text())
        assert floors["type_hint_pct"] == original_floor


class TestQualityGuardInvalidArtifact:
    """Handles invalid artifact gracefully."""

    def test_missing_artifact_fails(self, tmp_path):
        """When artifact file is missing, guard fails."""
        (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))

        # No artifact file created
        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 1
        result = json.loads(stdout)
        assert result["pass"] is False
        assert result["artifact_valid"] is False

    def test_invalid_json_fails(self, tmp_path):
        """When artifact contains invalid JSON, guard fails."""
        (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
        artifact_file = tmp_path / "data" / "state" / "quality-artifact.json"
        artifact_file.write_text("not json {{{")
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text("{}")

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 1
        result = json.loads(stdout)
        assert result["pass"] is False
        assert result["artifact_valid"] is False

    def test_unknown_schema_version_fails(self, tmp_path):
        """Schema version other than 1 is rejected."""
        (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
        artifact_file = tmp_path / "data" / "state" / "quality-artifact.json"
        artifact = {
            "schema_version": 2,
            "measured_at": "2026-10-21T00:00:00+00:00",
            "metrics": [],
        }
        artifact_file.write_text(json.dumps(artifact))
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text("{}")

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 1
        result = json.loads(stdout)
        assert result["pass"] is False

    def test_metric_not_found_fails(self, tmp_path):
        """Artifact exists but type_hint_pct metric is absent."""
        (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
        artifact_file = tmp_path / "data" / "state" / "quality-artifact.json"
        artifact = {
            "schema_version": 1,
            "measured_at": "2026-10-21T00:00:00+00:00",
            "metrics": [
                {
                    "metric": "coverage_pct",
                    "value": None,
                    "unit": "percent",
                },
            ],
        }
        artifact_file.write_text(json.dumps(artifact))
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text("{}")

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 1
        result = json.loads(stdout)
        assert result["pass"] is False


class TestQualityGuardNullMetric:
    """Metric with value=null: skip, don't fail."""

    def test_null_value_passes(self, tmp_path):
        """When type_hint_pct value is null, guard passes (unmeasurable)."""
        (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
        artifact_file = tmp_path / "data" / "state" / "quality-artifact.json"
        artifact = {
            "schema_version": 1,
            "measured_at": "2026-10-21T00:00:00+00:00",
            "metrics": [
                {
                    "metric": "type_hint_pct",
                    "value": None,
                    "unit": "percent",
                    "reason": "test null",
                },
            ],
        }
        artifact_file.write_text(json.dumps(artifact))

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 0
        result = json.loads(stdout)
        assert result["pass"] is True
        assert result["current"] is None

    def test_null_no_floor_update(self, tmp_path):
        """Null value does not update the floor."""
        (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
        artifact_file = tmp_path / "data" / "state" / "quality-artifact.json"
        artifact = {
            "schema_version": 1,
            "measured_at": "2026-10-21T00:00:00+00:00",
            "metrics": [
                {
                    "metric": "type_hint_pct",
                    "value": None,
                    "unit": "percent",
                    "reason": "test null",
                },
            ],
        }
        artifact_file.write_text(json.dumps(artifact))
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 0
        # Floor should be unchanged
        floors = json.loads(floors_file.read_text())
        assert floors["type_hint_pct"] == 50.0


class TestQualityGuardCheckOnly:
    """--check-only mode: always exits 0."""

    def test_check_only_ignores_failure(self, tmp_path):
        """--check-only returns exit 0 even when regression detected."""
        _make_artifact(40.0, tmp_path)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))

        rc, stdout, stderr = _run_guard("--check-only", tmp_dir=tmp_path)
        assert rc == 0

    def test_check_only_prints_result(self, tmp_path):
        """--check-only still prints the result."""
        _make_artifact(52.0, tmp_path)

        rc, stdout, _ = _run_guard("--check-only", tmp_dir=tmp_path)
        assert rc == 0
        result = json.loads(stdout)
        assert result["pass"] is True


class TestQualityGuardJsonOutput:
    """--json flag: prints JSON to stdout."""

    def test_json_output_format(self, tmp_path):
        """--json prints a valid JSON dict with expected keys."""
        _make_artifact(52.0, tmp_path)

        rc, stdout, _ = _run_guard(tmp_dir=tmp_path)
        assert rc == 0
        result = json.loads(stdout)
        # Must have these keys
        assert "pass" in result
        assert "current" in result
        assert "floor" in result
        assert "message" in result
        assert "artifact_valid" in result


class TestQualityGuardCLI:
    """CLI behavior tests (exit codes and output)."""

    def test_normal_exit_zero_on_pass(self, tmp_path):
        """Normal mode exits 0 when value >= floor."""
        _make_artifact(52.0, tmp_path)
        rc, stdout, stderr = _run_guard(tmp_dir=tmp_path)
        assert rc == 0
        result = json.loads(stdout)
        assert result["pass"] is True

    def test_normal_exit_one_on_regression(self, tmp_path):
        """Normal mode exits 1 when value < floor."""
        _make_artifact(40.0, tmp_path)
        floors_file = tmp_path / "data" / "state" / ".quality-floors.json"
        floors_file.write_text(json.dumps({"type_hint_pct": 50.0}))

        rc, stdout, stderr = _run_guard(tmp_dir=tmp_path)
        assert rc == 1
        result = json.loads(stdout)
        assert result["pass"] is False
