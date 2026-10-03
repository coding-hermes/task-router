#!/usr/bin/env python3
"""quality_score.py — the quality-ladder score verb (TR-267).

One data-driven ladder (metric, stage, target, priority) for the repo's
quality scores: type_hint_pct, coverage_pct, wiring_pct. This script prints
every metric's CURRENT value, the stage that value has reached, and the next
target — plus the regression floor, the only value on the ladder that may
ever block.

THE RULE (owner directive 2026-10-03), stated here so no reader has to infer
it: a stage target NEVER blocks work. As other work comes down the ladder is
not important; it never gets to be the blocking reason for a commit, a CI run
or a tick close. The ONLY blocking value is the regression floor — once a
metric has achieved a stage target, it must never drop below that target
again. A drop below the floor is a regression; a low absolute number is not.

Measurement honesty:

* type_hint_pct is measured HERE, by an AST scan over scripts/ (method is
  stated in the output). Two numbers are reported: the headline RETURN
  annotation coverage (functions with a return annotation / all
  FunctionDef+AsyncFunctionDef) and the ARGUMENT annotation coverage
  (annotated params / all params), as separate numbers.
* coverage_pct and wiring_pct are NOT YET instrumented (TR-187 / TR-282
  pending). A metric that cannot be measured FAILS LOUDLY: a named
  METRIC-UNMEASURABLE error line, and a nonzero exit when that metric was
  the explicit --metric ask. Never a silent 0 — a fabricated 0 would look
  like a measured baseline and poison the ladder's floor logic.

Usage:
  quality_score.py [--json] [--metric NAME] [--check-floor]
                   [--ladder PATH] [--scripts-dir DIR]

Exit 0 normally; 1 on a --check-floor violation; 3 when an explicitly
requested metric is unmeasurable.
"""
import argparse
import ast
import json
import os
import sys

#: Metrics the ladder tracks, in report order.
METRICS = ("type_hint_pct", "coverage_pct", "wiring_pct")

#: Why each not-yet-instrumented metric is unmeasurable, by name.
UNMEASURABLE_REASON = {
    "coverage_pct": "no coverage runner instrumented yet (TR-187 pending)",
    "wiring_pct": "no wiring metric exists yet (TR-282 pending)",
}

#: The named error tag the fail-loud contract requires in the output.
METRIC_UNMEASURABLE = "METRIC-UNMEASURABLE"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LADDER = os.path.join(_REPO_ROOT, "data", "tables", "quality_ladder.jsonl")
DEFAULT_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")


class MetricUnmeasurable(Exception):
    """A metric with no instrumented measurement path (TR-187 / TR-282)."""


# ---------------------------------------------------------------------------
# The ladder table.
# ---------------------------------------------------------------------------

def load_ladder(path):
    """Parse the ladder JSONL -> list of row dicts (blank lines skipped)."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def stage_targets(ladder, metric):
    """stage -> target for one metric, sorted by stage (floor rows excluded).

    The `floor` row kind carries stage=null and is not a rung; it is handled
    by floor_value() below.
    """
    table = {}
    for row in ladder:
        if row.get("metric") == metric and row.get("stage") is not None:
            table[int(row["stage"])] = float(row["target"])
    return dict(sorted(table.items()))


def stage_for(value, targets):
    """(highest achieved stage, next target above the value) for one metric.

    achieved is None while the value sits below the stage-0 target; next is
    None once every rung is met.
    """
    achieved = None
    for stage, target in sorted(targets.items()):
        if value >= target:
            achieved = stage
    next_target = None
    for target in sorted(targets.values()):
        if value < target:
            next_target = target
            break
    return achieved, next_target


def metric_floor(ladder, metric, baseline):
    """The metric's regression floor for a recorded baseline.

    `baseline` is the metric's LAST RECORDED value (caller-supplied via
    --baseline: the ladder deliberately stores no floor number). The floor is
    the highest stage target that baseline had achieved — "a drop below the
    last achieved stage target fails". No recorded history (baseline below
    every target, or none given) means no stage was ever achieved, so the
    floor is 0.0 and cannot trip: the ladder observes until it has something
    to protect. This is the ONLY blocking value on the ladder.
    """
    targets = stage_targets(ladder, metric)
    achieved = [t for t in targets.values() if t <= baseline]
    return max(achieved) if achieved else 0.0


# ---------------------------------------------------------------------------
# type_hint_pct — the AST scan (the one instrumented metric).
# ---------------------------------------------------------------------------

def scan_type_hints(scripts_dir):
    """AST scan over scripts/ -> measurement dict.

    Counts every FunctionDef and AsyncFunctionDef reachable in the tree
    (methods, nested and module-level functions alike — ast.walk sees them
    all). Return coverage is the headline number; argument coverage is
    reported alongside as its own number, never merged into it.
    """
    ret_total = ret_annotated = 0
    arg_total = arg_annotated = 0
    files = 0
    syntax_errors = []
    for dirpath, dirnames, filenames in os.walk(scripts_dir):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            files += 1
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            try:
                tree = ast.parse(source, filename=path)
            except SyntaxError as exc:
                syntax_errors.append(f"{path}: {exc.msg} (line {exc.lineno})")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                ret_total += 1
                if node.returns is not None:
                    ret_annotated += 1
                a = node.args
                params = (list(a.posonlyargs) + list(a.args)
                          + ([a.vararg] if a.vararg else [])
                          + list(a.kwonlyargs)
                          + ([a.kwarg] if a.kwarg else []))
                arg_total += len(params)
                arg_annotated += sum(1 for p in params if p.annotation is not None)
    if ret_total == 0:
        raise MetricUnmeasurable(
            f"AST scan found no functions under {scripts_dir} — nothing to score")
    ret_pct = 100.0 * ret_annotated / ret_total
    arg_pct = 100.0 * arg_annotated / arg_total if arg_total else 0.0
    return {
        "method": "ast-scan",
        "return_annotated": ret_annotated,
        "return_total": ret_total,
        "arg_annotated": arg_annotated,
        "arg_total": arg_total,
        "files_scanned": files,
        "syntax_errors": syntax_errors,
        "return_pct": round(ret_pct, 2),
        "arg_pct": round(arg_pct, 2),
    }


def measure(metric, scripts_dir):
    """Current value for one metric, or MetricUnmeasurable. NEVER silent 0."""
    if metric == "type_hint_pct":
        return scan_type_hints(scripts_dir)["return_pct"]
    reason = UNMEASURABLE_REASON.get(metric)
    if reason is None:
        reason = f"no measurement path implemented for {metric!r}"
    raise MetricUnmeasurable(reason)


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------

def build_report(ladder, scripts_dir, only=None):
    """Report rows for the requested metrics (default: all of METRICS).

    Each row carries value, stage, next_target and the measurement method.
    An unmeasurable metric carries the named error tag and its ladder
    targets — it never pretends to a value.
    """
    names = [only] if only else list(METRICS)
    rows = []
    for metric in names:
        targets = stage_targets(ladder, metric)
        try:
            value = measure(metric, scripts_dir)
        except MetricUnmeasurable as exc:
            rows.append({"metric": metric, "error": METRIC_UNMEASURABLE,
                         "reason": str(exc),
                         "targets": targets, "stage": None, "next_target": None,
                         "method": None})
            continue
        achieved, nxt = stage_for(value, targets)
        method = "ast-scan" if metric == "type_hint_pct" else None
        rows.append({"metric": metric, "value": value, "stage": achieved,
                     "next_target": nxt, "targets": targets, "method": method})
    return rows


def render(report, scripts_dir):
    """Human-readable score sheet."""
    out = ["== quality ladder score (TR-267) =="]
    for row in report:
        if "error" in row:
            out.append(f"{row['metric']}: {METRIC_UNMEASURABLE} — {row['reason']}")
            continue
        out.append(f"{row['metric']}: value={row['value']:.2f}  "
                   f"stage={row['stage'] if row['stage'] is not None else 'below-stage-0'}"
                   f"  next_target={row['next_target'] if row['next_target'] is not None else 'ladder-complete'}"
                   f"  method={row['method']}")
    # The AST scan detail (return vs argument coverage) rides along whenever
    # type_hint_pct was measured — two separate numbers, stated as such.
    type_row = next((r for r in report if r["metric"] == "type_hint_pct"
                     and "error" not in r), None)
    if type_row is not None:
        m = scan_type_hints(scripts_dir)
        out.append(
            f"  type_hint detail: return-annotated {m['return_annotated']}/"
            f"{m['return_total']} functions ({m['return_pct']:.2f}%) | "
            f"arg-annotated {m['arg_annotated']}/{m['arg_total']} params "
            f"({m['arg_pct']:.2f}%) | {m['files_scanned']} files,"
            f" {len(m['syntax_errors'])} syntax errors | method={m['method']}")
        for err in m["syntax_errors"]:
            out.append(f"  scan-warning: {err}")
    out.append("rule: stage targets NEVER block a commit, CI run or tick close — "
               "the only blocking value is the regression floor "
               "(a drop below the highest achieved stage target)")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="quality-ladder score verb: current value, stage, next "
                    "target per metric (TR-267)")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-parseable JSON")
    ap.add_argument("--metric", choices=METRICS, default=None,
                    help="score one metric instead of all")
    ap.add_argument("--check-floor", action="store_true",
                    help="exit 1 if a measured metric sits below its "
                         "regression floor (the ladder's only blocking value)")
    ap.add_argument("--baseline", default=None, action="append",
                    dest="baselines", metavar="METRIC=VALUE",
                    help="last recorded value for a metric, e.g. "
                         "--baseline type_hint_pct=21.3; the floor is the "
                         "highest stage target the BASELINE achieved "
                         "(repeatable; the ladder stores no floor number)")
    ap.add_argument("--ladder", default=DEFAULT_LADDER,
                    help="path to quality_ladder.jsonl")
    ap.add_argument("--scripts-dir", default=DEFAULT_SCRIPTS_DIR,
                    help="scripts/ tree the AST scan measures")
    args = ap.parse_args(argv)

    baselines = {}
    if args.baselines:
        for spec in args.baselines:
            if "=" not in spec:
                ap.error(f"--baseline expects METRIC=VALUE, got {spec!r}")
            name, _, raw = spec.partition("=")
            baselines[name.strip()] = raw

    ladder = load_ladder(args.ladder)
    report = build_report(ladder, args.scripts_dir, only=args.metric)

    unmeasured = [r for r in report if "error" in r]
    if args.metric is not None and unmeasured:
        # Explicit ask for a metric with no instrumented path: fail LOUDLY —
        # named error line on stderr AND a nonzero exit. Never a silent 0.
        row = unmeasured[0]
        print(f"{METRIC_UNMEASURABLE}: {row['metric']} — {row['reason']}",
              file=sys.stderr)
        if args.json:
            print(json.dumps(row))
        return 3

    if args.json:
        payload = {"metrics": report, "scripts_dir": args.scripts_dir}
        print(json.dumps(payload))
    else:
        sys.stdout.write(render(report, args.scripts_dir))

    if args.check_floor:
        violations = []
        for row in report:
            if "error" in row or row.get("value") is None:
                continue
            raw = baselines.get(row["metric"])
            if raw is None:
                # No recorded baseline: no stage was ever achieved, the floor
                # cannot trip. Stated, never implied.
                print(f"floor: {row['metric']} — no recorded baseline, floor=0.0 "
                      "(nothing to protect yet)", file=sys.stderr)
                continue
            baseline = float(raw)
            floor = metric_floor(ladder, row["metric"], baseline)
            print(f"floor: {row['metric']} baseline={baseline:.2f} -> floor="
                  f"{floor:.2f}, current={row['value']:.2f}", file=sys.stderr)
            if row["value"] < floor:
                violations.append((row, floor))
        if violations:
            for row, floor in violations:
                print(f"FLOOR VIOLATION: {row['metric']} value "
                      f"{row['value']:.2f} < floor {floor:.2f} — a drop below "
                      "the last achieved stage target (the ONLY blocking "
                      "value on the ladder)", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
