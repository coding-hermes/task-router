"""TR-232 — probe/third-party ranking scale parity.

Our probes were damped at ingest (score = 0.85 * passed/total) while
third-party and estimate sources reach 1.0, so model_tier quantiles were
computed over a MIXED pool where a full-marks probe (0.85) could never outrank
a 0.9 third-party benchmark row. Fix: probes are stored undamped at ingest and
the seed undamps the committed damped rows (score recovered from the source
text's passed/total ratio; the ratio-less full-marks rows read 1.0), so the
quantile scale is computed over one consistent unit.

Hermetic: every seed run targets a scratch copy of data/tables + scratch
registry/ns (the test_seed_ns_guard convention). No repo writes, no network.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DATA_DIR = os.path.join(REPO, "data", "tables")
PY = sys.executable

_HAS_DUCKDB = pytest.importorskip("duckdb") is not None

#: the damping multiplier the probes carried before TR-232.
PROBE_DAMP = 0.85
#: passed/total ratios parse out of the ingest source strings in two shapes:
#: "(field 3/4, n=1 ..." and "(4/4 deterministic checks, n=1 ...".
_RATIO = re.compile(r"\((?:[A-Za-z0-9_\-]+ )?(\d+)/(\d+)[,)]")
#: 2-decimal damped rows (09-25 batch) sit up to half a cent off 0.85*ratio.
_DAMP_TOL = 0.0051


def _scratch_env(tmp_path):
    data = tmp_path / "data" / "tables"
    data.parent.mkdir(exist_ok=True)
    shutil.copytree(DATA_DIR, data)
    state = tmp_path / "state"
    state.mkdir()
    return {"ROUTING_DATA_DIR": str(data),
            "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
            "ROUTING_NS": str(tmp_path / "ns"),
            "ROUTER_STATE_DIR": str(state)}


def _seed(env, timeout=600):
    p = subprocess.run([PY, os.path.join(SCRIPTS, "router_seed.py")],
                       capture_output=True, text=True, env=dict(os.environ, **env),
                       timeout=timeout)
    assert p.returncode == 0, p.stderr[-800:]
    return p


def _jsonl(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _probe_perf_rows(data_dir):
    """model_perf rows carried from probe evidence (overlay + bridge)."""
    return [r for r in _jsonl(os.path.join(data_dir, "model_perf.jsonl"))
            if r.get("source") == "bench" and "live-probe" in str(r.get("source_ref", ""))]


# --------------------------------------------------------------------------
# 1. the ingester stores the raw fraction (no 0.85 multiplier)
# --------------------------------------------------------------------------

def test_probe_ingest_emits_undamped_scores(tmp_path):
    """A 4/4 agentic result and a 3/3 v5 result land as 1.0, not 0.85.

    The ingester is the FIRST damping site: rows it appends feed the seed's
    model_perf on the next reseed, so the cap has to go where the number is
    born."""
    env = _scratch_env(tmp_path)
    data = env["ROUTING_DATA_DIR"]
    results = tmp_path / "results.json"
    results.write_text(json.dumps([
        {"model": "prov/model-a", "agent_tick": {"passed": 4, "total": 4}},
        {"model": "prov/model-b", "T1-TOOL": 3, "T2-CODE": 1},
    ]))
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_probe_ingest.py"),
         "--results", str(results), "--battery", "v5", "--date", "2026-09-28",
         "--commit"],
        capture_output=True, text=True, env=dict(os.environ, ROUTING_DATA_DIR=data),
        timeout=120)
    assert p.returncode == 0, p.stderr[-500:]
    # v5 rows: T1-TOOL 3/3 -> 1.0 (damped would read 0.85); T2-CODE 1/3 -> 0.333
    rows = {(r["model"], r["category"]): r for r in _jsonl(os.path.join(data, "benchmarks.jsonl"))
            if str(r.get("source", "")).startswith("live-probe-2026-09-28")}
    assert rows[("prov/model-b", "tool_use")]["score"] == pytest.approx(1.0)
    assert rows[("prov/model-b", "code_gen")]["score"] == pytest.approx(0.333)
    # agentic battery on the same file: dict shape, 4/4 -> 1.0
    p = subprocess.run(
        [PY, os.path.join(SCRIPTS, "router_probe_ingest.py"),
         "--results", str(results), "--battery", "agentic", "--date", "2026-09-28",
         "--commit"],
        capture_output=True, text=True, env=dict(os.environ, ROUTING_DATA_DIR=data),
        timeout=120)
    assert p.returncode == 0, p.stderr[-500:]
    rows = {(r["model"], r["category"]): r for r in _jsonl(os.path.join(data, "benchmarks.jsonl"))
            if str(r.get("source", "")).startswith("live-probe-2026-09-28")}
    assert rows[("prov/model-a", "agent_tick")]["score"] == pytest.approx(1.0)


def test_probe_ingest_rerun_is_skipped(tmp_path):
    """Re-running the same battery cannot re-stamp (the idempotency contract
    the ingest docstring promises)."""
    env = _scratch_env(tmp_path)
    data = env["ROUTING_DATA_DIR"]
    results = tmp_path / "results.json"
    results.write_text(json.dumps([
        {"model": "prov/model-a", "agent_tick": {"passed": 4, "total": 4}},
    ]))
    for _ in range(2):
        p = subprocess.run(
            [PY, os.path.join(SCRIPTS, "router_probe_ingest.py"),
             "--results", str(results), "--battery", "agentic", "--date", "2026-09-28",
             "--commit"],
            capture_output=True, text=True, env=dict(os.environ, ROUTING_DATA_DIR=data),
            timeout=120)
        assert p.returncode == 0, p.stderr[-500:]
    rows = [r for r in _jsonl(os.path.join(data, "benchmarks.jsonl"))
            if r["model"] == "prov/model-a"]
    assert len(rows) == 1, f"rerun re-stamped: {len(rows)} rows"


# --------------------------------------------------------------------------
# 2. the seed undamps the committed damped probe rows
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_seed_undamps_committed_probe_rows(tmp_path):
    """Probe-derived model_perf rows reach above 0.85 after a seed.

    The committed benchmarks carry 1150 damped probe rows (max score exactly
    0.85); today every probe-derived perf sits at or below that ceiling while
    third-party rows reach 1.0 — the mixed-pool bias TR-232 files."""
    env = _scratch_env(tmp_path)
    _seed(env)
    perfs = _probe_perf_rows(env["ROUTING_DATA_DIR"])
    assert perfs, "no probe-derived perf rows after seed"
    top = max(r["perf"] for r in perfs)
    assert top > 0.85, (
        f"probe-derived perf ceiling still {top} — probes are damped while "
        "third-party rows reach 1.0 (TR-232 mixed-scale bias)")


@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_seed_probe_undamp_is_idempotent(tmp_path):
    """The second seed on an already-undamped tree changes nothing.

    score := p/t parsed from the source text is a fixed point; the undamp must
    not re-divide (or shrink) scores on every reseed."""
    env = _scratch_env(tmp_path)
    _seed(env)
    data = env["ROUTING_DATA_DIR"]
    first = {f: _jsonl(os.path.join(data, f)) for f in
             ("benchmarks.jsonl", "model_perf.jsonl", "model_tier.jsonl")}
    _seed(env)
    second = {f: _jsonl(os.path.join(data, f)) for f in
              ("benchmarks.jsonl", "model_perf.jsonl", "model_tier.jsonl")}
    assert first == second, "second seed moved probe rows — undamp not idempotent"


# --------------------------------------------------------------------------
# 3. the ranking outcome the brief names
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_DUCKDB, reason="duckdb not importable")
def test_probe_aced_model_outranks_third_party(tmp_path):
    """A model that aced our probe 4/4 outranks a 0.9 third-party benchmark.

    The exact TR-232 scenario, on a two-model scratch category: model-a's
    committed probe row is the DAMPED 0.85 shape (as stored today), model-b
    carries a 0.9 ExploitBench row. After the seed undamps, model-a's perf is
    1.0 and its tier is strictly above model-b's."""
    env = _scratch_env(tmp_path)
    data = env["ROUTING_DATA_DIR"]
    # two synthetic live lanes (missing keys load as NULL; archive=false +
    # valid_to NULL keep them live for the estimate/overlay passes)
    models_path = os.path.join(data, "models.jsonl")
    with open(models_path, "a") as f:
        f.write(json.dumps({"provider": "prov", "model": "tr232-probe-ace",
                            "archive": False}) + "\n")
        f.write(json.dumps({"provider": "prov", "model": "tr232-bench-090",
                            "archive": False}) + "\n")
    with open(os.path.join(data, "benchmarks.jsonl"), "a") as f:
        # the damped 4/4 probe row shape as committed today (0.85 * 4/4)
        f.write(json.dumps({
            "model": "tr232-probe-ace", "category": "test", "score": 0.85,
            "max_score": 1.0, "valid_from": "2026-09-27",
            "source": "live-probe-2026-09-27/TEST: agentic deterministic battery "
                      "(test 4/4, n=1 small-probe, NOT large-bench)"}) + "\n")
        f.write(json.dumps({
            "model": "tr232-bench-090", "category": "test", "score": 0.9,
            "max_score": 1.0, "valid_from": "2026-09-27",
            "source": "ExploitBench research-bench-2026-09-27"}) + "\n")
    _seed(env)
    perfs = {r["model"]: r for r in _jsonl(os.path.join(data, "model_perf.jsonl"))
             if r["category"] == "test"}
    ace, bench = perfs.get("tr232-probe-ace"), perfs.get("tr232-bench-090")
    assert ace and bench, f"missing perf rows: {sorted(perfs)}"
    assert ace["perf"] == pytest.approx(1.0), (
        f"probe-aced model perf {ace['perf']} — damping survived the seed")
    tiers = {r["model"]: r["tier"]
             for r in _jsonl(os.path.join(data, "model_tier.jsonl"))
             if r["category"] == "test"}
    assert tiers.get("tr232-probe-ace", -99) > tiers.get("tr232-bench-090", -99), (
        f"tiers {tiers} — a full-marks probe still loses to a 0.9 third-party row")


# --------------------------------------------------------------------------
# 4. the committed data carries the undamped convention
# --------------------------------------------------------------------------

def _probe_ratio(src):
    """The passed/total ratio inside an ingest source string, or None.

    Same two-shape union as router_seed._PROBE_RATIO_PATTERNS (which cannot be
    imported — importing the seed runs it): '(field 3/4,' and
    '(4/4 (deterministic) checks'. test_seed_undamps_committed_probe_rows
    proves the two agree on the real corpus end to end."""
    for pat in (re.compile(r"\((?:[A-Za-z0-9_\-]+ )?(\d+)/(\d+)[,)]"),
                re.compile(r"\((\d+)/(\d+) (?:deterministic )?checks")):
        m = pat.search(src)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def test_committed_probe_rows_match_their_ratios():
    """Every committed probe row whose source text carries a passed/total
    ratio stores that ratio (not 0.85 * ratio). The damped grid is detectable:
    score within half a 2-decimal step of 0.85*ratio while off the raw ratio."""
    bad = []
    for r in _jsonl(os.path.join(DATA_DIR, "benchmarks.jsonl")):
        src = str(r.get("source", ""))
        if not src.startswith("live-probe"):
            continue
        ratio = _probe_ratio(src)
        if ratio is None:
            continue  # ratio-less rows (union-alpha shape) are checked below
        p, t = ratio
        score = r["score"]
        damped = abs(score - PROBE_DAMP * p / t) <= _DAMP_TOL
        raw = abs(score - p / t) <= _DAMP_TOL
        if damped and not raw:
            bad.append((r["model"], r["category"], score, f"{p}/{t}"))
    assert not bad, (
        f"{len(bad)} committed probe rows still sit on the damped 0.85 grid "
        f"(first: {bad[:3]}) — reseed to apply the TR-232 undamp")


def test_committed_ratioless_probe_rows_are_full_marks():
    """Ratio-less probe rows (the 09-16 free-text shape) must be full marks
    (1.0 undamped / 0.85 damped) — the only value the damping can have
    produced without a recorded ratio is the full-marks preimage."""
    bad = []
    for r in _jsonl(os.path.join(DATA_DIR, "benchmarks.jsonl")):
        src = str(r.get("source", ""))
        if not src.startswith("live-probe") or _probe_ratio(src):
            continue
        if r["score"] not in (0.85, 1.0):
            bad.append((r["model"], r["category"], r["score"]))
    assert not bad, (
        f"ratio-less probe rows off the full-marks grid: {bad[:5]} — "
        "their undamped value is not recoverable, file a gap")
