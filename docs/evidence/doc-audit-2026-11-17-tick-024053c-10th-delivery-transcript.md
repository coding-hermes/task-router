# Documentation audit transcript — task-router (tick task-router-docs-2026-11-17-02-40-53, 10th delivery)

AUDITED TIP: f146a61e8dfef4203ed6573c986ec34609d6df02 (origin/main at tick start;
`git rev-parse origin/main`).
PREVIOUS DELIVERY: 03eac105 (9th delivery) filed DOC-39/DOC-40 on tip 3f675db5;
f146a61e is a data-only commit after it (`data(registry): market cross-check the
GLM-5.3 family — 3 cards`).
PROBE ROOT: `/home/kara/.hermes/cache/scratch/tr-docs-10th/wt` — clean detached
worktree at f146a61e (`git worktree add --detach ... f146a61e`; `git status
--porcelain` empty).
CANONICAL CHECKOUT: /home/kara/task-router was NOT used as a probe root or a
write target — `git rev-list --left-right --count origin/main...HEAD` printed
`120	7` (120 behind, 7 ahead) with 5 modified and 2 untracked paths
(`data/tables/*.jsonl`, `scripts/router_outcomes.py`, `data/router.duckdb`,
`data/state/backfill_queue/`), i.e. a write stop for board mutation.

All commands below were run from the probe root unless a `cd` is shown.

## 1. Link and anchor censuses

```
$ python3 <skill>/scripts/doc_link_census.py .
markdown files scanned: 191
dead relative links: 1
distinct dead targets: 1
SAMPLE docs/reviews/tr388-quorum/seat1-glm.md -> r  [no renamed sibling (never captured)]
```

The single dead link lives in `docs/reviews/` (archival tree, excluded from the
reader-facing corpus by the census's own definition). No filing.

```
$ python3 <skill>/scripts/doc_anchor_census.py . --all
md files scanned: 191
dead internal anchors: 0
```

## 2. Backticked path-reference census

```
$ python3 <skill>/scripts/path_ref_census.py . --show-runtime
corpus files scanned: 84
path tokens resolved: 517
DEAD repo-path references (15 distinct):
  data/modelsdev_cache.json            <- docs/configuration.md:37
  data/registry-edits.jsonl            <- docs/ui/data-command-center.md:240
  data/state/outcomes-verified.jsonl   <- CHANGELOG.md:28, docs/outcomes-schema.md:215,:221
  data/state/redis-health.json         <- docs/specs/SPEC-REDIS-READ-PLANE.md:217
  data/state/redis-instance.json       <- docs/specs/SPEC-REDIS-READ-PLANE.md:313
  data/state/redis-publish-errors.jsonl<- docs/specs/SPEC-REDIS-READ-PLANE.md:195
  data/tables/projects.js              <- docs/completeness-backlog-2026-10-12.md:128, -10-13.md:128
  docs/drivers.md                      <- docs/outcomes-schema.md:129
  docs/muster-quickstart.md            <- docs/muster-scope.md:72
  scripts/router_redis.py              <- docs/specs/SPEC-REDIS-READ-PLANE.md:286
  scripts/router_redis_up.sh           <- docs/specs/SPEC-REDIS-READ-PLANE.md:317
  tests/check_spec_norms.py            <- docs/specs/SPEC-REDIS-READ-PLANE.md:338
  tests/test_evidence_replay.py        <- docs/evidence-run-store-decision.md:64
  tests/test_redis_read_plane.py       <- docs/specs/SPEC-REDIS-READ-PLANE.md:337
  tests/test_x.py                      <- docs/traceability-doctrine.md:40
```

Classification (each read at the cited line before rejecting):

| token | verdict |
|---|---|
| data/modelsdev_cache.json | OWNED by open DOC-9 (the ROUTER_MODELSDEV_CACHE default) |
| docs/drivers.md | OWNED by open DOC-36 |
| tests/test_evidence_replay.py | OWNED by open DOC-38 (phantom `test=`) |
| docs/muster-quickstart.md | REJECTED — muster-scope.md:72 reads "(suggested home: `docs/muster-quickstart.md`)", a citation of intent for a deliverable (surface C), not of an artefact |
| data/tables/projects.js | REJECTED — quoted inside a row's evidence string in a dated backlog (`evidence='[{"command": "python3 -c count profile on data/tables/projects.js`), archival tree, and the token is a quotation, not a doc claim |
| data/registry-edits.jsonl | REJECTED — runtime store its own writer creates (router_server edit mode audits refusals there); not a shipped file |
| data/state/redis-*.json, scripts/router_redis*.py, tests/test_redis*.py, tests/check_spec_norms.py | REJECTED — docs/specs/SPEC-REDIS-READ-PLANE.md:3-5 declares itself "Implementation authority, subject to the R52 measurement gate. v0.3 has NOT yet been re-judged"; its §14/§15 name the files it will create. Citations of intent. |
| tests/test_x.py | REJECTED — traceability-doctrine.md:40 uses it as an illustrative placeholder |

Tree of `docs/` (12 md) and `docs/decisions/` re-checked separately because the
shipped census excludes them by default:

```
$ python3 usage_decisions_paths.py wt
files scanned: 12
DEAD tokens: 2
  TASK_ROUTER_HOME/metrics.jsonl  <- docs/decisions/review-tr-003-metrics-ledger-retention.md:77
  data/metrics.jsonl              <- docs/decisions/review-tr-003-metrics-ledger-retention.md:1, :9, :117
```

Both are runtime/gitignored stores — not defects.

## 3. Environment-key census

```
$ python3 env_census.py wt
audited tip: f146a61e8dfef4203ed6573c986ec34609d6df02
source files scanned: 414   env keys derived: 187
reader-facing md files: 72
UNDOCUMENTED env keys: 53
```

Every one of the 53 was resolved to its reader:

- 11 operator keys — OWNED by open DOC-37 (DUCKBRAIN_URL / DUCKBRAIN_TOKEN_FILE /
  DUCKBRAIN_NAMESPACES / MUSE_CODE_SUB_TOKEN / MUSE_CODE_SUB_CREDENTIALS /
  JEV_MODEL / OUTCOMES_FRESHNESS_MAX_AGE_H / PROVIDER_HEALTH_URL /
  QUALITY_ARTIFACT_PATH / QUALITY_FLOOR_PATH / HERMES_API_KEY).
- 7 store/endpoint keys — OWNED by DOC-11/DOC-16 (ROUTING_MD_CACHE,
  ROUTING_STATE_DB, ROUTING_DUCKBRAIN_HOME, ROUTING_BOARD_PY,
  ROUTING_LIFECYCLE_FILE), DOC-26 (ROUTING_TIER_LADDER_IGNORE), DOC-27
  (ROUTER_BASE_URL), DOC-29 (ROUTER_STATE_DIR_WARN).
- 1 documented elsewhere: HILO_BIN (docs/usage/context-call.md:23).
- 18 test/infra fixtures — DOC-37 already names and rejects these by name.
- 16 shell-script locals (REPO_DIR, REPO_SCRIPTS, LOGDIR, LIVE_DIR, LIVE_HASH,
  CANONICAL_HASH, SIDECAR, SHOME, SPEC_URL, PROBE_OUT, RC_GATE, REQ_INIT,
  REQ_INITED, SEEDED, FAKE_HOME, GUARD_SUITE_TIMEOUT). Read at their definitions:
  all but one are internal `VAR=...` assignments in scripts/sync_runtime.sh,
  scripts/muster_battery.sh, scripts/provider_health_publish.sh,
  scripts/router-data-quality.sh, scripts/fresh_clone_smoke.sh,
  scripts/gitreins-guard-tests.sh — not read from the environment by an operator.
  GUARD_SUITE_TIMEOUT is an override, but it is documented in its own script
  header (scripts/gitreins-guard-tests.sh:46 "Override with
  GUARD_SUITE_TIMEOUT=<seconds>"), which is the surface its reader runs.

No new filing on this axis.

## 4. CLI surface

```
$ PYTHONPATH=. python3 -m task_router.cli --help
26 verbs: chain-run circuit clinepass context-call decide diff estimate gaps
ingress learn ledger lifecycle maintain metrics modelsdev outcomes plan-sweep
pricing pricing-audit probe probefix quota seed server spawn status validate web
```

Per-verb subcommand parity (`<verb> --help`, exit 0 for each):

| verb | argparse choices | README table | verdict |
|---|---|---|---|
| learn | dump,lesson,doctrine,provider | dump,lesson,doctrine,provider | match |
| modelsdev | sync,fetch,mappings | fetch,sync,mappings | match |
| ledger | start,end,status | start,end,status | match |
| circuit | record-failure,record-success,status,clear | same | match |
| ingress | endpoints,translate,forward,poll,serve | same | match |
| maintain | reprice,seed,export,snapshot,commit,all | reprice/seed/export/snapshot/commit (no `all`) | adjacent to open README-1000489997499222, which owns README:660's correctness — not re-filed |
| outcomes | +apply-verdicts,+deadline-report | absent | OWNED by DOC-18 |
| quota | +accounting,pacing,burn | absent | OWNED by DOC-30 |

`router --help` still prints 6 of 26 commands with a blank description
(chain-run, context-call, ingress, lifecycle, outcomes, pricing-audit) — OWNED by
DOC-28. `router probe --help` documents `--only`, `--no-write/--dry-run`,
`--config`, `--providers`, `--output`; README:661 matches.

## 5. The finding — observability spec versus the served endpoint

Live readings, all on processes whose source revision is the audited tip
(`python3 scripts/router_deploy.py` dry-run printed
`"source_revision": "f146a61e8dfef4203ed6573c986ec34609d6df02"`, pid 109895 for
:9092; `ss -ltnp` shows pids 3315430 (:9092) and 752047 (:9391) running
`/home/kara/task-router/scripts/router_server.py --mode read-only`):

```
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9092/metrics   -> 200
$ curl -s http://127.0.0.1:9092/metrics | grep -c '^task_router_'        -> 850
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9391/metrics   -> 200
$ curl -s http://127.0.0.1:9391/metrics | grep -c '^task_router_'        -> 852
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9093/metrics   -> 404
    (:9093 is /home/kara/.hermes/scripts/router_web.py — the `router web`
     settings UI, a different program from router_server's ui surface)
```

router_server's `ui` surface probed separately on a scratch port (ephemeral
daemon, scratch `ROUTER_STATE_DIR`, killed by `timeout 25`):

```
$ ROUTER_STATE_DIR=<scratch> PYTHONPATH=. python3 scripts/router_server.py \
    --mode read-only --surface ui --host 127.0.0.1 --port 19099 &
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:19099/health   -> 200
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:19099/metrics  -> 200
$ curl -s http://127.0.0.1:19099/metrics | wc -l                        -> 838
```

Served families (`# TYPE` lines, api+proxy identical):

```
router_admission_inflight, router_admission_queue_depth,
router_admission_rejected_total, router_chain_hops, router_circuit_open_pairs,
router_circuit_pair_state, router_circuit_provider_breakers,
router_lane_coverage, router_lane_excluded_total, router_metrics_jsonl_bytes,
router_metrics_jsonl_family_bytes, router_prometheus_scrape_errors_total,
router_proxy_nohops_total, router_proxy_requests_total,
router_resolve_duration_seconds, router_resolve_errors_total,
router_resolve_nohops_total, router_resolve_total, router_tier,
router_tier_cost_cells, router_tier_effort_levels
— each ALSO exported under the task_router_ prefix (42 families in total).
```

`docs/observability-spec.md` at the same tip still says the opposite:

- :3-7 "Status: proposed (audit 2026-11-03) … Audit verdict before: **NONE** —
  zero `/metrics` endpoint on any listener … Audit verdict after this spec is
  implemented: **SCRAPE-READY**".
- :41 (under "## 2. Proven current state (evidence, not speculation)")
  `| Is there a /metrics endpoint? | **No.** All three listeners return 404. |`
- :42 `| Is a Prometheus client used? | **No.** Absent from pyproject.toml … |`
  (still true, but the §4 prescription it supports is not what shipped)
- :176-178 "**Decision: a `/metrics` route on the existing servers (all three
  listeners), via the official `prometheus_client` library.**"
- :186 "# scripts/router_server.py — shape of the change, not applied by this audit"

The shipped implementation contradicts the prescription and confirms the route:

```
$ grep -rn prometheus_client --include=*.py --include=*.toml . | head
./scripts/router_prometheus.py:6: here instead of pulling in prometheus_client.
./tests/test_metrics_endpoint.py:340: "prometheus_client must NOT become a runtime dependency (TR-377)"
$ grep -n "'/metrics'" scripts/router_server.py
5556:            if parsed.path == '/metrics':
```

`README.md:210-225` documents `GET /metrics` correctly and sends the reader to
this spec for the `task_router_` alias ("each ALSO emitted under the spec's
`task_router_` prefix ([docs/observability-spec.md](docs/observability-spec.md))
so the spec's V1 scrape check holds"). The reader who follows that link is told
the endpoint does not exist and that the renderer is `prometheus_client`.

Catalogue divergence measured with `metric_parity.py` (spec names vs served
families, union of :9092 and :9391): 17 of the 31 names in §3 are not exported by
any listener (admission_limit, circuit_provider_breakers_open, circuit_transitions,
code_stale, coverage_measured_ratio, outcomes_rows, probe_providers_up,
proxy_cost_usd, proxy_fallbacks, proxy_hop_attempts, proxy_hop_duration_seconds,
proxy_request_duration_seconds, proxy_upstream_errors, quota_remaining_ratio,
registry_age_days, resolve_chain_length, resolve_lanes), and 7 served families are
absent from the catalogue (chain_hops, circuit_provider_breakers, lane_coverage,
lane_excluded_total, prometheus_scrape_errors_total, resolve_errors_total,
resolve_total). Most of the unexported set is already functional work owned by
open rows TR-374 (data-plane families: coverage, staleness, registry age) and
TR-373 (resolve-lane textfile) — so the defect is NOT "these are missing"; it is
that the spec never says which of its catalogue entries shipped, and two of its
"current state" statements are now false.

## 6. Runbook probes (measured, no filing)

```
$ python3 scripts/router_deploy.py            # docs/graceful-restart.md:32
{ "ok": true, "plan": [ {service: task-router-server, port: 9092},
                        {service: task-router-proxy, port: 9391} ],
  "source": {pid: 109895, source_digest: 48e4ffbe..., source_dirty: false,
             source_revision: f146a61e...} }
rc=0
```

docs/graceful-restart.md matches the script's flags (`--apply`, `--allow-dirty`,
`--service`, `--timeout` at scripts/router_deploy.py:34-41), the ordered services
(:27-28) and the readyz/digest proofs (:62, :102). No rot.

```
$ test -f scripts/ci_gate_check.py && grep -n -- '--require-green' scripts/ci_gate_check.py
172:        "--require-green",
$ ls monitoring/task-router-alerts.yml tests/test_nohops_alert_rule.py tests/test_metrics_endpoint.py
all present
```

docs/ci-gate.md:39-41 and observability-spec.md:289-292 cite artefacts that exist.

## 7. Board dedup before filing

```
$ git show origin/main:.coding-hermes/board/tasks.jsonl | jq -r \
  'select(type=="object")|select((.id//"")|startswith("DOC-"))|select(((.title//"")+(.id//""))|test("observab|metric";"i"))|[.id,(.title//"")[0:120]]|@tsv'
DOC-32	P4 — README.md:194 calls `/openapi.json` "the OpenAPI 3.1 description of every endpoint"; measured live at b8381ef9 ...
```

No open DOC row owns the observability spec. Functional rows TR-371/372/373/374/
375/376/377 exist and are named in the filed row so the mechanism is not
re-derived.

34 DOC rows are open at the tip; every one already carries a P4/P5 label in the
title with P3 in the priority field (the mapped state this board's push boundary
requires — TR-348, `scripts/board_commit_boundary_gate.py`,
`scripts/board_priority_normalize.py`). Re-triage: nothing to do.
