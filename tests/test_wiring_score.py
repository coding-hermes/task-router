"""TR-282 — the wiring score consumer: contract tests.

Pins scripts/wiring_score.py: edge parsing (incl. torn-line tolerance),
scoring formula, scope + classification, baseline deltas, JSON/human
shapes, honest unmeasurability, and the anti-vacuity law — a deliberately
unwired fixture scores BELOW a deliberately-wired one, both recorded.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "wiring_score.py"
QUALITY_SCRIPT = REPO_ROOT / "scripts" / "quality_artifact.py"

spec = importlib.util.spec_from_file_location("wiring_score", SCRIPT)
wiring_score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wiring_score)


def run_score(*args, cwd=REPO_ROOT):
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(cwd),
    )
    return proc


@pytest.fixture()
def fixture_repo(tmp_path):
    """Tiny deterministic repo: 3 surfaces, 1 tested+wired, 1 unwired."""
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "wired.py").write_text("import os\n")
    (tmp_path / "scripts" / "core.py").write_text("import os\n")
    (tmp_path / "scripts" / "unwired.py").write_text("import os\n")
    (tmp_path / "tests" / "test_wired.py").write_text(
        "import scripts.wired\n")
    graph = tmp_path / ".vfs" / "graph"
    graph.mkdir(parents=True)
    (graph / "edges.jsonl").write_text(FIXTURE_EDGES)
    return tmp_path


# ---------------------------------------------------------------------------
# Edge-store parsing.
# ---------------------------------------------------------------------------

class TestEdgeParsing:
    def test_load_edges_parses_valid_lines(self, tmp_path):
        store = tmp_path / "edges.jsonl"
        store.write_text(
            '{"from":"a.py","to":"b.py","rel":"imports"}\n'
            '{"from":"b.py","to":"pkg:os","rel":"imports"}\n')
        edges, skipped = wiring_score.load_edges(store)
        assert len(edges) == 2
        assert skipped == 0
        assert edges[0]["from"] == "a.py"

    def test_load_edges_skips_torn_and_blank_lines(self, tmp_path):
        store = tmp_path / "edges.jsonl"
        store.write_text(
            '{"from":"a.py","to":"b.py"}\n'
            "\n"
            "{torn line not json\n"
            '{"only":"one-key"}\n'
            '["not","an","object"]\n')
        edges, skipped = wiring_score.load_edges(store)
        assert len(edges) == 1
        assert skipped == 3

    def test_load_edges_empty_store(self, tmp_path):
        store = tmp_path / "edges.jsonl"
        store.write_text("")
        edges, skipped = wiring_score.load_edges(store)
        assert edges == []
        assert skipped == 0


# ---------------------------------------------------------------------------
# Scoring formula.
# ---------------------------------------------------------------------------

class TestScoringFormula:
    def _stats(self):
        return {
            "wired.py": {"inbound": 2, "outbound": 1, "external_outbound": 0,
                         "test_edge": True},
            "called_only.py": {"inbound": 1, "outbound": 0,
                               "external_outbound": 0, "test_edge": False},
            "tested_only.py": {"inbound": 0, "outbound": 1,
                               "external_outbound": 0, "test_edge": True},
            "unwired.py": {"inbound": 0, "outbound": 0,
                           "external_outbound": 0, "test_edge": False},
        }

    def test_formula_is_mechanical_and_recorded(self):
        stats = self._stats()
        rollup = wiring_score.score_surfaces(stats)
        # called 2/4, tested 2/4 -> 100*(0.5*0.5 + 0.5*0.5) = 50.0
        assert rollup["score"] == pytest.approx(50.0)
        assert rollup["called_share"] == pytest.approx(0.5)
        assert rollup["tested_share"] == pytest.approx(0.5)
        assert rollup["wired_share"] == pytest.approx(0.25)
        assert rollup["surfaces_total"] == 4

    def test_formula_constant_and_formula_string_carried(self):
        assert wiring_score.W_CALL == 0.5
        assert wiring_score.W_TEST == 0.5
        assert "0.5*called_share" in wiring_score.FORMULA
        assert "0.5*tested_share" in wiring_score.FORMULA

    def test_perfectly_wired_scores_100(self):
        stats = {
            "a.py": {"inbound": 3, "outbound": 1, "external_outbound": 0,
                     "test_edge": True},
            "b.py": {"inbound": 1, "outbound": 0, "external_outbound": 0,
                     "test_edge": True},
        }
        assert wiring_score.score_surfaces(stats)["score"] == 100.0

    def test_nothing_wired_scores_0(self):
        stats = {
            "a.py": {"inbound": 0, "outbound": 0, "external_outbound": 0,
                     "test_edge": False},
        }
        assert wiring_score.score_surfaces(stats)["score"] == 0.0

    def test_test_files_are_never_scoring_subjects(self):
        stats = {
            "app.py": {"inbound": 0, "outbound": 1, "external_outbound": 0,
                       "test_edge": True},
            "tests/test_app.py": {"inbound": 1, "outbound": 0,
                                  "external_outbound": 0, "test_edge": False},
        }
        rollup = wiring_score.score_surfaces(stats)
        assert rollup["surfaces_total"] == 1
        assert rollup["tested_share"] == 1.0


# ---------------------------------------------------------------------------
# Scope + classification.
# ---------------------------------------------------------------------------

class TestScopeAndClassification:
    def test_scope_prefix_filters_surfaces(self, tmp_path):
        (tmp_path / "scripts").mkdir(parents=True)
        (tmp_path / "scripts" / "a.py").write_text("import os\n")
        (tmp_path / "lib").mkdir(parents=True)
        (tmp_path / "lib" / "b.py").write_text("import os\n")
        graph = tmp_path / ".vfs" / "graph"
        graph.mkdir(parents=True)
        (graph / "edges.jsonl").write_text(
            '{"from":"scripts/a.py","to":"lib/b.py"}\n')
        report = wiring_score.measure(root=tmp_path, scope="scripts")
        names = {s["module"] for s in report["surfaces"]}
        assert names == {"scripts/a.py"}
        # the cross-scope edge counts as outbound evidence for the surface
        assert report["surfaces"][0]["outbound"] == 1

    def test_classification_ladder(self):
        classify = wiring_score.classify
        plain = {"inbound": 0, "outbound": 0, "external_outbound": 0,
                 "test_edge": False}
        assert classify("tests/test_x.py", plain) == "test"
        assert classify("x.py", dict(plain)) == "dead"
        assert classify("x.py", dict(plain, inbound=0, outbound=1)) == "entrypoint"
        assert classify("x.py", dict(plain, inbound=1, outbound=1)) == "lib"
        assert classify("x.py", dict(plain, inbound=1, outbound=0)) == "service"

    def test_is_test_path_variants(self):
        assert wiring_score.is_test_path("tests/test_a.py")
        assert wiring_score.is_test_path("scripts/test_a.py")
        assert wiring_score.is_test_path("scripts/a_test.py")
        assert not wiring_score.is_test_path("scripts/a.py")

    def test_untested_surface_list_excludes_test_files(self):
        stats = {
            "app.py": {"inbound": 1, "outbound": 0, "external_outbound": 0,
                       "test_edge": False},
            "tests/test_app.py": {"inbound": 1, "outbound": 0,
                                  "external_outbound": 0, "test_edge": False},
        }
        untested = [f for f, s in stats.items()
                    if not wiring_score.is_test_path(f) and not s["test_edge"]]
        assert untested == ["app.py"]


# ---------------------------------------------------------------------------
# Baseline deltas.
# ---------------------------------------------------------------------------

class TestBaselineDeltas:
    def test_delta_counts_new_inbound_edge(self, tmp_path):
        base = tmp_path / "base.jsonl"
        base.write_text('{"from":"lib.py","to":"app.py"}\n')
        now = tmp_path / "now.jsonl"
        now.write_text(
            '{"from":"lib.py","to":"app.py"}\n'
            '{"from":"new.py","to":"app.py"}\n')
        edges_now, _ = wiring_score.load_edges(now)
        edges_base, _ = wiring_score.load_edges(base)
        repo_files = ["app.py", "lib.py", "new.py"]
        stats = wiring_score.build_surfaces(edges_now, repo_files, None)
        deltas = wiring_score.baseline_deltas(stats, edges_base)
        assert deltas["app.py"]["inbound"] == 1
        assert deltas["lib.py"]["inbound"] == 0
        assert deltas["new.py"]["inbound"] == 0  # new.py not in baseline

    def test_delta_is_negative_when_edge_dropped(self, tmp_path):
        base = tmp_path / "base.jsonl"
        base.write_text('{"from":"lib.py","to":"app.py"}\n')
        now = tmp_path / "now.jsonl"
        now.write_text("")
        edges_now, _ = wiring_score.load_edges(now)
        edges_base, _ = wiring_score.load_edges(base)
        stats = wiring_score.build_surfaces(edges_now, ["app.py", "lib.py"], None)
        deltas = wiring_score.baseline_deltas(stats, edges_base)
        assert deltas["app.py"]["inbound"] == -1


# ---------------------------------------------------------------------------
# JSON + human shapes (CLI).
# ---------------------------------------------------------------------------

FIXTURE_EDGES = (
    '{"from":"scripts/wired.py","to":"scripts/core.py"}\n'
    '{"from":"tests/test_wired.py","to":"scripts/wired.py"}\n'
    '{"from":"scripts/core.py","to":"pkg:os"}\n'
)


class TestOutputShapes:
    def test_json_shape_is_complete(self, fixture_repo):
        proc = run_score("--json", "--root", str(fixture_repo))
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert "score" in data
        assert 0.0 <= data["score"] <= 100.0
        for key in ("formula", "source", "surfaces", "classes",
                    "untested_surfaces", "graph", "limitations"):
            assert key in data, f"missing {key}"
        assert "0.5*called_share" in data["formula"]
        for surf in data["surfaces"]:
            assert {"module", "class", "inbound", "outbound",
                    "test_edge"} <= set(surf)
        assert set(data["classes"]) == {"entrypoint", "service", "lib",
                                        "test", "dead"}

    def test_human_output_names_score_and_formula(self, fixture_repo):
        proc = run_score("--root", str(fixture_repo))
        assert proc.returncode == 0
        assert "wiring score" in proc.stdout
        assert "score:" in proc.stdout
        assert "0.5*called_share" in proc.stdout
        assert "limitation:" in proc.stdout

    def test_baseline_flag_adds_deltas(self, fixture_repo, tmp_path):
        base = tmp_path / "base.jsonl"
        base.write_text('{"from":"scripts/wired.py","to":"scripts/core.py"}\n')
        proc = run_score("--json", "--root", str(fixture_repo),
                         "--baseline", str(base))
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert "deltas" in data and "baseline" in data
        assert data["deltas"]["scripts/unwired.py"]["inbound"] == 0

    def test_probe_hilo_flag_tolerates_missing_hilo(self, fixture_repo):
        import os
        env_path = os.environ.get("PATH")
        proc = run_score("--json", "--root", str(fixture_repo),
                         "--probe-hilo")
        assert proc.returncode in (0, 3)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            assert "hilo_stats" in data


# ---------------------------------------------------------------------------
# Honest unmeasurability.
# ---------------------------------------------------------------------------

class TestUnmeasurable:
    def test_missing_graph_fails_loudly_not_silent_zero(self, tmp_path):
        proc = run_score("--json", "--root", str(tmp_path))
        assert proc.returncode == 3
        assert "WIRING-UNMEASURABLE" in proc.stderr
        data = json.loads(proc.stdout)
        assert data["error"] == "WIRING-UNMEASURABLE"
        assert "reason" in data and len(data["reason"]) > 0
        assert "hilo" in data["reason"]
        assert "score" not in data
        assert data["formula"] and data["source"]

    def test_degenerate_warm_over_many_files_flags_partial(self, tmp_path):
        """The known bad shape: a big repo whose warm produced ~1 edge."""
        scripts = tmp_path / "scripts"
        scripts.mkdir(parents=True)
        for i in range(25):
            (scripts / f"mod_{i:02d}.py").write_text("import os\n")
        graph = tmp_path / ".vfs" / "graph"
        graph.mkdir(parents=True)
        (graph / "edges.jsonl").write_text(
            '{"from":"scripts/mod_00.py","to":"pkg:os"}\n')
        proc = run_score("--json", "--root", str(tmp_path))
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert data["graph"]["graph_valid"] is False
        assert data["graph"]["repo_files_on_disk"] >= 20
        assert data["graph"]["edges"] < 10
        assert any("PARTIAL GRAPH" in lim for lim in data["limitations"])

    def test_valid_graph_carries_no_partial_flag(self, fixture_repo):
        proc = run_score("--json", "--root", str(fixture_repo))
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        # 4 files < MIN_FILES_FOR_VALIDITY: the degenerate check never arms
        # on a tiny repo, and this fixture's 3 edges are real coverage
        assert data["graph"]["graph_valid"] is True
        assert not any("PARTIAL GRAPH" in lim for lim in data["limitations"])


# ---------------------------------------------------------------------------
# ANTI-VACUITY: unwired fixture < wired fixture, both runs recorded.
# ---------------------------------------------------------------------------

class TestAntiVacuity:
    def _make_repo(self, root: Path, wired: bool):
        """A function module plus, when `wired`, a caller chain and tests.

        The caller chain's root lives OUTSIDE the scored scope (scripts/)
        because an entrypoint legitimately has no inbound caller — the
        scored scope pkg/ then contains only callable surfaces.
        """
        (root / "pkg").mkdir(parents=True)
        (root / "pkg" / "fn.py").write_text(
            "def compute(x):\n    return x + 1\n")
        if wired:
            (root / "pkg" / "caller.py").write_text(
                "import pkg.fn\n"
                "def run():\n    return pkg.fn.compute(1)\n")
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "run.py").write_text(
                "import pkg.caller\n"
                "def main():\n    return pkg.caller.run()\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_fn.py").write_text(
                "import pkg.fn\n"
                "def test_compute():\n    assert pkg.fn.compute(1) == 2\n")
            (root / "tests" / "test_caller.py").write_text(
                "import pkg.caller\n"
                "def test_run():\n    assert pkg.caller.run() == 2\n")

    def _score(self, wired: bool, tmp_path, results: dict):
        root = tmp_path / ("wired" if wired else "unwired")
        self._make_repo(root, wired)
        (root / ".vfs" / "graph").mkdir(parents=True)
        edges = root / ".vfs" / "graph" / "edges.jsonl"
        if wired:
            edges.write_text(
                '{"from":"pkg/caller.py","to":"pkg/fn.py"}\n'
                '{"from":"scripts/run.py","to":"pkg/caller.py"}\n'
                '{"from":"tests/test_fn.py","to":"pkg/fn.py"}\n'
                '{"from":"tests/test_caller.py","to":"pkg/caller.py"}\n')
        else:
            # the graph exists but knows nothing about pkg/fn.py: no caller,
            # no test — exactly the "deliberately-unwired" shape
            edges.write_text(
                '{"from":"pkg:os","to":"other/elsewhere.py"}\n')
        proc = run_score("--json", "--scope", "pkg", "--root", str(root))
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        results["wired" if wired else "unwired"] = data["score"]
        return data

    def test_unwired_fixture_scores_below_wired_fixture(self, tmp_path):
        results = {}
        unwired = self._score(False, tmp_path, results)
        wired = self._score(True, tmp_path, results)
        assert results["unwired"] < results["wired"], (
            f"anti-vacuity violated: unwired={results['unwired']} "
            f"wired={results['wired']}")
        # exact mechanics: unwired = 0.0 (no caller, no test),
        # wired = 100.0 (every scored surface called AND tested)
        assert unwired["score"] == 0.0
        assert wired["score"] == 100.0
        assert wired["called"] == wired["surfaces_total"]
        assert wired["tested"] == wired["surfaces_total"]

    def test_both_fixture_runs_recorded_in_json(self, tmp_path):
        results = {}
        self._score(False, tmp_path, results)
        self._score(True, tmp_path, results)
        assert set(results) == {"wired", "unwired"}
        assert isinstance(results["wired"], (int, float))
        assert isinstance(results["unwired"], (int, float))


# ---------------------------------------------------------------------------
# Artifact integration (TR-281 carrier).
# ---------------------------------------------------------------------------

def run_artifact(*args):
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(QUALITY_SCRIPT), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    return proc


class TestArtifactIntegration:
    def _artifact(self):
        proc = run_artifact("--json")
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def _wiring(self):
        return next(m for m in self._artifact()["metrics"]
                    if m["metric"] == "wiring_pct")

    def test_wiring_metric_measured_when_graph_available(self):
        graph = REPO_ROOT / ".vfs" / "graph" / "edges.jsonl"
        if not graph.exists():
            pytest.skip("this checkout has no warmed Hilo graph")
        wiring = self._wiring()
        assert wiring["value"] is not None
        assert 0.0 <= wiring["value"] <= 100.0
        assert "reason" not in wiring

    def test_wiring_null_carries_concrete_reason(self):
        wiring = self._wiring()
        if wiring["value"] is not None:
            pytest.skip("graph available in this checkout; null arm unexercised")
        assert isinstance(wiring["reason"], str) and wiring["reason"]
        assert "hilo" in wiring["reason"].lower() or "graph" in wiring["reason"].lower()

    def test_formula_is_carried_in_both_arms(self):
        wiring = self._wiring()
        assert wiring.get("formula")
        assert "0.5*called_share" in wiring["formula"]

    def test_source_is_carried_in_both_arms(self):
        wiring = self._wiring()
        assert wiring.get("source")
        assert "edges.jsonl" in wiring["source"]

    def test_definition_cmd_points_at_the_consumer(self):
        wiring = self._wiring()
        assert "scripts/wiring_score.py" in wiring["definition_cmd"]

    def test_type_hint_behavior_unchanged(self):
        artifact = self._artifact()
        th = next(m for m in artifact["metrics"]
                  if m["metric"] == "type_hint_pct")
        assert th["value"] is not None
        assert th["breakdown"], "type_hint keeps its per-module breakdown"
        assert th["stage"] == 1 and th["target"] == 40.0

    def test_coverage_null_unchanged(self):
        artifact = self._artifact()
        cov = next(m for m in artifact["metrics"]
                   if m["metric"] == "coverage_pct")
        assert cov["value"] is None
        assert "TR-187" in cov["reason"]

    def test_formulas_are_in_lockstep(self):
        """The artifact's formula string matches the instrument's."""
        import importlib
        proc = run_score("--json", "--root", str(REPO_ROOT))
        if proc.returncode != 0:
            pytest.skip("no graph in this checkout")
        data = json.loads(proc.stdout)
        wiring = self._wiring()
        assert data["formula"] == wiring["formula"]
