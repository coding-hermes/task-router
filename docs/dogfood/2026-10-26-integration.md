# Task-router dogfood integration report — 2026-10-26

Target: task-router HEAD f75e1bace20cdffb46ace69b5246fcf2415fc808.

Promise: a user can install task-router, seed its registry, and resolve a task profile into a gated, price-ordered provider/model chain; resolution failures remain fail-open JSON errors instead of blocking the caller.

Real-use control run

An isolated scratch HOME, TASK_ROUTER_HOME, ROUTER_STATE_DIR, virtual environment, and data directory were used. The editable package and duckdb 2.0.0 were installed with uv. `router seed` completed rc=0 in 11.85s. `router spawn --profile P0_FORE --format json --quiet` returned rc=0 with a non-empty 53-hop chain and `source=registry.json`, `fallback_used=false`; cold wall time was 0.17s and the warm repeat was 0.12s. `router status --format json` and `router validate` returned rc=0. An unknown project returned rc=0 with JSON `error: project definitely-not-a-project not in registry`, confirming the fail-open contract. `router spawn --list-profiles` also returned rc=0.

API control run

A separate read-only server was started on 127.0.0.1:19192 against the isolated state. `/health`, `/openapi.json`, `/profiles`, and `/resolve?profile=P0_FORE` returned HTTP 200. The health payload reported loaded commit f75e1bace20c equal to the repo HEAD and `ready=true`. The resolve endpoint returned a concrete head and 53-hop chain. Timings: health 0.03s, OpenAPI 0.00s, profiles 0.00s, resolve 0.23s. The server child was killed and a separate probe confirmed it was gone.

Install leg

The standard bunker QA battery was launched with `BUNKER_QA_SERVER=bunker-mvp` against `/home/kara/task-router`. Bunker2 and bunker-mvp were reachable with active bunkerd and Docker; bunker3 and bunker4 timed out. Agent 2bd9809a was created on bunker-mvp, the battery ran, evidence was collected, and the agent was destroyed. Evidence: `/tmp/task-router-bunker-20261026.jsonl`.

The fresh-install cell is FAIL, not a silent pass: `install rc=0 but no artifact found (toolsd not in PATH, PREFIX/bin/toolsd missing, or zero-size); Successfully installed duckdb-2.0.0 pyyaml-6.0.3 task-router-0.2.0`. This is a harness/product-contract mismatch: task-router installs the `router` console entry point, while the generic battery only accepts an artifact named `toolsd`. The evidence also reports the native suite failure `tests/test_board_complete_gate.py::test_live_probe_refuses_zero_tags_zero_releases`; act failed because the Docker image was absent, and the resource cell hit the same test. These failures are not re-filed here because the board already has open QA-TASKROUTER-6 for the zero-tags refusal; the generic artifact-name failure needs a separate harness/QA row if not already owned.

Performance judgment

Interactive operations were below a noticeable threshold: spawn 0.17s cold / 0.12s warm, status 0.18s, validate 0.06s, and API resolve 0.23s. Seed is a one-shot 11.85s administrative operation. No PERF row was filed because the user-visible interactive path is comfortably fast and no profile is justified.

Value verdict

PROMISING-BUT-ROUGH. The documented local workflow works and is useful, with low friction and no source reading required. Fail-open behavior, seeded resolution, API reads, and teardown all worked. Installability is not a clean pass because the bunker battery misgrades the valid `router` artifact as absent and the native battery contains the existing zero-tags failure. The shared live :9092 server was stale (loaded 187bcae while the target repo was f75e1ba); it was not used as product evidence.

Evidence paths: `/tmp/task-router-dogfood-scratch-20261026/run.log`, `/tmp/task-router-bunker-20261026.jsonl`, `/tmp/task-router-api-run.sh`.