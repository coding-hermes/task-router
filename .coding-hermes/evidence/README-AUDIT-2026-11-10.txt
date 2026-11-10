README health sweep: task-router
Tick: task-router-readme-2026-11-10-12-37-33

Identity and publication boundary
- Scheduler stand-in /home/kara/.hermes/stand-in/readme/task-router is not a repository and has no README; no product probes were run there.
- Canonical repository: /home/kara/task-router; remote origin git@github.com:coding-hermes/task-router.git; remote default refs/heads/main; published tip at probe start/end: 3c095fedf50032bcdf2f431e343a8ef674984943.
- Canonical checkout HEAD d28b57673f7ca9d926ad282b0946c3b8beb03353 diverges from origin/main: ahead 1, behind 1. The board surfaces were clean at probe start. No canonical README/docs/board writes were made.
- Published-tip probe: fresh archive at /home/kara/.hermes/stand-in/readme/task-router/probe-2026-11-10-12-37-33, README 44,788 bytes.
- No release tags exist locally; GitHub latest-release endpoint was not re-run in this sweep. Release freshness remains UNVERIFIED.

Freshness source measurements
- git log -1 --format='%H %ad %s' -- README.md on canonical checkout: fd899acff9dcc29b5e4dd91742623da7ead0cf9a 2026-11-05T08:18:00-05:00 feat(TR-377): /metrics Prometheus exposition on the serving listeners (stdlib, five families).
- README was last changed before published origin/main by one board-only commit (3c095fed); `git diff --stat HEAD..origin/main -- README.md` was empty. Post-README shipped-path log includes the latest board-only commit, eeaae758 registry data, 6fe2ef02 blocker record, and preceding implementation/data changes. Claims were compared in the fresh published archive.

Reader-question structure and link inventory
- README contains sections for identity/use, build/run/test/install/deploy. No dedicated "limits and known gaps" section was found, but existing rows/previous sweep cover the structure class; no duplicate row minted.
- Every local Markdown target listed in README was checked in the published archive. One documented local path is absent: `scripts/schema_check.py` (README Makefile table at README.md:23 says `make schemas` validates state artifacts, and README's command inventory has no direct mention of the missing helper; Makefile:86-87 invokes the absent path). This is a broken documented command and a fresh finding.
- The README CLI table contains duplicate rows for chain-run (2), lifecycle (2), outcomes (2), pricing-audit (2), quota (2), and still has no context-call row. Existing README-2 covers this; re-measured, no duplicate minted.
- The README does not contain `context-call`; the installed CLI source contains `scripts/router_context_call.py` and the probe's `router context-call --help` arm returned exit 2 with `invalid choice: 'context-call'` because the probe installed the package from an archive that does not expose the expected entrypoint. This is a probe/setup discrepancy, not a new finding; source and prior README-2 evidence remain the authoritative installed-surface comparison.

Command matrix exact observations
- README quickstart literal `python3 -m pytest -q tests/` was executed from the published archive under the tool runner's interpreter and exited 1: `No module named pytest`. The documented preceding commands install editable package and duckdb only; pyproject.toml:21 declares pytest, duckdb, and pyyaml under the `test` extra. Existing README-1-R2 covers this recurrence; no duplicate minted.
- Safe control: a fresh venv in the published archive installed `.[test]` successfully, then `venv/bin/python -m pytest -q tests/` ran and was still in progress when the bounded 420-second runner timed out. Captured stdout reached 26% with failures and one skip; no final result was observed. This arm is UNVERIFIED beyond the captured partial output.
- Fresh venv editable install succeeded; `pip install duckdb` succeeded. `router seed` executed for real but exited 1 with `SATISFIABILITY FAIL` and `Seeding REFUSED`; it did not produce registry.json. `router validate` then exited 1 with `registry.exists: missing`. This is a documented path defect: the quickstart says `router seed` seeds the registry and that `make seed && make validate` passes with no env vars, but the published data currently refuses seeding on an unsatisfiable `math` rating set. Existing TR-1000976681059248 covers a separate seed refusal/write-atomicity defect; this current unsatisfiable published-tip path needs its own row unless an existing row fingerprint matches.
- `make help` executed and printed the documented targets, including `make schemas`. The Makefile's `schemas` recipe invokes `scripts/schema_check.py`, which is absent from the published archive; the literal target was not run because source inspection proved the executable path is absent and the target is state-validating but not safely runnable without the missing file. Exact source lines: Makefile:86-87.
- `make watchdog ARGS="--report-only --json"` executed against the host's existing listeners and exited 0 by report-only contract, while its JSON reported 9092 STALE (loaded 9d1bf7c38f82 vs repo d28b57673f7c), 9093 UNREACHABLE/HTTP 404, and 9391 STALE. This is the promised report-only output and remains a live-host observation, not a published-tip product verdict.
- `router status`, help arms, spawn, estimate, metrics and other read-only arms were partially captured before the bounded matrix timed out; their full outputs are in the raw probe directory. Several router arms were run against a partially seeded archive and are not promoted to a clean-install pass.

Accuracy and drift checks
- Published README.md:346 still exports `ROUTER_CLASSIFIER_MODEL=deepseek-v4.1-flash`; published scripts/router_classify.py:309 defaults to `glm-5.3-flash`. The stale model identifier finding is already covered by README-4; re-measured, no duplicate minted.
- GitHub latest-release endpoint and external badge/link HTTP checks were not run in this sweep. No README badges/images were present in the extracted link inventory. Release availability and external link status remain UNVERIFIED.
- Repository-specific docs-check target: no README/AGENTS/workflow promise of `make docs-check` was found; no finding filed for its absence.

Findings filed
- README-5 (P4): missing scripts/schema_check.py makes the documented make schemas command non-runnable at published tip. Filed in the dedicated published-tip write worktree; the canonical checkout was not mutated. The worktree board remains structurally FAIL with the unchanged baseline (450 errors, 571 warnings after the writer's two task-created events); no commit or publication was made.
- README-6 (P4): published-tip router seed refuses on the current unsatisfiable math data, contradicting the quickstart seed/validate promise. Filed in the same dedicated write worktree; deduplication against TR-1000976681059246/TR-384 is explicitly left for the foreman. No commit or publication was made.

Traceability
ch:trace row=README-5 spec=docs/traceability-doctrine.md#hard-rules wave=none:serial-readme-sweep test=none:documented-command-execution doc=README.md:23 prompt=~/.hermes/skills/coding-hermes-readme/SKILL.md evidence=readme-command-matrix-2026-11-10.txt witness=none:published-tip-repository-path-probe-only verdict=none:open-finding commit=none:open memory=none:exact-key-not-found
ch:trace row=README-6 spec=docs/traceability-doctrine.md#hard-rules wave=none:serial-readme-sweep test=none:documented-command-execution doc=README.md:42-54 prompt=~/.hermes/skills/coding-hermes-readme/SKILL.md evidence=readme-command-matrix-2026-11-10.txt witness=none:published-tip-fresh-archive-command-run verdict=none:open-finding commit=none:open memory=none:exact-key-not-found
ch:trace row=README-1-R2 spec=docs/traceability-doctrine.md#hard-rules wave=none:serial-readme-sweep test=none:documentation-command-execution doc=README.md:34-70 prompt=~/.hermes/skills/coding-hermes-readme/SKILL.md evidence=readme-command-matrix-2026-11-10.txt witness=none:not-reproduced-as-new-row-existing-finding-remeasured verdict=none:not-reproduced commit=none:no-closure memory=none:exact-key-not-found
ch:trace row=README-2 spec=docs/traceability-doctrine.md#hard-rules wave=none:serial-readme-sweep test=none:cli-surface-comparison doc=README.md:630-670 prompt=~/.hermes/skills/coding-hermes-readme/SKILL.md evidence=readme-command-matrix-2026-11-10.txt witness=none:not-reproduced-as-new-row-existing-finding-remeasured verdict=none:not-reproduced commit=none:no-closure memory=none:exact-key-not-found
ch:trace row=README-4 spec=docs/traceability-doctrine.md#hard-rules wave=none:serial-readme-sweep test=none:classifier-source-comparison doc=README.md:346 prompt=~/.hermes/skills/coding-hermes-readme/SKILL.md evidence=readme-command-matrix-2026-11-10.txt witness=none:not-reproduced-as-new-row-existing-finding-remeasured verdict=none:not-reproduced commit=none:no-closure memory=none:exact-key-not-found

Board boundary: rows README-5 and README-6 were appended in the dedicated origin/main write worktree, not the canonical checkout. The two rows and task-created events are local/branch-only and not published. The canonical checkout remains unchanged and structurally invalid at baseline. README and docs were not edited.
UNVERIFIED: release endpoint/latest release, external link HTTP reachability, final full-suite result after the 420-second timeout, literal execution of the missing schema target, and deployment/classifier live upstream command requiring credentials.

STATUS: PARTIAL
TIER: T3 — fresh published-tip archive commands and live host watchdog executed; rows were written/read back in an unpublished write worktree, while release/link and full-suite final result remain unverified.
EVIDENCE: README-AUDIT-2026-11-10.txt; readme-command-matrix-2026-11-10.txt; probe-2026-11-10-12-37-33/; write-worktree boardctl readback and RESULT: FAIL (450 error(s), 571 warning(s)).
