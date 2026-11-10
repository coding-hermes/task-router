README health sweep producer evidence
Tick: task-router-readme-2026-11-10-12-37-33
Probe tree: published origin/main archive at 3c095fedf50032bcdf2f431e343a8ef674984943

Finding A: documented make schemas path
README.md:23 lists `make schemas` as a state-artifact validation target.
Makefile:86-87 contains:
  schemas: install
          @$(VENV_PY) scripts/schema_check.py --all
Published-tip existence probe:
  git ls-tree -r --name-only 3c095fedf50032bcdf2f431e343a8ef674984943 | grep -F scripts/schema_check.py
Observed: no output; exit status 1.
Fresh archive execution boundary: the target's referenced executable is absent, so the literal target was not invoked. This is a documented-command source-path failure, not a runner error.

Finding B: documented seed promise
README.md:42-46 says `router seed` seeds the compiled registry; README.md:52-54 says a checkout's `make seed && make validate` passes with no environment variables.
Fresh published-tip archive command sequence:
  python3 -m venv <probe>/venv
  <probe>/venv/bin/pip install -e .
  <probe>/venv/bin/pip install duckdb
  <probe>/venv/bin/router seed
Observed: editable install exit 0; duckdb install exit 0; router seed exit 1.
Decisive output:
  SATISFIABILITY FAIL: 6 of 264 rating(s) in the emit domain have NO routable lane, across 1 category
    math: levels 0..5 — no lane MEASURES this capability ...
  Seeding REFUSED: add a routable lane for the categories above, or re-run with --allow-unsatisfiable to seed a knowingly-starved registry
Follow-up:
  <probe>/venv/bin/router validate
Observed: exit 1; `[FAIL] registry.exists: missing: <probe>/registry.json — run scripts/router_seed.py once to generate it`.
The seed refusal is from the published data path; no registry.json was produced in the probe archive.

Existing findings remeasured without new rows
README-1-R2: literal `python3 -m pytest -q tests/` exited 1 with `No module named pytest`; control `pip install -e '.[test]'` succeeded, but the full control suite timed out after 420 seconds with stdout only through 26% and no final result.
README-2: CLI reference duplicates chain-run, lifecycle, outcomes, pricing-audit and quota; context-call remains absent from README. No duplicate row minted.
README-4: README.md:346 exports `ROUTER_CLASSIFIER_MODEL=deepseek-v4.1-flash`; scripts/router_classify.py:309 defaults to `glm-5.3-flash`. No duplicate row minted.

Other observed probes
`make watchdog ARGS="--report-only --json"` exited 0 and returned 9092 STALE, 9093 UNREACHABLE HTTP 404, and 9391 STALE against existing host listeners.

This producer artifact is frozen before board-row authoring. It contains no ch:trace marker.
