#!/usr/bin/env python3
"""TR-1000976681059241 — quality guard: regression floor for the quality artifact.

Reads data/state/quality-artifact.json and checks the type_hint_pct metric
against a stored regression floor.  The floor is the last-seen value per
metric (persisted in data/state/.quality-floors.json).

Guard logic (the ladder's own rule: a stage target NEVER blocks):
- If the metric has not been measured yet (value=null) — PASS, log info.
- If current value < stored floor for this metric — FAIL (exit 1, block).
- If current value >= stored floor — PASS (even if below target).
- On PASS, the floor is updated to the current value (monotone non-decreasing
  floor is enforced: the floor only rises, never falls).

A metric miss (low value) never blocks by default — only regression below
the last-seen floor triggers a hard failure.

Usage:
  python3 scripts/quality_guard.py [--check-only] [--json]

Exit codes:
  0 — PASS (no regression detected)
  1 — FAIL (regression below floor detected or artifact invalid)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ARTIFACT_PATH = REPO_ROOT / "data" / "state" / "quality-artifact.json"
_DEFAULT_FLOOR_PATH = REPO_ROOT / "data" / "state" / ".quality-floors.json"

# Allow tests to override paths via env vars (consistent with router scripts).
ARTIFACT_PATH = Path(os.environ.get("QUALITY_ARTIFACT_PATH", _DEFAULT_ARTIFACT_PATH))
FLOOR_PATH = Path(os.environ.get("QUALITY_FLOOR_PATH", _DEFAULT_FLOOR_PATH))
METRIC = "type_hint_pct"  # The only measured metric in the artifact
MEASUREMENT_ERROR = 0.5  # Percentage points tolerance (floor only rises if ≥ error margin)

# ──────────────────────────────────────────────────────────────────────
# Public helpers (callable from guard script)
# ──────────────────────────────────────────────────────────────────────


def _load_floors() -> dict:
    """Load the regression floor file; create empty if missing."""
    if FLOOR_PATH.exists():
        try:
            return json.loads(FLOOR_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_floors(floors: dict) -> None:
    """Persist regression floors atomically."""
    FLOOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    FLOOR_PATH.write_text(json.dumps(floors, indent=2, sort_keys=True) + "\n")


def check_quality_artifact() -> dict:
    """Validate the artifact and check for regression.

    Returns a dict with:
      - pass: bool — True if no regression detected
      - current: float | None — current type_hint_pct value
      - floor: float | None — stored floor value
      - message: str — human-readable explanation
      - artifact_valid: bool — True if artifact passed schema validation
    """
    result = {
        "pass": False,
        "current": None,
        "floor": None,
        "message": "",
        "artifact_valid": False,
    }

    # 1. Read and validate the artifact.
    if not ARTIFACT_PATH.exists():
        rel = str(ARTIFACT_PATH.relative_to(REPO_ROOT)) if str(ARTIFACT_PATH).startswith(str(REPO_ROOT)) else str(ARTIFACT_PATH)
        result["message"] = (
            f"Artifact not found at {rel}. "
            f"Run 'python3 scripts/quality_artifact.py' first."
        )
        return result

    try:
        artifact = json.loads(ARTIFACT_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        result["message"] = f"Artifact is invalid JSON: {exc}"
        return result

    # Schema validation.
    if artifact.get("schema_version") != 1:
        result["message"] = (
            f"Unknown schema_version {artifact.get('schema_version')} "
            f"(expected 1)"
        )
        return result

    metrics = artifact.get("metrics", [])
    if not isinstance(metrics, list) or len(metrics) == 0:
        result["message"] = "Artifact has no metrics list."
        return result

    result["artifact_valid"] = True

    # 2. Find the type_hint_pct metric.
    metric_entry = None
    for m in metrics:
        if m.get("metric") == METRIC:
            metric_entry = m
            break

    if metric_entry is None:
        result["message"] = (
            f"Artifact missing metric '{METRIC}'."
        )
        return result

    # 3. Check if the metric has been measured.
    current_value = metric_entry.get("value")
    if current_value is None:
        result["pass"] = True
        result["current"] = None
        result["message"] = (
            f"{METRIC} is not yet measured (value=null). "
            f"Skipping regression check."
        )
        return result

    result["current"] = float(current_value)

    # 4. Load current floor.
    floors = _load_floors()
    old_floor = floors.get(METRIC)

    if old_floor is not None:
        result["floor"] = float(old_floor)
        # Check for regression: current must be >= floor minus tolerance.
        if result["current"] < float(old_floor) - MEASUREMENT_ERROR:
            result["pass"] = False
            result["message"] = (
                f"{METRIC} REGRESSION: {result['current']:.2f}% < "
                f"floor {result['floor']:.2f}% "
                f"(tolerance {MEASUREMENT_ERROR}pp). "
                f"Quality has regressed — do not land this commit."
            )
            return result
        else:
            result["pass"] = True
            result["message"] = (
                f"{METRIC} OK: {result['current']:.2f}% >= "
                f"floor {result['floor']:.2f}%."
            )
    else:
        # No floor yet — pass and establish one.
        result["pass"] = True
        result["message"] = (
            f"{METRIC} first measurement: {result['current']:.2f}%. "
            f"Floor established."
        )

    # 5. Update floor (only if current >= old_floor).
    new_floor = result["current"]
    if old_floor is None or new_floor > float(old_floor):
        floors[METRIC] = new_floor
        _save_floors(floors)

    return result


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quality guard: regression floor for the quality artifact"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Exit 0 even on regression (useful for logging without blocking)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the check result as JSON to stdout",
    )
    args = parser.parse_args(argv)

    result = check_quality_artifact()

    if args.json:
        print(json.dumps(result, indent=2))
        # When --json is set, stdout is JSON-only; no extra text.
        if not result["pass"]:
            print(f"FAIL: {result['message']}", file=sys.stderr)
            if args.check_only:
                return 0  # --check-only overrides exit code
            return 1
        if result["pass"]:
            print(f"PASS: {result['message']}", file=sys.stderr)
        else:
            print(f"SKIP: {result['message']}", file=sys.stderr)
        return 0 if result["pass"] else (0 if args.check_only else 1)

    # Non-JSON mode: human-readable stdout + stderr only.
    if not args.check_only and not result["pass"]:
        print(f"FAIL: {result['message']}", file=sys.stderr)
        return 1

    if result["pass"]:
        print(f"PASS: {result['message']}")
    else:
        print(f"SKIP: {result['message']}", file=sys.stderr)

    return 0 if result["pass"] else (0 if args.check_only else 1)


if __name__ == "__main__":
    sys.exit(main())
