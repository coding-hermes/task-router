QA-TASKROUTER-3 — bare-run acceptance self-skips without the derived registry store
Evidence captured 2026-11-06 by foreman tick task-router-foreman-2026-11-06-14-41-12

ROW (from .coding-hermes/board/tasks.jsonl)
  tests/test_validate.py bare-run acceptance is fresh-clone-hostile: asserts registry.json
  exists but the file is gitignored and only CI-seeded.

IMPLEMENTATION
  branch wt/QA-TASKROUTER-3, worker commit ef0f7b7 (1 file, +32/-1, tests/test_validate.py)
  merged to main as d59304b (merge commit, co-author trailer x1, ch:trace present)

AC1 — absent-store arm (foreman-run, worker worktree with NO registry.json)
  $ ls registry.json  -> No such file or directory
  $ ~/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_validate.py -rs
  SKIPPED [1] tests/test_validate.py:256: registry.json is a derived, gitignored store —
              run `make seed` before this bare-run acceptance
  20 passed, 1 skipped in 28.75s        rc=0

AC2 — store-present arm keeps full strength (merged main, registry.json present)
  $ ~/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_validate.py -rs
  21 passed in 29.99s                   rc=0      (0 skipped: the acceptance really ran)
  Stale/invalid arms remain loud and unchanged:
    test_validate_corrupt_registry_exit1 / test_validate_stale_registry_listed /
    test_validate_beyond_tolerance_is_still_stale  -> all passed in the 21

AC3 — pinned test name preserved
  $ git grep -n -F test_bare_run_at_repo_root_exits0_on_healthy_checkout -- tests/test_validate.py
  tests/test_validate.py:302:def test_bare_run_at_repo_root_exits0_on_healthy_checkout(monkeypatch):

RELATED GATE — smoke + web suites on merged main
  $ ~/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_smoke.py tests/test_web.py
  15 passed in 2.52s

LINT — no new findings
  current : I001 tests/test_validate.py:7:1 · E741 tests/test_validate.py:412:35
  base 218e194: I001 tests/test_validate.py:7:1 · E741 tests/test_validate.py:381:35
  Same two classes at base (E741 line shifts by the +31 inserted lines) => zero new lint.
  CI does not run ruff (grep of .github/workflows: no ruff stage).

GITREINS
  tier 1: PASSED
  tier 2: PASS, verdict id 234e9c7d
  verdict.json: .gitreins/history/2026-11-06/1d636908/verdict.json
    task_id=QA-TASKROUTER-3  passed=true  commit=d59304beea33e17f83c7bd228aac7e37dd96127a

REMOTE
  origin/main at d59304b after push; git rev-list --count origin/main..HEAD = 0
