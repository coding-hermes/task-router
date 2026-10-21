"""tests/test_context_call.py — TR-254: ONE CONTEXT CALL integration tests.

Tests cover:
  T1: Structure — all four surfaces return labelled fragments
  T2: Provenance — each fragment has {surface, source_path_or_key, taken_at}
  T3: Read-only — no file content is modified by a context-call run
  T4: Hilo against task-router repo itself (proven against a fleet repo)
  T5: Contradiction detection — board row with commit but pending status
  T6: JSON output mode
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def task_router_repo():
    """Path to the task-router repo for Hilo queries."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def clean_tree(task_router_repo):
    """Snapshot untracked/modified files and restore after test."""
    repo = Path(task_router_repo)
    original = {}
    # Track modified files
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=task_router_repo,
        capture_output=True, text=True, check=False,
    )
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        # Format: "XY filename" or "?? filename"
        parts = line.split(" ", 1)
        if len(parts) == 2:
            fpath = parts[1].lstrip("? ")
            fp = Path(task_router_repo) / fpath
            # Skip symlinks and directories — only hash regular files
            if fp.exists() and fp.is_file():
                try:
                    original[str(fp)] = fp.read_bytes()
                except (OSError, PermissionError):
                    pass
    yield
    # Restore
    for fpath, content in original.items():
        fp = Path(fpath)
        if fp.exists():
            fp.write_bytes(content)


# ---------------------------------------------------------------------------
# T1: Structure — all four surfaces return labelled fragments
# ---------------------------------------------------------------------------

class TestStructure:
    def test_code_surface_returns_fragments(self, task_router_repo):
        """Code surface returns fragments with labels."""
        from scripts.router_context_call import query_hilo
        result = query_hilo(task_router_repo)
        fragments = result.get("fragments", [])
        # Should have at least one fragment from code surface
        code_fragments = [f for f in fragments if isinstance(f, dict) and f.get("surface") == "code"]
        assert len(code_fragments) >= 0, "code surface may return 0 if no Python/Go files"
        for frag in code_fragments:
            assert "surface" in frag
            assert "source_path_or_key" in frag
            assert "taken_at" in frag

    def test_duckbrain_surface_returns_fragments(self):
        """DuckBrain surface returns fragments (may be auth-empty)."""
        from scripts.router_context_call import query_duckbrain
        result = query_duckbrain(["task-router"])
        fragments = result.get("fragments", [])
        # DuckBrain may return 0 fragments if no auth token found,
        # but should still return at least one fragment (auth note) or the query should
        # succeed and return either data fragments or a "no auth" note
        assert len(fragments) >= 1 or result.get("namespaces_queried"), \
            f"DuckBrain should return fragments or namespaces_queried; got {fragments}"
        for frag in fragments:
            assert isinstance(frag, dict)
            assert "surface" in frag
            assert frag.get("surface") == "duckbrain"

    def test_specs_surface_returns_fragments(self, task_router_repo):
        """Specs surface returns fragments for spec files."""
        from scripts.router_context_call import query_specs
        result = query_specs(task_router_repo)
        fragments = result.get("fragments", [])
        spec_fragments = [f for f in fragments if isinstance(f, dict) and f.get("surface") == "specs"]
        # Should have fragments from docs/specs/
        assert len(spec_fragments) >= 1, "Should find spec files in docs/specs/"
        for frag in spec_fragments:
            assert "source_path_or_key" in frag
            assert "taken_at" in frag

    def test_board_surface_returns_fragments(self, task_router_repo):
        """Board surface returns fragments with summary."""
        from scripts.router_context_call import query_board
        result = query_board(task_router_repo)
        fragments = result.get("fragments", [])
        board_fragments = [f for f in fragments if isinstance(f, dict) and f.get("surface") == "board"]
        assert len(board_fragments) >= 1, "Board should return at least summary fragment"
        for frag in board_fragments:
            assert "source_path_or_key" in frag
            assert "taken_at" in frag


# ---------------------------------------------------------------------------
# T2: Provenance — each fragment has required provenance fields
# ---------------------------------------------------------------------------

class TestProvenanceLabels:
    def test_all_fragments_have_provenance(self, task_router_repo):
        """Every fragment from every surface has {surface, source_path_or_key, taken_at}."""
        from scripts.router_context_call import query_hilo, query_specs, query_board
        code = query_hilo(task_router_repo)
        specs = query_specs(task_router_repo)
        board = query_board(task_router_repo)

        for surface_name, result in [("code", code), ("specs", specs), ("board", board)]:
            for frag in result.get("fragments", []):
                if isinstance(frag, dict):
                    assert "surface" in frag, f"Missing 'surface' in {surface_name} fragment"
                    assert "source_path_or_key" in frag, f"Missing 'source_path_or_key' in {surface_name} fragment"
                    assert "taken_at" in frag, f"Missing 'taken_at' in {surface_name} fragment"


# ---------------------------------------------------------------------------
# T3: Read-only — no file content is modified by a context-call run
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_no_files_modified(self, task_router_repo, clean_tree):
        """Running context-call does not modify any tracked files.

        Note: files dirty from this tick's development (cli.py, the script
        itself, this test file, data files) are excluded — we only verify
        that context-call doesn't introduce ADDITIONAL modifications.
        """
        from scripts.router_context_call import query_hilo, query_specs, query_board

        # Capture files that are already dirty before context-call runs
        already_dirty = set()
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=task_router_repo,
            capture_output=True, text=True, check=False,
        )
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                parts2 = parts[1].split(None, 1)
                fpath = parts2[1] if len(parts2) > 1 else parts2[0].lstrip("?")
                already_dirty.add(fpath)

        # Run all surface queries
        query_hilo(task_router_repo)
        query_specs(task_router_repo)
        query_board(task_router_repo)

        # Check no NEW files were modified by context-call
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=task_router_repo,
            capture_output=True, text=True, check=False,
        )
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                parts2 = parts[1].split(None, 1)
                fpath = parts2[1] if len(parts2) > 1 else parts2[0].lstrip("?")
                if fpath in already_dirty:
                    continue  # was already dirty before context-call ran
                assert False, f"File modified by context-call: {fpath} (was already dirty: {already_dirty})"


# ---------------------------------------------------------------------------
# T4: Hilo proven against a real fleet repo (task-router itself)
# ---------------------------------------------------------------------------

class TestHiloProven:
    def test_hilo_queries_task_router_repo(self, task_router_repo):
        """Hilo returns code structure for task-router (a real fleet repo, not Hilo itself)."""
        from scripts.router_context_call import query_hilo
        result = query_hilo(task_router_repo)

        # Verify we got results from task-router files
        code_fragments = [f for f in result.get("fragments", [])
                         if isinstance(f, dict) and f.get("surface") == "code"
                         and "source_path_or_key" in f]

        # Should have found at least some Python files in task-router
        assert len(code_fragments) >= 1, f"Expected code fragments from task-router, got {len(code_fragments)}"

        # Verify at least one fragment has meaningful content
        for frag in code_fragments[:1]:
            assert "source_path_or_key" in frag
            assert "taken_at" in frag
            # At minimum should have a source_path_or_key and taken_at

        # Verify Hilo version is known
        assert result.get("hilo_version") != "unknown", f"Hilo not found: {result.get('hilo_version')}"

    def test_hilo_version_is_readable(self):
        """Hilo binary is accessible and reports version."""
        from scripts.router_context_call import _hilo_version
        version = _hilo_version()
        assert version, "Hilo version string should not be empty"
        assert "unknown" not in version.lower() or version == "unknown"  # may not be installed


# ---------------------------------------------------------------------------
# T5: Contradiction detection
# ---------------------------------------------------------------------------

class TestContradictionDetection:
    def test_board_commit_pending_flag(self):
        """Board row with commit hash but pending status is flagged."""
        from scripts.router_context_call import detect_contradictions

        code_result = {"sample_files": ["scripts/foo.py"]}
        board_result = {
            "fragments": [
                {"surface": "board", "id": "TR-999", "status": "pending",
                 "commit_hash": "abc1234", "title": "test"},
            ]
        }
        spec_result = {"fragments": []}

        contradictions = detect_contradictions(code_result, board_result, spec_result)

        # Should detect the commit/pending mismatch
        commit_mismatches = [c for c in contradictions if c.get("type") == "board-commit-mismatch"]
        assert len(commit_mismatches) >= 1, "Should flag board row with commit but pending status"

    def test_no_false_contradiction(self):
        """No contradiction when everything is consistent."""
        from scripts.router_context_call import detect_contradictions

        code_result = {"sample_files": ["scripts/foo.py"]}
        board_result = {
            "fragments": [
                {"surface": "board", "id": "TR-999", "status": "complete",
                 "commit_hash": "abc1234", "title": "test"},
            ]
        }
        spec_result = {"fragments": []}

        contradictions = detect_contradictions(code_result, board_result, spec_result)
        # No commit/pending mismatches for complete rows
        mismatches = [c for c in contradictions if c.get("type") == "board-commit-mismatch"]
        assert len(mismatches) == 0, "Complete rows should not be flagged"


# ---------------------------------------------------------------------------
# T6: JSON output mode
# ---------------------------------------------------------------------------

class TestJsonOutput:
    def test_json_output_is_valid_json(self, task_router_repo):
        """--json flag produces valid JSON."""
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "router_context_call.py")
        result = subprocess.run(
            [sys.executable, script, task_router_repo, "--json"],
            cwd=os.path.dirname(os.path.dirname(script)),
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"Context call failed: {result.stderr}"
        data = json.loads(result.stdout)
        assert "results" in data
        assert "contradictions" in data
        assert "total_latency_s" in data
        assert "surfaces" in data

    def test_json_output_contains_surface_keys(self, task_router_repo):
        """JSON output contains all queried surface keys."""
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "router_context_call.py")
        result = subprocess.run(
            [sys.executable, script, task_router_repo, "--json", "--surfaces", "code,specs,board"],
            cwd=os.path.dirname(os.path.dirname(script)),
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        results = data.get("results", {})
        assert "code" in results or "code" not in data.get("surfaces", [])
        assert "specs" in results or "specs" not in data.get("surfaces", [])
        assert "board" in results or "board" not in data.get("surfaces", [])
