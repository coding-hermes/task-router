"""TR-267 — the quality ladder is data-driven, monotone and never blocking.

Four contract families, straight from the row:

1. TARGETS ARE MONOTONE NON-DECREASING per metric. A ladder whose rungs
   descend (60 then 40) is not a ladder; a later stage with a lower target
   would let a metric "achieve" a stage by regressing.
2. EVERY ROW CARRIES definition_cmd. A target nobody can re-derive from a
   mechanical command is an opinion; the ladder is only data-driven if each
   metric's number can be regenerated on demand.
3. NOTHING BLOCKS EXCEPT THE FLOOR. The owner rule (2026-10-03): stage
   targets never block a commit, CI run or tick close; the only blocking
   value is the regression floor. This test is the gate that keeps the
   rule true in the data — any future `block: true` on a stage row fails
   here, loudly, before it can wedge a tick.
4. THE SCORE VERB EXISTS AND MEASURES. scripts/quality_score.py runs and
   emits a parseable float for type_hint_pct (its instrumented metric),
   and fails LOUDLY (named METRIC-UNMEASURABLE, nonzero exit) for the
   metrics TR-187/TR-282 have not instrumented — never a silent 0.

Rows are mutated IN MEMORY, never on disk: the ladder file is not rewritten
by any test (a test that "fixes" the ladder to pass would be the exact
defect family 3 exists to stop).
"""
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LADDER_PATH = os.path.join(REPO, "data", "tables", "quality_ladder.jsonl")
SCORE_SCRIPT = os.path.join(REPO, "scripts", "quality_score.py")

METRICS = ("type_hint_pct", "coverage_pct", "wiring_pct")
#: The one row kind allowed to carry block=true (see docstring family 3).
FLOOR_METRIC = "floor"


def load_ladder():
    rows = []
    with open(LADDER_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def stage_rows(rows, metric):
    """The metric's stage rungs, ordered by stage. Floor rows are not rungs."""
    return sorted((r for r in rows
                   if r.get("metric") == metric and r.get("stage") is not None),
                  key=lambda r: r["stage"])


# ---------------------------------------------------------------------------
# Family 1: monotone non-decreasing targets per metric.
# ---------------------------------------------------------------------------

def test_every_metric_has_all_six_stages():
    rows = load_ladder()
    for metric in METRICS:
        stages = [r["stage"] for r in stage_rows(rows, metric)]
        assert stages == [0, 1, 2, 3, 4, 5], (
            f"{metric}: ladder must carry stages 0..5 exactly once, got {stages}")


@pytest.mark.parametrize("metric", METRICS)
def test_targets_are_monotone_nondecreasing(metric):
    rows = load_ladder()
    targets = [float(r["target"]) for r in stage_rows(rows, metric)]
    for earlier, later in zip(targets, targets[1:]):
        assert later >= earlier, (
            f"{metric}: targets {targets} are not monotone non-decreasing "
            f"(stage targets {earlier} -> {later})")


def test_type_hint_targets_match_the_owner_directive():
    # Owner directive 2026-10-03: 20/40/60/70/80/90 across stages 0..5.
    rows = load_ladder()
    targets = [float(r["target"]) for r in stage_rows(rows, "type_hint_pct")]
    assert targets == [20.0, 40.0, 60.0, 70.0, 80.0, 90.0]


# ---------------------------------------------------------------------------
# Family 2: every row is re-derivable — definition_cmd present where required.
# ---------------------------------------------------------------------------

def test_every_stage_row_has_a_definition_cmd():
    rows = load_ladder()
    for row in rows:
        if row.get("metric") == FLOOR_METRIC:
            continue  # the floor is computed by quality_score.py, not a command
        cmd = row.get("definition_cmd")
        assert cmd, f"row without definition_cmd: {row.get('metric')} stage {row.get('stage')}"
        assert isinstance(cmd, str) and cmd.strip(), (
            f"blank definition_cmd on {row.get('metric')} stage {row.get('stage')}")


def test_stage_definition_cmds_name_a_mechanical_measurement():
    # "Mechanical" means runnable: the command must reference the score verb
    # (or a script/make target), not prose. Every stage row here points at
    # scripts/quality_score.py with the metric named.
    rows = load_ladder()
    for row in rows:
        if row.get("metric") == FLOOR_METRIC:
            continue
        assert "quality_score.py" in row["definition_cmd"], (
            f"{row.get('metric')} stage {row.get('stage')}: definition_cmd is "
            f"not a mechanical measurement command: {row['definition_cmd']!r}")
        assert row["metric"] in row["definition_cmd"], (
            f"{row.get('metric')} stage {row.get('stage')}: definition_cmd does "
            f"not select its own metric")


# ---------------------------------------------------------------------------
# Family 3: THE RULE — nothing blocks except the floor row.
# ---------------------------------------------------------------------------

def test_floor_row_exists_and_is_the_only_blocking_row():
    rows = load_ladder()
    floors = [r for r in rows if r.get("metric") == FLOOR_METRIC]
    assert len(floors) == 1, "exactly one floor row kind must exist"
    assert floors[0].get("block") is True, "the floor row must be the blocking row"
    for row in rows:
        if row.get("metric") == FLOOR_METRIC:
            continue
        assert row.get("block") is False, (
            f"BLOCKING STAGE ROW — violates the owner rule that stage targets "
            f"never block: {row.get('metric')} stage {row.get('stage')} has "
            f"block={row.get('block')!r}")


def test_no_stage_row_may_ever_be_made_blocking():
    # The mutation arm: flipping any stage row to blocking must FAIL this
    # suite's own predicate (proves family 3's check has teeth rather than
    # passing vacuously on today's file shape).
    rows = load_ladder()
    stage_row = next(r for r in rows if r.get("metric") != FLOOR_METRIC)
    mutated = [dict(r, block=True) if r is stage_row else r for r in rows]
    offenders = [r for r in mutated
                 if r.get("metric") != FLOOR_METRIC and r.get("block")]
    assert offenders, "mutation must produce a blocking stage row"
    # ...and the real predicate must reject it:
    with pytest.raises(AssertionError):
        for row in mutated:
            if row.get("metric") == FLOOR_METRIC:
                continue
            assert row.get("block") is False


# ---------------------------------------------------------------------------
# Family 4: the score verb exists, measures, and fails loudly.
# ---------------------------------------------------------------------------

def run_score(argv):
    """Run scripts/quality_score.py in a subprocess (repo-relative)."""
    return subprocess.run([sys.executable, SCORE_SCRIPT] + argv,
                          capture_output=True, text=True, timeout=120,
                          cwd=REPO)


def test_score_script_exists():
    assert os.path.isfile(SCORE_SCRIPT), f"{SCORE_SCRIPT} missing"


def test_type_hint_pct_measures_and_parses_as_float():
    # The brief's acceptance: the script runs and prints a parsed float for
    # type_hint_pct, measured by the AST scan (method stated in output).
    proc = run_score(["--json", "--metric", "type_hint_pct"])
    assert proc.returncode == 0, f"quality_score.py failed: {proc.stderr}"
    payload = json.loads(proc.stdout)
    row = payload["metrics"][0]
    assert row["metric"] == "type_hint_pct"
    value = row["value"]
    assert isinstance(value, float), f"expected float, got {value!r}"
    assert 0.0 <= value <= 100.0
    assert row["method"] == "ast-scan", "measurement method must be stated"


def test_unmeasurable_metrics_fail_loudly_not_silently_zero():
    # coverage_pct / wiring_pct are not instrumented (TR-187 / TR-282):
    # explicit ask must produce the named error line AND a nonzero exit.
    # A silent 0 would read as a measured baseline and poison the floor.
    for metric in ("coverage_pct", "wiring_pct"):
        proc = run_score(["--metric", metric])
        assert proc.returncode != 0, (
            f"{metric} returned success — an unmeasurable metric must fail loudly")
        assert "METRIC-UNMEASURABLE" in proc.stderr, (
            f"{metric}: missing named METRIC-UNMEASURABLE error line; "
            f"stderr was: {proc.stderr!r}")


def test_full_report_names_unmeasurable_metrics_but_still_scores_the_rest():
    # Default run: the instrumented metric scores; the uninstrumented ones
    # are named as METRIC-UNMEASURABLE rows inside a successful report —
    # visible, never silently dropped, never fabricated as 0.
    proc = run_score(["--json"])
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    by_name = {r["metric"]: r for r in payload["metrics"]}
    assert set(by_name) == set(METRICS)
    assert "value" in by_name["type_hint_pct"]
    for metric in ("coverage_pct", "wiring_pct"):
        assert by_name[metric].get("error") == "METRIC-UNMEASURABLE"
        assert "value" not in by_name[metric], (
            f"{metric}: an unmeasurable metric must not carry a value (silent 0)")


def test_check_floor_passes_at_the_current_baseline():
    # Today no metric has achieved a stage (type_hint is ~0.5%), so every
    # floor is 0.0 and --check-floor must pass: the ladder observes, it does
    # not block (the rule). The violation arm below proves the flag itself.
    proc = run_score(["--check-floor"])
    assert proc.returncode == 0, f"unexpected floor violation: {proc.stderr}"


def test_check_floor_flag_detects_a_regression():
    # RED/GREEN for the floor mechanism, without touching the tree: drive
    # the verb against a synthetic ladder+scripts pair. The BASELINE
    # (last recorded value) is what achieves stages: baseline 100.0 has
    # achieved stage 0 (target 20), so the floor is 20.0 — a later tree
    # measuring below 20 is a regression and must exit 1.
    import json as _json
    import os as _os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ladder = _os.path.join(tmp, "ladder.jsonl")
        with open(ladder, "w", encoding="utf-8") as fh:
            fh.write(_json.dumps({"metric": "type_hint_pct", "stage": 0,
                                  "target": 20, "block": False,
                                  "definition_cmd": "x", "note": "t"}) + "\n")
        scripts = _os.path.join(tmp, "scripts")
        _os.makedirs(scripts)

        def write_func(name, annotated):
            ret = " -> int" if annotated else ""
            with open(_os.path.join(scripts, name), "w", encoding="utf-8") as fh:
                fh.write(f"def f(x){ret}:\n    return 1\n")

        common = ["--check-floor", "--ladder", ladder,
                  "--baseline", "type_hint_pct=100"]
        # GREEN: current tree at 100% sits above its floor (20.0).
        write_func("a_good.py", True)
        green = run_score(common + ["--scripts-dir", scripts])
        assert green.returncode == 0, f"unexpected violation: {green.stderr}"
        assert "floor=" in green.stderr, "floor derivation must be stated"
        # RED: the tree regresses to 0% — below the floor the 100.0
        # baseline earned. This must be the ONE thing that fails.
        write_func("a_good.py", False)
        red = run_score(common + ["--scripts-dir", scripts])
        assert red.returncode == 1, "a drop below the achieved floor must exit 1"
        assert "FLOOR VIOLATION" in red.stderr
        # And the boundary the owner rule demands: a baseline BELOW the
        # first stage target achieved nothing, so the floor cannot trip —
        # even a 0% tree is never blocked by stage targets.
        unearned = run_score(["--check-floor", "--ladder", ladder,
                              "--baseline", "type_hint_pct=5",
                              "--scripts-dir", scripts])
        assert unearned.returncode == 0, (
            "a baseline that never achieved a stage must not block")


def test_floor_is_dynamic_not_stored():
    # The ladder file stores no floor NUMBER (target is null on the floor
    # row): the floor is computed from the highest achieved stage target,
    # so re-seeding stage targets re-derives the floor. A stored number
    # would go stale the first time a target moved.
    rows = load_ladder()
    floor_rows = [r for r in rows if r.get("metric") == FLOOR_METRIC]
    assert floor_rows and floor_rows[0].get("target") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
