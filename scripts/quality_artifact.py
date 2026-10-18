#!/usr/bin/env python3
"""TR-281 — THE SCORE ARTIFACT: one verb, one path, one authority.

One machine-readable quality artifact so gitreins, the board and the UI
read the same numbers. This script is the SINGLE writer; every consumer
reads data/state/quality-artifact.json and never recomputes a metric.

Schema version 1:
{
  "schema_version": 1,
  "measured_at": "<ISO-8601 UTC>",
  "metrics": [
    {
      "metric": "<name>",
      "value": <float|null>,
      "unit": "<percent|null>",
      "stage": <int|null>,
      "target": <float|null>,
      "priority": "<P0..P3|null>",
      "blocking": <bool>,
      "definition_cmd": "<command|null>",
      "reason": "<string|null — present only when value is null>",
      "breakdown": [
        {"module": "<path>", "value": <float>, "n": <int>}
      ]
    }
  ]
}

Fail-loud contract:
- The verb exits non-zero when the artifact CANNOT be produced (e.g. the
  measurement script crashes, the output path is unwritable).
- A metric that cannot be measured is NOT a failure — it writes
  value=null with a reason string, and the artifact is still produced.
- A metric miss (low value) never blocks by default; only the regression
  floor (TR-267) blocks, and that is a separate check.

Usage:
  python3 scripts/quality_artifact.py [--output PATH]

Default output: data/state/quality-artifact.json (atomic write via .tmp + rename).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "state" / "quality-artifact.json"
SCHEMA_VERSION = 1

# The three metrics the ladder tracks (TR-267).
METRICS = ("type_hint_pct", "coverage_pct", "wiring_pct")


def measure_type_hint_pct() -> dict:
    """Run the stage-0 instrument and parse its JSON output.

    Returns {value, breakdown} where breakdown is [{module, value, n}].
    Raises RuntimeError if the instrument fails.
    """
    script = REPO_ROOT / "scripts" / "type_hint_score.py"
    if not script.exists():
        raise RuntimeError(f"type_hint_score.py not found at {script}")

    result = subprocess.run(
        [sys.executable, str(script), "--json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"type_hint_score.py exited {result.returncode}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"type_hint_score.py output not JSON: {exc}") from exc

    # The instrument returns {modules: [...], overall: {...}}.
    # We want the overall fn_score as the value, and per-module fn_score as breakdown.
    overall = data.get("overall", {})
    value = overall.get("fn_score")
    if value is None:
        raise RuntimeError("type_hint_score.py output missing overall.fn_score")

    breakdown = []
    for mod in data.get("modules", []):
        module_name = mod.get("module", "<unknown>")
        mod_value = mod.get("fn_score")
        mod_n = mod.get("functions", 0)
        if mod_value is not None:
            breakdown.append({"module": module_name, "value": mod_value, "n": mod_n})

    return {"value": float(value), "breakdown": breakdown}


def build_metric(name: str) -> dict:
    """Build one metric row for the artifact.

    For unmeasurable metrics (coverage_pct, wiring_pct), value=null with a reason.
    For measurable metrics, value is the measured number.
    """
    base = {
        "metric": name,
        "value": None,
        "unit": "percent",
        "stage": None,
        "target": None,
        "priority": None,
        "blocking": False,
        "definition_cmd": f"python3 scripts/quality_score.py --json --metric {name}",
        "breakdown": [],
    }

    if name == "type_hint_pct":
        try:
            measurement = measure_type_hint_pct()
            base["value"] = measurement["value"]
            base["breakdown"] = measurement["breakdown"]
            # Stage/target/priority come from the ladder (TR-267).
            # For now, hardcode stage 1 (40% target) as the current goal.
            base["stage"] = 1
            base["target"] = 40.0
            base["priority"] = "P1"
        except RuntimeError as exc:
            base["reason"] = f"measurement failed: {exc}"
    elif name == "coverage_pct":
        base["reason"] = "no coverage runner instrumented yet (TR-187 pending)"
        base["priority"] = "P0"
        base["stage"] = 0
        base["target"] = 0.0
    elif name == "wiring_pct":
        base["reason"] = "no wiring metric exists yet (TR-282 pending)"
        base["priority"] = "P1"
        base["stage"] = 0
        base["target"] = 0.0
    else:
        base["reason"] = f"unknown metric {name!r}"

    return base


def build_artifact() -> dict:
    """Build the full artifact."""
    metrics = [build_metric(name) for name in METRICS]
    return {
        "schema_version": SCHEMA_VERSION,
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": metrics,
    }


def write_atomic(path: Path, content: str) -> None:
    """Write content to path atomically via .tmp + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TR-281: write the quality artifact (one verb, one authority)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output path (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the artifact to stdout instead of writing it",
    )
    args = parser.parse_args(argv)

    try:
        artifact = build_artifact()
    except Exception as exc:
        print(f"ERROR: artifact cannot be produced: {exc}", file=sys.stderr)
        return 1

    content = json.dumps(artifact, indent=2, sort_keys=True)

    if args.json:
        print(content)
        return 0

    try:
        write_atomic(args.output, content)
    except OSError as exc:
        print(f"ERROR: cannot write artifact to {args.output}: {exc}", file=sys.stderr)
        return 1

    try:
        rel = args.output.relative_to(REPO_ROOT)
        print(f"artifact written to {rel}", file=sys.stderr)
    except ValueError:
        # Output path is outside the repo — print absolute path
        print(f"artifact written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
