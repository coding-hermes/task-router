"""TR-318 — measured probe rows win over survey estimates in the degenerate
replace (guard/mock/multilingual quality-estimate pass).

The evidence chain was complete but the last pass undid it:
  benchmarks.jsonl carries the measured GUARD-V2 row (glm-5.3-flash 0/4 ->
  score 0.0); BENCH_OVERLAY maps the source to the guard category;
  apply_overlay() writes model_perf (0.0, 'bench', 'bench:live-probe-.../GUARD-V2')
  ... and THEN apply_quality_estimates() ran its TR-002 degenerate replace,
  which keyed on the VALUE alone: `cur[0] in (0.0, 1.0)`. Every n=1 probe row
  lands exactly on 0.0 or 1.0 (a 4-check battery is 0/4 or 4/4), so the pass
  re-overwrote three measured cells with QUALITY_ESTIMATES survey values:
    glm-5.3-flash guard 0.0 -> 0.8   (the stomp this task was filed for)
    gpt-5.6-sol   guard 1.0 -> 0.92
    gpt-5.6-sol   mock  1.0 -> 0.86
  A measurement wins over an estimate in BOTH directions — evidence ranking,
  not score ranking — the same precedence apply_overlay already enforces.

  Hermetic: every seed run targets a scratch copy of data/tables + scratch
  registry/ns (the test_seed_ns_guard convention). No repo writes, no network.
  duckdb-gated like the other seed-driving suites.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
PY = sys.executable

_HAS_DUCKDB = pytest.importorskip("duckdb") is not None

#: (model, category) -> the bench row the committed data carries for it.
#: glm-5.3-flash guard is the row TR-318 was filed for (0/4 on GUARD-V2);
#: gpt-5.6-sol guard/mock are the other two members of the measured stomp set.
MEASURED = {
    ("glm-5.3-flash", "guard"): ("live-probe-2026-09-27/GUARD-V2", 0.0),
    ("gpt-5.6-sol", "guard"): ("live-probe-2026-09-27/GUARD-V2", 1.0),
    ("gpt-5.6-sol", "mock"): ("live-probe-2026-09-27/MOCK-V3", 1.0),
}
#: the survey estimates that used to win (data/tables/quality_estimates.jsonl).
SURVEY = {
    ("glm-5.3-flash", "guard"): 0.8,
    ("gpt-5.6-sol", "guard"): 0.92,
    ("gpt-5.6-sol", "mock"): 0.86,
}


def _scratch_env(tmp_path):
    data = tmp_path / "data" / "tables"
    data.parent.mkdir(exist_ok=True)
    shutil.copytree(DATA_DIR, data)
    state = tmp_path / "state"
    state.mkdir()
    return {
        "ROUTING_DATA_DIR": str(data),
        "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
        "ROUTING_NS": str(tmp_path / "ns"),
        "ROUTER_STATE_DIR": str(state),
    }


def _seed(env, timeout=600):
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_seed.py")],
        capture_output=True,
        text=True,
        env=dict(os.environ, **env),
        timeout=timeout,
    )
    assert p.returncode == 0, p.stderr[-800:]
    return p


def _jsonl(path):
    with open(path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _perf_map(data_dir):
    return {
        (r["model"], r["category"]): r
        for r in _jsonl(os.path.join(data_dir, "model_perf.jsonl"))
    }


# --------------------------------------------------------------------------
# RED control on the CURRENT seed: the stomp is real
# --------------------------------------------------------------------------


def _seed_fixtures_present(data_dir):
    """The committed inputs the fix needs (fail loud if data drifts)."""
    bench = _jsonl(os.path.join(data_dir, "benchmarks.jsonl"))
    for (model, cat), (key, _score) in MEASURED.items():
        rows = [
            b
            for b in bench
            if b["model"] == model
            and b["category"] == cat
            and key in str(b.get("source", ""))
        ]
        assert rows, (
            f"fixture drift: no committed bench row for {model}/{cat} via {key}"
        )
    qe = {
        json.loads(ln)["model"]: json.loads(ln)
        for ln in open(os.path.join(data_dir, "quality_estimates.jsonl"))
        if ln.strip()
    }
    for (model, cat), survey in SURVEY.items():
        assert (qe.get(model) or {}).get(cat) is not None, (
            f"fixture drift: no survey estimate for {model}/{cat}"
        )


def test_measured_probe_rows_win_over_survey_estimates(tmp_path):
    """After the seed, each measured cell keeps source='bench' + the live-probe
    source_ref and the MEASURED value — the survey estimate never lands there."""
    env = _scratch_env(tmp_path)
    data = env["ROUTING_DATA_DIR"]
    _seed_fixtures_present(data)
    _seed(env)
    perf = _perf_map(data)
    for (model, cat), (key, score) in MEASURED.items():
        row = perf.get((model, cat))
        assert row is not None, f"{model}/{cat}: measured cell missing from model_perf"
        assert row["source"] == "bench", (
            f"{model}/{cat}: source {row['source']!r} — measurement lost to an estimate"
        )
        assert f"bench:{key}" in str(row.get("source_ref", "")), (
            f"{model}/{cat}: source_ref {row.get('source_ref')!r} does not name the probe"
        )
        assert row["perf"] == pytest.approx(score), (
            f"{model}/{cat}: perf {row['perf']} != measured {score} (survey stomp?)"
        )


def test_survey_wins_only_where_the_bench_row_is_absent(tmp_path):
    """Attribution contrast (proves the mechanism, not just the outcome): the
    SAME seed on the SAME data except the GUARD-V2 bench row's source no longer
    matches an overlay key -> the cell falls back to the survey estimate (the
    pre-fix value). The only reason the committed-data run keeps the measurement
    is the bench row's measured evidence.

    model_perf.jsonl is a seed OUTPUT (never an input — the in-memory rebuild
    starts from models.jsonl + benchmarks.jsonl every run), so the pre-fix end
    state is simulated where it actually lives: the benchmarks table.
    """
    env = _scratch_env(tmp_path)
    data = env["ROUTING_DATA_DIR"]
    _seed_fixtures_present(data)
    # Scrub the GUARD-V2 key for the glm row in the scratch BENCH table: the
    # overlay no longer maps it, so no measured evidence reaches the cell.
    bench_path = os.path.join(data, "benchmarks.jsonl")
    model, cat = "glm-5.3-flash", "guard"
    key, _score = MEASURED[(model, cat)]
    rows = _jsonl(bench_path)
    n_scrubbed = 0
    for r in rows:
        if (r["model"], r["category"]) == (model, cat) and key in str(
            r.get("source", "")
        ):
            # Replace the key WHOLE: the overlay matches by substring
            # (`source LIKE '%key%'`), so merely suffixing it would still match.
            r["source"] = str(r["source"]).replace(key, "scrubbed-no-overlay-key")
            n_scrubbed += 1
    assert n_scrubbed >= 1, "control failed: no bench row scrubbed"
    with open(bench_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _seed(env)
    perf = _perf_map(data)
    row = perf[(model, cat)]
    # With the measurement gone the cell must NOT carry it. The fallback is the
    # models.jsonl perf_guard declared column (0.8 — non-degenerate, so the
    # estimate pass never fires): the pre-fix wash was declared 0.8 -> bench 0.0
    # (declaration-replaced) -> estimate 0.8 (the stomp), so the declared column
    # is exactly where the cell sits without the bench row.
    assert row["source"] != "bench" and "live-probe" not in str(
        row.get("source_ref", "")
    ), f"scrubbed control still carries the measurement: {row}"
    assert row["perf"] == pytest.approx(SURVEY[(model, cat)]), (
        f"scrubbed control should carry the declared 0.8, got {row}"
    )


def test_survey_estimates_still_fill_unmeasured_cells(tmp_path):
    """TR-002/TR-181 must keep working: cells with NO measured evidence still
    receive the survey estimate (guard/mock replace + insert paths intact)."""
    env = _scratch_env(tmp_path)
    data = env["ROUTING_DATA_DIR"]
    _seed_fixtures_present(data)
    _seed(env)
    perf = _perf_map(data)
    # glm-5.3-offpeak carries a survey guard estimate and NO GUARD-V2 row:
    # its cell stays estimate-sourced with the survey value.
    row = perf.get(("glm-5.3-offpeak", "guard"))
    assert row is not None, "survey gap-fill for an unmeasured lane vanished"
    assert row["source"] == "estimate" and row["source_ref"] == "QUALITY_ESTIMATES"
    assert row["perf"] == pytest.approx(
        SURVEY[("glm-5.3-flash", "guard")]
    )  # same survey value 0.8


def test_seed_run_is_idempotent_on_measured_cells(tmp_path):
    """Second seed over the same scratch data: the measured cells stay measured
    (no re-stomp on reseed — the original defect reproduced on every run)."""
    env = _scratch_env(tmp_path)
    data = env["ROUTING_DATA_DIR"]
    _seed_fixtures_present(data)
    _seed(env)
    _seed(env)
    perf = _perf_map(data)
    for (model, cat), (key, score) in MEASURED.items():
        row = perf[(model, cat)]
        assert row["source"] == "bench" and row["perf"] == pytest.approx(score)
