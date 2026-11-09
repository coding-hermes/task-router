"""TR-398 — the reasoning-effort axis in capability storage.

The unit of capability becomes (provider, model, reasoning_effort). Proven
here:
  (a) the seed keeps two same-model/category rows with DIFFERENT efforts as
      two distinct tier entries — it never collapses across efforts;
  (b) the commit gate REFUSES a perf row missing reasoning_effort;
  (c) the commit gate REFUSES an effort outside the closed vocabulary (an
      un-normalized provider alias, a case-variant, an empty stamp) — and,
      per the owner's never-downgrade rule, ACCEPTS a verbatim unmapped
      provider name;
  (d) rows stamped 'default' behave exactly as before the axis existed
      (regression guard: same tiers, one entry, seeded views unchanged).

Seed tests run the REAL scripts/router_seed.py in a scratch environment
(ROUTING_REGISTRY / ROUTING_DATA_DIR / ROUTING_NS all pointed at tmp) — the
same doctrine as test_seed_ns_guard.py: never against the live registry or
the fleet mirror.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SEED = REPO / "scripts" / "router_seed.py"
GATE = REPO / "scripts" / "model_data_commit_gate.py"
SCRIPTS = REPO / "scripts"
PY = sys.executable
sys.path.insert(0, str(SCRIPTS))

try:
    import duckdb  # noqa: F401
    _HAS_DUCKDB = True
except ImportError:
    _HAS_DUCKDB = False


def _gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("model_data_commit_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = str(REPO)
    return mod


G = _gate()


# ---------------------------------------------------------------------------
# the vocabulary itself (capability_vocab is the ONE place)
# ---------------------------------------------------------------------------
def test_vocabulary_is_the_owner_corrected_set_in_comparison_order():
    import capability_vocab
    assert capability_vocab.REASONING_EFFORTS == (
        'default', 'off', 'low', 'medium', 'high', 'x-high', 'max')
    # ranked levels compare; 'default' never ranks as a level
    ranks = capability_vocab.EFFORT_RANK
    assert 'default' not in ranks
    ordered = sorted(ranks, key=ranks.get)
    assert ordered == ['off', 'low', 'medium', 'high', 'x-high', 'max']


def test_aliases_never_downgrade_a_level():
    import capability_vocab
    n = capability_vocab.normalize_reasoning_effort
    assert n('minimal') == ('low', 'alias')
    assert n('xhigh') == ('x-high', 'alias')      # NOT 'high' — first-class level
    assert n('max-thinking') == ('max', 'alias')
    # an unmapped provider name is stored VERBATIM, never guessed down
    assert n('ultra') == ('ultra', 'verbatim')
    # a case-variant of a canonical spelling must NOT be stored as-is
    assert capability_vocab.is_stored_reasoning_effort('X-HIGH') is False
    assert capability_vocab.is_stored_reasoning_effort('x-high') is True
    assert capability_vocab.is_stored_reasoning_effort('xhigh') is False
    assert capability_vocab.is_stored_reasoning_effort('ultra') is True
    assert capability_vocab.is_stored_reasoning_effort('') is False
    assert capability_vocab.is_stored_reasoning_effort(None) is False


# ---------------------------------------------------------------------------
# (a) + (d): the real seed over a scratch corpus
# ---------------------------------------------------------------------------
def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _synthetic_data(tmp_path, bench_rows):
    """A minimal self-contained data dir (every BASE_COLUMNS table non-empty —
    the seed falls back to the ns mirror on an empty file and would crash)."""
    data = tmp_path / "data" / "tables"
    _write_jsonl(data / "providers.jsonl", [{"id": "prov-a"}])
    _write_jsonl(data / "models.jsonl", [
        {"provider": "prov-a", "model": "test-model", "normalized_price": 1.0,
         "token_factor": 1.0, "plan_tier": 1,
         "archive": False, "disabled": False}])
    _write_jsonl(data / "benchmarks.jsonl", bench_rows)
    _write_jsonl(data / "archetypes.jsonl", [{"id": "generic", "bar": 0.5}])
    _write_jsonl(data / "projects.jsonl", [{"id": "p1"}])
    _write_jsonl(data / "task_profiles.jsonl", [
        {"id": "P0_FORE", "title": "t", "created_at": "2026-01-01 00:00:00",
         "version": 1, "tag": "P0_FORE"}])
    _write_jsonl(data / "task_profile_requirements.jsonl", [
        {"task_id": "P0_FORE", "category": "code_gen", "level": -5}])
    return data


def _run_seed(tmp_path, bench_rows):
    data = _synthetic_data(tmp_path, bench_rows)
    env = dict(os.environ)
    env.update({
        "ROUTING_REGISTRY": str(tmp_path / "registry.json"),
        "ROUTING_DATA_DIR": str(data),
        "ROUTING_NS": str(tmp_path / "ns"),
        "ROUTING_ALLOW_UNSATISFIABLE": "1",
    })
    p = subprocess.run([PY, str(SEED)], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=240)
    return p, data


def _read(path):
    return [json.loads(l) for l in open(path) if l.strip()]


@pytest.mark.skipif(not _HAS_DUCKDB, reason="seed needs duckdb")
def test_seed_keeps_two_efforts_as_two_distinct_tiers(tmp_path):
    """(a): same model+category, different efforts -> two perf rows, two tiers."""
    p, data = _run_seed(tmp_path, [
        {"model": "test-model", "category": "code_gen", "score": 0.60,
         "max_score": 1.0, "source": "research-bench-tr398",
         "valid_from": "2026-11-01", "reasoning_effort": "default"},
        {"model": "test-model", "category": "code_gen", "score": 0.90,
         "max_score": 1.0, "source": "research-bench-tr398",
         "valid_from": "2026-11-01", "reasoning_effort": "high"},
    ])
    assert p.returncode == 0, p.stderr + p.stdout

    perf = [r for r in _read(data / "model_perf.jsonl")
            if r["model"] == "test-model" and r["category"] == "code_gen"]
    by_effort = {r["reasoning_effort"]: r["perf"] for r in perf}
    assert by_effort == {"default": 0.6, "high": 0.9}, (
        "the seed must NOT collapse two efforts into one perf row")

    tiers = [r for r in _read(data / "model_tier.jsonl")
             if r["model"] == "test-model" and r["category"] == "code_gen"]
    tier_by_effort = {r["reasoning_effort"]: r["tier"] for r in tiers}
    assert len(tiers) == 2 and set(tier_by_effort) == {"default", "high"}, (
        "the seed must NOT collapse two efforts into one tier entry")
    # the two tiers carry their own effort's percentile scale
    assert tier_by_effort["default"] == tier_by_effort["high"] == 5  # single-value pools

    levels = [r for r in _read(data / "category_levels.jsonl")
              if r["category"] == "code_gen"]
    assert {r["reasoning_effort"] for r in levels} == {"default", "high"}


@pytest.mark.skipif(not _HAS_DUCKDB, reason="seed needs duckdb")
def test_default_effort_rows_seed_exactly_as_before(tmp_path):
    """(d): a 'default'-stamped corpus behaves exactly as pre-axis seeds did —
    one perf row, one tier row, the same tier value the old quantile math
    produced (a single-value pool reads tier 5 on the -5..+5 scale)."""
    p, data = _run_seed(tmp_path, [
        {"model": "test-model", "category": "code_gen", "score": 0.72,
         "max_score": 1.0, "source": "research-bench-tr398",
         "valid_from": "2026-11-01", "reasoning_effort": "default"},
    ])
    assert p.returncode == 0, p.stderr + p.stdout

    perf = [r for r in _read(data / "model_perf.jsonl")
            if r["model"] == "test-model"]
    assert len(perf) == 1
    assert perf[0]["reasoning_effort"] == "default"
    assert perf[0]["perf"] == 0.72
    assert perf[0]["source"] == "bench"

    tiers = [r for r in _read(data / "model_tier.jsonl")
             if r["model"] == "test-model"]
    assert len(tiers) == 1 and tiers[0]["tier"] == 5
    assert tiers[0]["reasoning_effort"] == "default"


def test_committed_corpus_is_all_default_so_pinned_expectations_hold():
    """The backfilled tree stays all-'default' — every pre-existing pinned
    expectation (tiers, category_levels, resolver behavior) is unchanged."""
    for name in ("model_perf", "benchmarks", "model_tier"):
        path = REPO / "data" / "tables" / f"{name}.jsonl"
        rows = _read(path)
        assert rows, name
        efforts = {r.get("reasoning_effort") for r in rows}
        assert efforts == {"default"}, (name, list(efforts)[:5])


# ---------------------------------------------------------------------------
# (b) + (c): the commit gate refuses incomplete / out-of-vocab effort stamps
# ---------------------------------------------------------------------------
def _git(repo, *args):
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
    })
    p = subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True,
                       text=True, env=env)
    assert p.returncode == 0, p.stderr
    return p


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    perf = [{"model": "known", "category": "code_gen", "perf": 0.7,
             "source": "bench", "source_ref": "bench:x",
             "reasoning_effort": "default"}]
    models = [{"provider": "acme", "model": "known", "normalized_price": 1.0,
               "perf_code_gen": 0.7}]
    _write_jsonl(repo / "data/tables/model_perf.jsonl", perf)
    _write_jsonl(repo / "data/tables/benchmarks.jsonl", [])
    _write_jsonl(repo / "data/tables/model_tier.jsonl", [])
    _write_jsonl(repo / "data/tables/models.jsonl", models)
    _write_jsonl(repo / "data/tables/model_notes.jsonl", [])
    _write_jsonl(repo / "data/null_reasons.jsonl", [])
    _write_jsonl(repo / "data/state/usability-baseline.json", {"defects": 0})
    (repo / "README.md").write_text("fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run_gate(repo):
    env = dict(os.environ)
    env.pop("ALLOW_UNUSABLE_DATA", None)
    return subprocess.run([PY, str(GATE), "--staged", "--repo", str(repo)],
                          capture_output=True, text=True, env=env, timeout=60)


def test_gate_refuses_a_perf_row_missing_reasoning_effort(tmp_path):
    """(b) TR-399 made real: an effort-less measurement cannot be committed."""
    repo = _make_repo(tmp_path)
    _write_jsonl(repo / "data/tables/model_perf.jsonl",
                 [{"model": "known", "category": "debug", "perf": 0.55,
                   "source": "bench", "source_ref": "bench:x"}])
    _git(repo, "add", "data/tables/model_perf.jsonl")
    p = _run_gate(repo)
    assert p.returncode == 1, "gate must refuse; got\n%s%s" % (p.stdout, p.stderr)
    assert "reasoning_effort is MISSING" in p.stderr
    assert "effort stamp (TR-398/TR-399)" in p.stderr


def test_gate_refuses_an_out_of_vocab_effort(tmp_path):
    """(c) un-normalized alias, case-variant, empty — all refused; the alias
    message names the TARGET so the fix never downgrades the level."""
    repo = _make_repo(tmp_path)
    _write_jsonl(repo / "data/tables/model_perf.jsonl", [
        {"model": "known", "category": "debug", "perf": 0.55,
         "source": "bench", "source_ref": "bench:x", "reasoning_effort": "xhigh"},
        {"model": "known", "category": "review", "perf": 0.55,
         "source": "bench", "source_ref": "bench:x", "reasoning_effort": "X-HIGH"},
        {"model": "known", "category": "schema", "perf": 0.55,
         "source": "bench", "source_ref": "bench:x", "reasoning_effort": ""},
    ])
    _git(repo, "add", "data/tables/model_perf.jsonl")
    p = _run_gate(repo)
    assert p.returncode == 1, p.stderr
    assert "un-normalized provider alias" in p.stderr and "'x-high'" in p.stderr
    assert "case-variant" in p.stderr
    assert "MISSING" in p.stderr


def test_gate_refuses_an_effortless_benchmark_row(tmp_path):
    """benchmarks.jsonl is the ingest target and is gated with the same rule."""
    repo = _make_repo(tmp_path)
    _write_jsonl(repo / "data/tables/benchmarks.jsonl",
                 [{"model": "known", "category": "code_gen", "score": 0.8,
                   "max_score": 1.0, "source": "research-bench-tr398",
                   "valid_from": "2026-11-01"}])
    _git(repo, "add", "data/tables/benchmarks.jsonl")
    p = _run_gate(repo)
    assert p.returncode == 1, p.stderr
    assert "benchmarks.jsonl" in p.stderr


def test_gate_accepts_canonical_verbatim_and_first_class_levels(tmp_path):
    repo = _make_repo(tmp_path)
    _write_jsonl(repo / "data/tables/benchmarks.jsonl", [
        {"model": "known", "category": "code_gen", "score": 0.8,
         "max_score": 1.0, "source": "research-bench-tr398",
         "valid_from": "2026-11-01", "reasoning_effort": "x-high"},
        {"model": "known", "category": "debug", "score": 0.8,
         "max_score": 1.0, "source": "research-bench-tr398",
         "valid_from": "2026-11-01", "reasoning_effort": "max"},
        {"model": "known", "category": "review", "score": 0.8,
         "max_score": 1.0, "source": "research-bench-tr398",
         "valid_from": "2026-11-01", "reasoning_effort": "ultra"},  # verbatim
    ])
    _git(repo, "add", "data/tables/benchmarks.jsonl")
    p = _run_gate(repo)
    assert p.returncode == 0, "never-downgrade rule: verbatim names pass\n%s" % p.stderr
