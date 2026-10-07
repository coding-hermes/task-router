"""QA-TASK-ROUTER-9 — clean-machine suite rules, pinned.

The battery (bunker-las-03, agent uid, git-archive frozen tree) died at
COLLECTION — `PermissionError: [Errno 13]` in 0.82s, zero tests run — and,
once collection survived, bled 14 failures across four classes. These tests
pin the rules that make the suite portable:

1. EACCES at collection: a module-level `Path(...).exists()` PROPAGATES
   PermissionError through a foreign-uid ancestor (os.path.exists swallows
   every OSError). No test module may probe paths with pathlib at import —
   subprocess tests use conftest.router_python().
2. Frozen-tree identity: router_health.git_commit() honours a
   BUILD_COMMIT / ROUTER_COMMIT env override BEFORE the .git read, so a
   git-archive export can serve a real commit; the override wins over .git.
3. Frozen-tree skips: tests asserting git-derived state declare a NAMED
   skip (repo_commit_resolvable / repo_has_git), never a bare failure.

No network, no subprocess with side effects; the EACCES simulation creates
and removes its own 000-dir under tmp_path (as the invoking user).
"""
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import conftest  # noqa: E402
import router_health as rh  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The EACCES collection mechanism (the QA-8 recurrence, root-caused)
# ---------------------------------------------------------------------------

def test_pathlib_exists_raises_on_eaccos_but_os_path_does_not(tmp_path):
    """The premise behind the whole rule: through an unreadable ancestor,
    pathlib's exists() RAISES (it only swallows ENOENT-family errors) while
    os.path.exists() answers False. If a stdlib change ever makes both
    safe, the conftest helpers still work — but this documents WHY pathlib
    is banned at test-module import."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "child").touch()
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            Path(locked / "child").exists()
        assert os.path.exists(str(locked / "child")) is False
    finally:
        locked.chmod(stat.S_IRWXU)


def test_router_python_is_executable_fallback_when_venv_is_behind_eacces(
        tmp_path, monkeypatch):
    """router_python() must DEGRADE to sys.executable — never raise — when
    the stat hits a foreign-uid ancestor. This is the exact shape that killed
    collection on bunker-las-03 (as Path(...).exists()). Cleanup uses an
    onerror handler because the locked subtree itself resists plain rmtree."""
    import shutil

    locked = tmp_path / ".hermes"
    (locked / "venvs" / "board" / "bin").mkdir(parents=True)
    (locked / "venvs" / "board" / "bin" / "python3").touch()
    locked.chmod(0)
    monkeypatch.setattr(conftest, "ROUTER_BOARD_PY",
                        str(locked / "venvs" / "board" / "bin" / "python3"))
    try:
        assert conftest.router_python() == sys.executable
    finally:
        locked.chmod(stat.S_IRWXU)

    def _force(remove, path, exc_info):
        os.chmod(path, stat.S_IRWXU)
        remove(path)

    shutil.rmtree(locked, onerror=_force)


def test_router_python_prefers_the_board_venv_when_readable(monkeypatch,
                                                            tmp_path):
    venv_py = tmp_path / "board" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True)
    venv_py.touch()
    monkeypatch.setattr(conftest, "ROUTER_BOARD_PY", str(venv_py))
    assert conftest.router_python() == str(venv_py)


def test_no_test_module_probes_paths_with_pathlib_at_import():
    """The class rule, enforced over the suite: a module-level
    Path(...).exists() / is_file() / is_dir() CALL is banned (it runs at
    collection, where an EACCES kills the whole run). Parsed with ast over
    module-level statements only — docstrings, comments and function bodies
    are not collection-time execution."""
    import ast

    def _module_level_calls(node):
        """Yield Call nodes at module scope, pruning def/class bodies (those
        run at test time, not collection)."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                yield child
            yield from _module_level_calls(child)

    offender = []
    tests_dir = Path(REPO) / "tests"
    for path in sorted(tests_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module level ONLY, defs/classes pruned
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                continue
            for sub in _module_level_calls(node):
                if (isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in ("exists", "is_file", "is_dir",
                                              "glob", "iterdir")
                        and isinstance(sub.func.value, ast.Call)
                        and isinstance(sub.func.value.func, ast.Name)
                        and sub.func.value.func.id == "Path"):
                    offender.append(f"{path.name}:{sub.lineno}:{sub.func.attr}")
    assert not offender, (
        "module-level pathlib existence probes (EACCES at collection risk): "
        f"{offender} — use conftest.router_python()/os.path.exists()")


# ---------------------------------------------------------------------------
# 2. Frozen-tree identity: BUILD_COMMIT / ROUTER_COMMIT override
# ---------------------------------------------------------------------------

def test_git_commit_honours_build_commit_override(monkeypatch):
    monkeypatch.delenv("ROUTER_COMMIT", raising=False)
    monkeypatch.setenv("BUILD_COMMIT", "9c44f2a1b77e3dd4deadbeef")
    assert rh.git_commit() == "9c44f2a1b77e"      # 12-char, like the .git read


def test_git_commit_honours_router_commit_override(monkeypatch):
    monkeypatch.delenv("BUILD_COMMIT", raising=False)
    monkeypatch.setenv("ROUTER_COMMIT", "0b12fd4fb7bcd0d88cc65e83fd341366")
    assert rh.git_commit() == "0b12fd4fb7bc"


def test_build_commit_wins_over_router_commit(monkeypatch):
    monkeypatch.setenv("BUILD_COMMIT", "aaaa1111bbbb")
    monkeypatch.setenv("ROUTER_COMMIT", "cccc3333dddd")
    assert rh.git_commit() == "aaaa1111bbbb"


def test_blank_env_overrides_are_ignored_not_stamped(monkeypatch):
    """' ' or '' must fall through to the .git read — a whitespace stamp
    would otherwise become the served identity for every /health caller."""
    monkeypatch.setenv("BUILD_COMMIT", "   ")
    monkeypatch.delenv("ROUTER_COMMIT", raising=False)
    # In the dev tree (.git present) the fallback is real; in a frozen tree
    # it is "unknown" — both are valid non-blank outcomes, NEITHER is spaces.
    assert rh.git_commit().strip() != ""


def test_override_beats_a_stale_head(monkeypatch):
    """The override wins even where .git exists: the packager stamped what it
    EXPORTED, which may differ from whatever HEAD the checkout drifted to."""
    monkeypatch.setenv("BUILD_COMMIT", "feed0000beef")
    monkeypatch.setattr(rh, "REPO", Path("/nonexistent/.git-nowhere"))
    assert rh.git_commit() == "feed0000beef"


def test_frozen_tree_without_override_stays_unknown_fail_open(monkeypatch):
    """Fail-open preserved: no env, no .git -> 'unknown', never a raise and
    never a fabricated hash."""
    monkeypatch.delenv("BUILD_COMMIT", raising=False)
    monkeypatch.delenv("ROUTER_COMMIT", raising=False)
    monkeypatch.setattr(rh, "REPO", Path("/nonexistent/.git-nowhere"))
    assert rh.git_commit() == "unknown"


# ---------------------------------------------------------------------------
# 3. The named-skip predicates
# ---------------------------------------------------------------------------

def test_repo_has_git_is_true_in_this_checkout_when_present():
    """Pinned against an explicit fixture, not the ambient tree: a dir with
    .git is history-capable, the same dir without it is frozen. (The dev
    checkout happens to have .git, but this suite also runs in frozen-tree
    QA cells where it must NOT assert that.)"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        monkey_repo = Path(td)
        (monkey_repo / "tests").mkdir()
        real = conftest.__file__
        monkey_file = str(monkey_repo / "tests" / "conftest.py")
        try:
            conftest.__file__ = monkey_file
            assert conftest.repo_has_git() is False   # no .git -> frozen
            (monkey_repo / ".git").mkdir()
            assert conftest.repo_has_git() is True    # dir -> normal checkout
            (monkey_repo / ".git").rmdir()
            (monkey_repo / ".git").write_text("gitdir: /x.git\n")
            assert conftest.repo_has_git() is True    # file -> linked worktree
        finally:
            conftest.__file__ = real


def test_repo_commit_resolvable_true_when_build_commit_set(monkeypatch):
    monkeypatch.setenv("BUILD_COMMIT", "abc")
    assert conftest.repo_commit_resolvable() is True


def test_repo_commit_resolvable_false_when_neither_source(monkeypatch,
                                                          tmp_path):
    monkeypatch.delenv("BUILD_COMMIT", raising=False)
    monkeypatch.delenv("ROUTER_COMMIT", raising=False)
    monkeypatch.setattr(conftest, "__file__",
                        str(tmp_path / "tests" / "conftest.py"))
    assert conftest.repo_commit_resolvable() is False
    assert conftest.repo_has_git() is False


def test_repo_has_git_accepts_a_gitfile_worktree(tmp_path, monkeypatch):
    """A linked worktree carries .git as a FILE (gitdir: pointer) — the
    predicate must treat it as history-capable, not frozen."""
    (tmp_path / ".git").write_text("gitdir: /somewhere/else.git\n")
    monkeypatch.setattr(conftest, "__file__",
                        str(tmp_path / "tests" / "conftest.py"))
    assert conftest.repo_has_git() is True
