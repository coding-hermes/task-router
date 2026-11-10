README health sweep — independent re-verification pass
Tick: task-router-readme-2026-11-10-12-37-33
Producer: this tick's second pass (first pass artifact: readme-command-matrix-2026-11-10.txt)
Published tip probed: origin/main = 3c095fedf50032bcdf2f431e343a8ef674984943 (git fetch run before probing)
Canonical checkout: /home/kara/task-router HEAD d28b57673f7ca9d926ad282b0946c3b8beb03353 (ahead 1 / behind 1 vs origin/main)
Probe tree: /home/kara/.hermes/stand-in/readme/task-router/probe-2026-11-10-12-37-33 (archive of 3c095fed)

README-5 — the `schemas` target is non-runnable
$ grep -rn "schema_check" --include='*.py' --include='*.md' --include='Makefile' --include='*.sh' --include='*.toml' .
  ./Makefile:87:	@$(VENV_PY) scripts/schema_check.py --all
  (single reference in the whole repo: the target itself. No renamed equivalent exists.)
$ ls scripts/ | grep -i schema
  (no output)
$ git ls-tree origin/main scripts/ | grep -i schema
  (no output, exit 1)
$ ls -d schemas
  ls: cannot access 'schemas': No such file or directory
$ make help | grep schemas
  make schemas   validate live state artifacts against schemas/
$ make -n schemas | tail -1
  .venv/bin/python scripts/schema_check.py --all
$ .venv/bin/python scripts/schema_check.py --all
  .venv/bin/python: can't open file '/home/kara/task-router/scripts/schema_check.py': [Errno 2] No such file or directory
  exit=2
Correction to the first-pass row citation: README.md does NOT mention `make schemas` (`grep -n "make schemas" README.md docs/*.md` → no output). The documented surface is the Makefile target index at Makefile:23 plus the `make help` line, implemented at Makefile:86-87.

README-6 — fresh-clone seed path refuses on committed data
README claims checked:
  README.md:42-45  "Seed the compiled registry (registry.json) from the committed tables. A fresh clone has none ... router seed"
  README.md:53-54  "A checkout's `make seed && make validate` passes with no env vars at all."
  README.md:110-114 "router validate → exit 0, all checks ok" is the clean seeded signal; "`router seed` (or its opt-in self-heal, `router validate --heal`) clears" the pre-seed state.
$ grep -n 'registry.json' .gitignore
  23:registry.json
$ git ls-files --error-unmatch registry.json
  (not tracked — a fresh clone genuinely has no registry.json)
$ ./venv/bin/router seed            # in probe archive
  SATISFIABILITY FAIL: 6 of 264 rating(s) in the emit domain have NO routable lane, across 1 category
    math: levels 0..5 — no lane MEASURES this capability ... blocked holder(s): ollama-cloud/qwen3.5:397b tier=5 (disabled, unpriced)
  Seeding REFUSED: add a routable lane for the categories above, or re-run with --allow-unsatisfiable to seed a knowingly-starved registry
  exit=1
$ VENV_PY=./venv/bin/python make seed     # literal documented target
  ... SATISFIABILITY FAIL ... Seeding REFUSED ...
  make: *** [Makefile:63: seed] Error 1
  exit=2
$ ./venv/bin/router validate --heal        # documented escape hatch
  INVALID — 2 issue(s):
    - registry.exists: missing: <probe>/registry.json — run scripts/router_seed.py once to generate it
    - heal: self-heal seed failed (rc=1): ... ollama-cloud/qwen3.5:397b tier=5 (disabled, unpriced) ... Seeding REFUSED
  exit=1
$ ls registry.json
  ls: cannot access 'registry.json': No such file or directory
Consequence: `make smoke` (fresh-checkout smoke, scripts/fresh_clone_smoke.sh:80-86 marks seed failure as FAIL) cannot pass at the published tip.

Release freshness (was UNVERIFIED in the first pass; closed this pass)
$ git tag -l | wc -l          -> 0
$ git describe --tags --abbrev=0 -> fatal: No names found, cannot describe anything.
$ gh release list -R coding-hermes/task-router --limit 5 -> no output, exit 0
Conclusion: the project has published zero tags and zero releases, so there is no release for the README to be stale against. Freshness is measured against main only.

Link and badge drift
$ grep -n '!\[' README.md -> no output (the README carries no badges/images, so no badge drift is possible)
Relative Markdown targets checked with `git cat-file -e origin/main:<path>`, all 15 present:
  CONTRIBUTING.md, LICENSE, SECURITY.md, docs/category-data-quality.md, docs/chains-2026-09-01.md,
  docs/configuration.md, docs/decisions/review-tr-003-metrics-ledger-retention.md, docs/health-plane.md,
  docs/integration.md, docs/observability-spec.md, docs/outcomes-schema.md, docs/registry-maintenance.md,
  docs/soft-gate-integration.md, docs/specs/SPEC-EXECUTION-DECISION.md, docs/ui/data-command-center.md
External URLs in README are only the clone URL and documented localhost endpoints
(127.0.0.1:8642, :9093, :9391, api.deepseek.com/v1) — no third-party HTTP link to rot.

Reader-question structure
README headings present: identity/use, Contents, Quick start, Verify your install, Data home, The router command,
Web UI, API server and MCP bridge, classified proxy deployment, bus ingress, board intake, Architecture,
Context windows, Profiles, Provider mapping, CLI reference, Configuration, Metrics, Data and namespaces.
No "Limits / known gaps" or "Troubleshooting" section. This class is already carried by README-3 (freshness/structure);
no duplicate row minted.

Existing rows re-measured, not re-filed
README-1-R2: the quickstart's `python3 -m pytest -q tests/` still fails without the `test` extra; held at P3
  because the finding is a broken documented quickstart path (anti-demotion clause), not doc-rot.
README-2: `router --help` lists `context-call` (usage: router context-call [-h] [--surfaces ...]) and README's CLI
  table still omits it — re-triaged to P5 (polish/duplication), not re-filed.
README-4: README.md:346 exports ROUTER_CLASSIFIER_MODEL=deepseek-v4.1-flash; scripts/router_classify.py:309 defaults
  glm-5.3-flash — re-triaged to P4 (doc rot), not re-filed.

UNVERIFIED
- Full `pytest -q tests/` green state at the published tip (first pass timed out at 420s; not re-attempted).
- Live deployment/classifier upstream command (needs credentials and a real gateway).
- Whether GitHub Actions is red for the same seed refusal (not probed this pass).

BOARD-LINT CENSUS (skill requirement: assert before/after counts)
  before (3c095fed board, extracted with git archive): RESULT: FAIL (450 error(s), 569 warning(s))
  after  (this tick's board, b587b66e):               RESULT: FAIL (450 error(s), 571 warning(s))
  delta: errors 0, warnings +2. Both new warnings are the same one:
    "title carries P4 but priority is P3" on README-5 and README-6.
  That is the state this repo's own adapter mandates: TR-348 maps doc severity P4/P5 into the
  closed P0-P3 priority field while the label stays in the title
  (scripts/board_priority_normalize.py, "title untouched, plus a notes provenance note").
  21 pre-existing rows carry the identical warning (DOC-5..DOC-17, TR-284, TR-318, INT-CI-20/21).
  `python3 scripts/board_priority_normalize.py` reports "board already canonical" (rc=0), and the
  pre-push board_commit_boundary_gate reports 0 off-vocabulary priority emissions for this diff.
  The 450 pre-existing errors are unrelated event-ordering defects in events.jsonl (baseline equal).

