"""QA-TASKROUTER-5 — fresh-install packaging contract for the `dev` extra.

The QA finding: `pip install -e .[dev]` exits 0 but pip warns
"task-router 0.2.0 does not provide the extra 'dev'" — the advertised
developer install path did not exist (pyproject.toml only defined the
`test` extra). This pins the contract at the pyproject level so removing
or renaming `dev` fails the suite instead of only warning at install time.

Contract (pyproject.toml → project.optional-dependencies):
  - `dev` exists and resolves (through self-referential extras) to at
    least what the suite needs: pytest, duckdb, pyyaml;
  - `test` itself directly names those three (pytest runs the suite;
    duckdb is imported by scripts/router_seed.py, which conftest.py runs
    per session; pyyaml by scripts/router_probe_run.py — INT-CI-20260928-01);
  - the project declares no runtime [project] dependencies — scripts/ stay
    stdlib-only for the bare board venv (the README install contract).

Parses pyproject.toml with stdlib tomllib (requires-python >=3.11), so the
test depends on nothing installed on the host.
"""
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

with open(REPO_ROOT / "pyproject.toml", "rb") as f:
    PYPROJECT = tomllib.load(f)

SUITE_DEPS = {"pytest", "duckdb", "pyyaml"}


def _extras() -> dict:
    return PYPROJECT["project"]["optional-dependencies"]


def _dep_name(req: str) -> str:
    """PEP 508 name of a requirement string ('pyyaml>=6' -> 'pyyaml')."""
    return re.split(r"[<>=!~;\[]", req.strip(), maxsplit=1)[0].lower()


def test_dev_extra_is_declared():
    assert "dev" in _extras()


def test_test_extra_directly_names_the_suite_deps():
    names = {_dep_name(d) for d in _extras()["test"]}
    assert SUITE_DEPS <= names


def test_dev_extra_resolves_to_the_suite_deps():
    """Resolve `dev` transitively, following task-router[x] self-references.

    A self-referential extra is the deduplicated form of "dev includes the
    test deps"; the resolver walks it so the assertion holds for both shapes
    (self-reference or a directly duplicated list).
    """
    extras = _extras()
    project_name = PYPROJECT["project"]["name"]
    self_ref = re.compile(
        rf"^{re.escape(project_name)}\[(?P<extras>.+)\]\s*$", re.IGNORECASE
    )
    resolved: set[str] = set()
    queue, seen = ["dev"], set()
    while queue:
        extra = queue.pop()
        if extra in seen:
            continue
        seen.add(extra)
        assert extra in extras, f"extra '{extra}' referenced but not declared"
        for req in extras[extra]:
            m = self_ref.match(req.strip())
            if m:
                queue.extend(e.strip() for e in m.group("extras").split(","))
            else:
                resolved.add(_dep_name(req))
    assert SUITE_DEPS <= resolved


def test_runtime_stays_stdlib_only():
    assert "dependencies" not in PYPROJECT["project"]
