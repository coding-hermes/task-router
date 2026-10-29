# Task-router integration report — 2026-10-29

Target HEAD: 46c7308fc36ab73bfe15696813e56caa794b1099. This report records the direct dogfood run for scheduler tick task-router-dogfood-2026-10-29-05-59-52.

## Promise

A fresh user can install task-router, seed its committed routing tables, validate the generated registry, and resolve P1_CODING into a gated, price-ordered chain. Resolution errors are fail-open JSON with exit code 0.

## Direct user path

The clean detached worktree was installed into an isolated venv. `pip install --no-deps -e .` succeeded in 4.82s, `pip install duckdb` succeeded in 0.71s, and `router --help` exited 0. The documented `router seed` operation then failed in 0.89s with:

`router: dispatch failed: [Errno 2] No such file or directory: /tmp/task-router-df-20261029-055952/data/ns/routing/tables/providers.jsonl`

The CLI seed wrapper exports ROUTING_NS below the isolated data home, but a fresh clone has no namespace mirror there. The committed `data/tables` exists in the checkout, so the user is blocked before registry generation. `router status --format json` explicitly showed `source=data/tables`, `fallback_used=true`, and zero rows. `router validate --json` exited 1 with missing registry and profile-table errors. `router spawn P1_CODING --format json` returned rc 0 and a JSON error, not a chain.

The unknown-project contract did hold: `router spawn definitely-not-a-project --format json` returned rc 0 and a JSON error. `router circuit status --json` also returned a valid empty JSON structure.

## Performance

No PERF row was filed. CLI help was 0.04s, status 0.25s, validate 0.07s, spawn 0.10–0.13s, and the seed failure was 0.89s. Nothing completed slowly enough to be user-noticeable; the seed path is a correctness/install failure, not a justified performance profile.

## Fresh-machine install leg

The bunker-mvp host was reachable. `bunker-qa.sh launch` created agent d28dbe43 and recorded HEAD 46c7308fc36ab73bfe15696813e56caa794b1099 in `/tmp/task-router-bunker-20261029-055952.jsonl`. At close, the battery was still running: its log had HARNESS-COMPOSE=ok and HARNESS_NODE=ok, but no completion marker. `bunker-qa.sh collect --evidence /tmp/task-router-bunker-20261029-055952.jsonl` returned `RUNNING` and correctly left the agent intact. This install leg is PARTIAL and must not be reported as a pass.

## Findings

The row-ready findings are TR-1000976681059245 (fresh-clone seed path fails on absent ROUTING_NS mirror) and TR-1000976681059246 (metrics counts unresolved/error records under null provider/model keys). The read-only lane did not leave these rows on the board; detailed JSON is in `/tmp/task-router-findings.json` for the owning foreman.

## Artifacts

Run evidence: `/tmp/task-router-df-20261029-055952/run-summary.json` and `/tmp/task-router-df-20261029-055952/run/`. Bunker evidence: `/tmp/task-router-bunker-20261029-055952.jsonl`. The reusable usage correction is in `skills/task-router-usage/SKILL.md`.

## 2026-10-29 dogfood run 40 — real CLI/API use and incomplete bunker battery

Target HEAD: `46c7308fc36ab73bfe15696813e56caa794b1099`.

The isolated local consumer path worked: editable install 4.69s, duckdb 1.76s, `router seed` 32.47s, `router status --format json` 0.30s, `router validate` 0.14s with all 14 checks passing. `router spawn --profile P0_FORE --format json --quiet` returned rc=0 with `source=registry.json`, `fallback_used=false`, a 50-hop chain, and head `xkiro/qwen/qwen3.7-plus:free`; cold 0.30s and warm 0.26s. The raw-level request `reasoning=5 debug=3 min_context=100000` returned a 93-hop chain. An unknown project returned rc=0 with a JSON error, preserving fail-open behavior.

A scratch read-only API server on port 19192 returned 200 for `/health`, `/openapi.json`, `/profiles`, and `/resolve?profile=P0_FORE`; `/resolve` took 0.13s. `/health` reported loaded commit `46c7308fc36a` equal to HEAD and `stale=false`. The read-only execution-decision POST returned HTTP 403. The listening port closed after shutdown. These interactive timings are not user-noticeably slow, so no PERF row was warranted.

The bunker helper was exercised literally. A first attempt against a detached worktree was rejected as not a git repo and was not treated as product evidence. The corrected primary-root launch on bunker-las-03 spawned agent `54ebefe7`, but repo sync failed because the harness used port 2223 and the server refused it; the agent was destroyed and verified absent. The primary-root run on bunker-mvp spawned `9d5d53d3` at this HEAD. Its fresh-install cell passed the package install (`task-router-0.2.0`, duckdb 2.0.0, PyYAML 6.0.3), but the battery was `INCOMPLETE/TRUNCATED` in the CI cell before the full suite and chaos cells completed. The agent logs establish install/toolchain setup, not a complete battery pass. Teardown first refused because live operator processes remained; after terminating those owned processes, destroy succeeded and `bunker info 9d5d53d3` returned `not_found`.

No source, board, or scheduler state was changed. Existing install/harness rows already cover the observed classes (TR-045, TR-046, TR-057, TR-082, TR-085, TR-134, QA-TASKROUTER-5/6, QA-TASK-ROUTER-9); this run adds corroborating evidence only.
