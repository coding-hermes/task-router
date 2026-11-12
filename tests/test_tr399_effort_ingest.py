"""TR-399 — the measurement pipeline records the effort it ran at.

A benchmark result without an effort stamp is INCOMPLETE: the row cannot be
paired with the cost of the effort actually billed, and silently defaulting it
is how the effort axis rots back to a single effort. Proven here:

  (a) a probe run at two efforts produces two perf rows with DISTINCT efforts
      (the ingest dedupes on (model, category, source key, effort), so the
      second effort is never swallowed as "already present");
  (b) the ingest REFUSES a results row that carries no effort when no
      --effort was given — exit 2, nothing written;
  (c) --efforts is an allowlist: a row stamped outside it is refused;
  (d) the probe runner stamps reasoning_effort on every result row it writes,
      and --efforts re-runs the battery once per effort with per-effort tags;
  (e) the pre-effort-era migration is documented in model_notes with an
      explicit reason (never an unexplained NULL).

The ingest is exercised as a SUBPROCESS against a scratch ROUTING_DATA_DIR —
never the live registry (same doctrine as test_tr398_effort_axis.py).
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
INGEST = REPO / "scripts" / "router_probe_ingest.py"
PROBE_RUN = REPO / "scripts" / "router_probe_run.py"
SCRIPTS = REPO / "scripts"
PY = sys.executable
sys.path.insert(0, str(SCRIPTS))


def _scratch(tmp_path):
    """A minimal data plane: the ingest appends to benchmarks/probe_gaps."""
    (tmp_path / "benchmarks.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "probe_gaps.jsonl").write_text("", encoding="utf-8")
    return tmp_path


def _results(tmp_path, rows):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return p


def _ingest(tmp_path, results, *extra):
    env = dict(os.environ)
    env["ROUTING_DATA_DIR"] = str(tmp_path)
    return subprocess.run(
        [PY, str(INGEST), "--results", str(results), "--battery", "agentic",
         "--date", "2026-11-11", "--commit", *extra],
        capture_output=True, text=True, env=env, cwd=str(REPO),
    )


def _bench(tmp_path):
    return [json.loads(l) for l in
            (tmp_path / "benchmarks.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]


# ---------------------------------------------------------------------------
# (a) two efforts -> two rows, never collapsed
# ---------------------------------------------------------------------------
def test_two_efforts_produce_two_rows_with_distinct_efforts(tmp_path):
    _scratch(tmp_path)
    rows = [
        {"provider": "p", "model": "m1", "reasoning_effort": "low",
         "agent_tick": {"passed": 2, "total": 4}},
        {"provider": "p", "model": "m1", "reasoning_effort": "high",
         "agent_tick": {"passed": 4, "total": 4}},
    ]
    r = _ingest(tmp_path, _results(tmp_path, rows))
    assert r.returncode == 0, r.stderr
    got = _bench(tmp_path)
    assert len(got) == 2, got
    assert {g["reasoning_effort"] for g in got} == {"low", "high"}
    # the scores are the ones each effort actually measured — no relabelling
    by_eff = {g["reasoning_effort"]: g["score"] for g in got}
    assert by_eff == {"low": 0.5, "high": 1.0}


def test_same_effort_twice_is_still_idempotent(tmp_path):
    _scratch(tmp_path)
    rows = [{"provider": "p", "model": "m1", "reasoning_effort": "high",
             "agent_tick": {"passed": 4, "total": 4}}]
    assert _ingest(tmp_path, _results(tmp_path, rows)).returncode == 0
    assert _ingest(tmp_path, _results(tmp_path, rows)).returncode == 0
    assert len(_bench(tmp_path)) == 1


# ---------------------------------------------------------------------------
# (b) a row with no effort is REFUSED, not defaulted
# ---------------------------------------------------------------------------
def test_ingest_refuses_a_result_without_an_effort_stamp(tmp_path):
    _scratch(tmp_path)
    rows = [{"provider": "p", "model": "m1",
             "agent_tick": {"passed": 4, "total": 4}}]
    r = _ingest(tmp_path, _results(tmp_path, rows))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "REFUSED" in r.stderr
    assert "m1" in r.stderr
    assert _bench(tmp_path) == [], "a refused run must write nothing"


def test_run_level_effort_stamps_rows_that_lack_their_own(tmp_path):
    _scratch(tmp_path)
    rows = [{"provider": "p", "model": "m1",
             "agent_tick": {"passed": 4, "total": 4}}]
    r = _ingest(tmp_path, _results(tmp_path, rows), "--effort", "medium")
    assert r.returncode == 0, r.stderr
    got = _bench(tmp_path)
    assert [g["reasoning_effort"] for g in got] == ["medium"]


def test_provider_alias_is_normalized_on_ingest(tmp_path):
    _scratch(tmp_path)
    rows = [{"provider": "p", "model": "m1", "reasoning_effort": "xhigh",
             "agent_tick": {"passed": 4, "total": 4}}]
    assert _ingest(tmp_path, _results(tmp_path, rows)).returncode == 0
    assert [g["reasoning_effort"] for g in _bench(tmp_path)] == ["x-high"]


# ---------------------------------------------------------------------------
# (c) --efforts is an allowlist
# ---------------------------------------------------------------------------
def test_efforts_allowlist_refuses_a_row_outside_it(tmp_path):
    _scratch(tmp_path)
    rows = [{"provider": "p", "model": "m1", "reasoning_effort": "max",
             "agent_tick": {"passed": 4, "total": 4}}]
    r = _ingest(tmp_path, _results(tmp_path, rows), "--efforts", "low,high")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "outside --efforts" in r.stderr
    assert _bench(tmp_path) == []


def test_efforts_allowlist_accepts_a_row_inside_it(tmp_path):
    _scratch(tmp_path)
    rows = [{"provider": "p", "model": "m1", "reasoning_effort": "high",
             "agent_tick": {"passed": 4, "total": 4}}]
    r = _ingest(tmp_path, _results(tmp_path, rows), "--efforts", "low,high")
    assert r.returncode == 0, r.stderr
    assert len(_bench(tmp_path)) == 1


# ---------------------------------------------------------------------------
# (d) the probe runner stamps the effort and can loop over efforts
# ---------------------------------------------------------------------------
def test_probe_runner_stamps_every_result_row():
    src = PROBE_RUN.read_text(encoding="utf-8")
    # every buffer row is created with the effort stamp
    for name in ("v5", "ag", "ext", "ext2", "ext3"):
        assert f"{name} = {{'provider': prov, 'model': model, 'reasoning_effort': effort}}" \
            in src, name
    assert "ap.add_argument('--effort'" in src
    assert "ap.add_argument('--efforts'" in src


def test_argv_helpers_replace_and_drop_flags():
    import importlib.util
    spec = importlib.util.spec_from_file_location("router_probe_run", PROBE_RUN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    argv = ["--providers", "p", "--efforts", "low,high", "--tag", "t"]
    assert mod._argv_without(argv, "--efforts") == ["--providers", "p", "--tag", "t"]
    assert mod._argv_without(["--efforts=low,high", "--x"], "--efforts") == ["--x"]
    # --effort must never eat --efforts (prefix collision) and vice versa
    assert mod._argv_set(["--providers", "p", "--effort", "low"], "--effort", "high") == \
        ["--providers", "p", "--effort", "high"]
    assert mod._argv_without(["--efforts", "low,high"], "--effort") == \
        ["--efforts", "low,high"]


def test_tier_probe_passes_the_effort_to_probe_and_ingest():
    src = (REPO / "scripts" / "router_tier_probe.py").read_text(encoding="utf-8")
    assert "cmd += ['--effort', _effort]" in src
    assert "'--effort', eff]" in src, "the ingest leg must carry the per-effort stamp"
    # a programmatic caller with a pre-TR-399 Namespace must not crash
    assert "getattr(args, 'effort', 'default')" in src


# ---------------------------------------------------------------------------
# (e) the pre-effort-era migration is documented, never an unexplained NULL
# ---------------------------------------------------------------------------
def test_pre_effort_era_migration_is_documented_in_model_notes():
    notes = [json.loads(l) for l in
             (REPO / "data" / "tables" / "model_notes.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    hits = [n for n in notes if "pre-effort-era" in json.dumps(n)]
    assert hits, "the single-effort imports must be migrated with a documented reason"
    assert any("TR-399" in json.dumps(n) for n in hits)


def test_every_committed_perf_and_bench_row_carries_an_effort():
    import capability_vocab
    for name in ("model_perf", "benchmarks"):
        path = REPO / "data" / "tables" / f"{name}.jsonl"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            assert capability_vocab.is_stored_reasoning_effort(
                row.get("reasoning_effort")), f"{name}.jsonl:{i} {row.get('model')}"
