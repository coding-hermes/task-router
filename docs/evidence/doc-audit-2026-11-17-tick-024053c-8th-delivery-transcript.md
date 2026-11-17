# task-router documentation audit — 8th delivery transcript

Tick: task-router-docs-2026-11-17-02-40-53
Generated: 2026-11-17T02:45:34-05:00
Probe root: /home/kara/.hermes/cache/scratch/tr-docs-8th (detached worktree, clean)
Canonical checkout: /home/kara/task-router (read-only reference; never a probe root or write target)

TIP ADVANCE: the worktree was first cut at 2707b9a3; origin/main advanced to
fbc7a57 (fix(router): market ingest refresh converges) before this transcript was
frozen. The advance touched NO .md file (5 paths: scripts/router_market_ingest.py,
tests/test_market_ingest.py, data/tables/benchmarks.jsonl, two backfill_queue files),
so every finding below was re-run at the advanced tip. No measurement in this file
mixes revisions.

## 1. Repository identity and published tip
$ git -C /home/kara/.hermes/cache/scratch/tr-docs-8th remote show origin | head -3
* remote origin
  Fetch URL: git@github.com:coding-hermes/task-router.git
  Push  URL: git@github.com:coding-hermes/task-router.git
$ git log -1 --format="%H %ci %s" origin/main
fbc7a5732578990f3fe9d10c37b936f69f979b6c 2026-11-17 02:40:33 -0500 fix(router): market ingest refresh converges — purge provenance-less rows, regenerate the corpus with source_ref
$ git rev-list --left-right --count origin/main...HEAD  (probe root)
0	0
$ git status --porcelain | wc -l  (probe root dirty paths)
0

## 2. Canonical checkout state (write-stop evidence)
$ git -C /home/kara/task-router rev-list --left-right --count origin/main...HEAD
118	7
$ git -C /home/kara/task-router status --porcelain | wc -l  (dirty paths)
8
$ git -C /home/kara/task-router status --porcelain
 M data/tables/model_perf.jsonl
 M data/tables/model_tier.jsonl
 M data/tables/models.jsonl
 M data/tables/probe_providers.jsonl
 M data/tables/providers.jsonl
 M scripts/router_outcomes.py
?? data/router.duckdb
?? data/state/backfill_queue/

Conclusion: canonical checkout is diverged (behind/ahead) and dirty -> not a write target.

## 3. Reader-facing corpus and dead markdown links
$ python3 <skill>/scripts/doc_link_census.py . --exclude <generated trees>
root: /home/kara/.hermes/cache/scratch/tr-docs-8th
markdown files scanned: 84 (excluded: docs/evidence, docs/dogfood, docs/reviews, docs/decisions, docs/research, docs/releases, docs/registry-runs, docs/usage, docs/acceptance, docs/envelopes, .coding-hermes)
dead relative links: 0
distinct dead targets: 0
  resolve by suffix swap [('.md', '.spec.md')]: 0   <- mechanical generator class
  resolve neither way: 0   <- pages never captured; needs a manifest, not a rewrite

## 4. Backticked path-reference census (links + inline code paths)
$ python3 /home/kara/.hermes/stand-in/docs/task-router/d8_path_ref_census.py /home/kara/.hermes/cache/scratch/tr-docs-8th
reader-facing markdown files: 84
path references resolved: 515

DEAD repo-path references (14 distinct):
  data/modelsdev_cache.json                      cited at docs/configuration.md:37
  data/registry-edits.jsonl                      cited at docs/ui/data-command-center.md:240
  data/state/outcomes-verified.jsonl             cited at CHANGELOG.md:28, docs/outcomes-schema.md:215, docs/outcomes-schema.md:221
  data/state/redis-health.json                   cited at docs/specs/SPEC-REDIS-READ-PLANE.md:217
  data/state/redis-instance.json                 cited at docs/specs/SPEC-REDIS-READ-PLANE.md:313
  data/state/redis-publish-errors.jsonl          cited at docs/specs/SPEC-REDIS-READ-PLANE.md:195
  docs/drivers.md                                cited at docs/outcomes-schema.md:129
  docs/muster-quickstart.md                      cited at docs/muster-scope.md:72
  scripts/router_redis.py                        cited at docs/specs/SPEC-REDIS-READ-PLANE.md:286
  scripts/router_redis_up.sh                     cited at docs/specs/SPEC-REDIS-READ-PLANE.md:317
  tests/check_spec_norms.py                      cited at docs/specs/SPEC-REDIS-READ-PLANE.md:338
  tests/test_evidence_replay.py                  cited at docs/evidence-run-store-decision.md:64
  tests/test_redis_read_plane.py                 cited at docs/specs/SPEC-REDIS-READ-PLANE.md:337
  tests/test_x.py                                cited at docs/traceability-doctrine.md:40

GITIGNORED/runtime paths (8 distinct, not a defect):
  data/metrics.jsonl                             cited at README.md:709, docs/configuration.md:308, docs/observability-spec.md:34
  data/state/demotions.jsonl                     cited at docs/configuration.md:194
  data/state/intake-resolves.jsonl               cited at README.md:497, docs/board-task-intake.md:32, docs/board-task-intake.md:79
  data/state/intake-seen.jsonl                   cited at README.md:495, docs/board-task-intake.md:25, docs/board-task-intake.md:63
  data/state/outcomes-averages.jsonl             cited at docs/outcomes-schema.md:13, docs/owner-asks.md:18
  data/state/outcomes.jsonl                      cited at README.md:647, README.md:733, docs/configuration.md:34
  data/state/router-events.jsonl                 cited at docs/configuration.md:311
  data/state/unservable-counters.jsonl           cited at README.md:262, docs/configuration.md:307

## 5. FINDING 1 — docs/drivers.md is cited by a reader-facing contract doc but exists nowhere
$ git ls-files docs/drivers.md
  (empty output = no such tracked file)
$ git log --all --oneline --diff-filter=A -- docs/drivers.md
  (empty output = never existed in any branch of this clone)
$ git grep -n "docs/drivers.md"
docs/outcomes-schema.md:129:HTTP path, which guards a bounded tail. See `docs/drivers.md` for the plugin
$ sed -n "126,131p" docs/outcomes-schema.md

Bulk import is idempotent over the **whole** store (it scans once, then appends
only unseen `(source, session, model)` keys) — the opposite trade-off from the
HTTP path, which guards a bounded tail. See `docs/drivers.md` for the plugin
contract.

$ git grep -n "shared driver contract" -- scripts
scripts/drivers/base.py:1:"""drivers/base.py — the shared driver contract.
$ grep -n "driver contract" docs/specs/SPEC-PROXY-DRIVERS.md | head -3

## 6. FINDING 2 — operator-facing env keys with zero markdown mention
$ python3 /home/kara/.hermes/stand-in/docs/task-router/d8_env_census.py /home/kara/.hermes/cache/scratch/tr-docs-8th | head -12
environment keys read by tracked python: 160
keys with ZERO reader-facing mention: 37

  BOARD_MERGE_DRIVER                 tests/test_board_merge_driver.py
  DUCKBRAIN_NAMESPACES               scripts/sync_task_router_duckbrain.py
  DUCKBRAIN_TOKEN_FILE               scripts/sync_task_router_duckbrain.py
  DUCKBRAIN_URL                      scripts/router_context_call.py, scripts/sync_task_router_duckbrain.py
  FLEET_POLICY_HTTP_TIMEOUT          scripts/fleet-cooldown-policy.py
  GH_HOST                            scripts/evidence_replay.py
  HERMES_API_KEY                     scripts/dummy_scheduler.py, scripts/router_ui_page.py
  HILO_BIN                           scripts/router_context_call.py
  JEV_MODEL                          scripts/router_jev.py

per-key census: tracked .md files mentioning the key (0 = documented nowhere)
  DUCKBRAIN_URL                  md=0  alltracked=2  src: scripts/router_context_call.py scripts/sync_task_router_duckbrain.py 
  DUCKBRAIN_TOKEN_FILE           md=0  alltracked=1  src: scripts/sync_task_router_duckbrain.py 
  DUCKBRAIN_NAMESPACES           md=0  alltracked=1  src: scripts/sync_task_router_duckbrain.py 
  JEV_MODEL                      md=0  alltracked=1  src: scripts/router_jev.py 
  MUSE_CODE_SUB_TOKEN            md=0  alltracked=3  src: scripts/router_muse_code.py 
  MUSE_CODE_SUB_CREDENTIALS      md=0  alltracked=1  src: scripts/router_muse_code.py 
  OUTCOMES_FRESHNESS_MAX_AGE_H   md=0  alltracked=2  src: scripts/router_outcomes_freshness.py 
  PROVIDER_HEALTH_URL            md=0  alltracked=1  src: scripts/provider_health_summary.py 
  QUALITY_ARTIFACT_PATH          md=0  alltracked=2  src: scripts/quality_guard.py tests/test_quality_guard.py 
  QUALITY_FLOOR_PATH             md=0  alltracked=2  src: scripts/quality_guard.py tests/test_quality_guard.py 
  HERMES_API_KEY                 md=0  alltracked=4  src: scripts/dummy_scheduler.py scripts/router_server.py 

$ python3 scripts/router_outcomes_freshness.py --help   (T3: the script has no help surface; it runs)
BREACH: the outcome store could not be read — /home/kara/.hermes/cache/scratch/tr-docs-8th/data/state/outcomes.jsonl: [Errno 2] No such file or directory: '/home/kara/.hermes/cache/scratch/tr-docs-8th/data/state/outcomes.jsonl'

$ cd /home/kara/.hermes/cache/scratch/tr-docs-8th && python3 scripts/quality_guard.py --help
usage: quality_guard.py [-h] [--check-only] [--json]

Quality guard: regression floor for the quality artifact

options:

## 7. FINDING 3 — a ch:trace marker in a decision doc cites a test file that does not exist
$ sed -n "60,66p" docs/evidence-run-store-decision.md
old debt; converting old rows happens row-by-row as they are re-touched, or
never. Mass-backfill would manufacture evidence, which is the failure mode
under repair.

<!-- ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_evidence_replay.py evidence=docs/evidence-run-store-decision.md -->
$ git ls-files tests/test_evidence_replay.py
  (empty output = no such test file)
$ git grep -ln "evidence_replay" -- tests
tests/test_tr251_boundary_and_replay.py
tests/test_tr339_ci_wiring.py

## 8. MEASURED ACCURATE (filed nothing)
$ python3 /home/kara/.hermes/stand-in/docs/task-router/d8_verb_census.py /home/kara/.hermes/cache/scratch/tr-docs-8th | tail -3
router decide  mentions=6   first=README.md:237

VERBS WITH ZERO reader-facing mention (1/28): context-call

$ make help
make venv      create the local virtualenv
make install   install the package into it (editable)
make seed      generate registry.json (the one step needing duckdb)
make validate  assert the checkout is healthy
make status    print machine-readable registry/health/quota state
make serve     run the API + UI on 9092 (foreground: Ctrl-C to stop)
make watchdog  TR-255: check every fleet listener's /health code.stale
make restart-router  TR-255: the deploy contract (pull -> sync -> restart -> verify)
make release-check    RELEASE-001: fail-closed preflight (tags/dirty/ancestor/pins)
make release-build    RELEASE-001: wheel + sdist + SHA256SUMS into dist/
make release-dry-run  RELEASE-001: full cut, NO tag, NO Release (dry-run default)
make test      the full suite
make guard     the repo's gitreins guard command (tests + CI gate)
make hooks     install .githooks (the pre-push CI gate)
make smoke     fresh-checkout smoke: venv -> install -> seed -> status
make schemas   validate live state artifacts against schemas/
$ grep -c "^#   " Makefile  vs  make help line count
16
16
  (16 help lines for 19 real targets: scan and clean are absent from the help surface)

$ git grep -nE "make [a-zA-Z0-9_.-]+" over the reader corpus (targets named in docs)
      9 make release-check
      7 make release-build
      5 make sure
      5 make release-dry-run
      4 make validate
      4 make the
      4 make seed
      4 make a
      3 make watchdog
      3 make restart-router
      1 make venv
      1 make two

## 9. Board ownership de-duplication (published board at this tip)
  docs/drivers.md              board_rows_mentioning=0
  DUCKBRAIN_URL                board_rows_mentioning=0
  JEV_MODEL                    board_rows_mentioning=0
  MUSE_CODE_SUB                board_rows_mentioning=0
  PROVIDER_HEALTH_URL          board_rows_mentioning=0
  QUALITY_ARTIFACT_PATH        board_rows_mentioning=0
  QUALITY_FLOOR_PATH           board_rows_mentioning=0
  HERMES_API_KEY               board_rows_mentioning=0
  test_evidence_replay         board_rows_mentioning=1

No open documentation row owns either mechanism. The single board row mentioning
test_evidence_replay is TR-343 (a functional CI evidence-replay row, not a doc row);
docs/drivers.md is mentioned by no row at all; the DUCKBRAIN_*/QUALITY_*/JEV_MODEL/
MUSE_CODE_*/PROVIDER_HEALTH_URL/HERMES_API_KEY keys are mentioned by no row.
Existing documentation rows DOC-11/DOC-16 (ROUTING_* store overrides) and
DOC-26/DOC-27/DOC-29 (single ROUTING_*/ROUTER_* keys) are EXCLUDED from finding 2 by
name so the row is a residual, not a sibling of an owned mechanism.

## 10. Board lint baseline (before filing)
[error] events.jsonl line 1025: event id 1000043204127924 descends below earlier id 1794082887258446167 (ids must ascend; gaps tolerated)
RESULT: FAIL (455 error(s), 606 warning(s))
boardctl: validation failed

## 11. Unverified / boundaries
- No T5: no real routing decision was driven through the proxy this sweep.
- Static source-vs-doc differentials (findings 1-3) are T2; find 2 additionally ran
  the two scripts named in it (T3 for the executable surfaces, not for the doc claim).
- docs/usage/context-call.md is reader-facing for the integrator audience and was
  included in the key census (HILO_BIN resolves there; DUCKBRAIN_* do not).
- This transcript contains exactly two trace-marker TOKEN occurrences, both INHERITED:
  one in the section-7 heading text and one in the quoted line read back out of
  docs/evidence-run-store-decision.md. This sweep authored no trace marker here; every
  authored marker lives on the board rows it describes.
