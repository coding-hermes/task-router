# Dogfood run 48 diagnostics — 2026-11-27

Tick: `task-router-dogfood-2026-11-27-20-50-20`. Product root: `/home/kara/task-router-dogfood-20261127` at `91888bc89ce3fd7e8d4fef4e3989c95c37aade1d`. Scratch evidence: `/home/kara/.hermes/cache/scratch/task-router-dogfood-20261127-run/`.

## Consumer setup

The tested tree was a fresh archive extracted outside the product checkout. A new venv was created. `pip install -e .` succeeded, and `router --help` returned the installed command index. The initial run used `TASK_ROUTER_HOME` and `ROUTER_STATE_DIR` under the scratch directory, so no production routing state was modified.

## Errors and lessons

1. `router seed` without DuckDB exited 1 in 0.09 s and emitted no text. The README documents DuckDB as the only seed dependency, but the failure is not the documented loud `router: dispatch failed: No module named 'duckdb'` shape. This was not filed as a new row because the board already carries the same fresh-install/documentation class (`README-1000811586570595`).
2. After installing DuckDB 2.0.1, `router seed` completed in 55.55 s, wrote `/home/kara/.hermes/cache/scratch/task-router-dogfood-20261127-run/home/registry.json` (10.5 MB), and returned `SATISFIABILITY OK`. `router validate --json` then returned `valid=true` with no issues.
3. The seeded spawn resolved 21 hops in 0.53–0.64 s warm. Its only warning was the documented inactive spawn ledger: `ledger.jsonl has no trace rows`, so model-busy enforcement was not exercised.
4. The cost estimate JSON was internally complete: 21 priced hops, total `$0.035195`, 2 PAYG, 18 subscription, 1 unknown billing. The human renderer printed `chain total (all 21 priced hops): $0.0352 · payg 2 · subscription 18` and did not say that one hop had unknown billing. A new user reading the text summary cannot reconcile the billing categories to the chain count, even though the JSON carries the distinction. This is the new finding `TR-1000976681059-1`.
5. `router chain-run --dry-run --max-hops 3` returned rc 0 with `success=false` and `final=null`. This is a planned dry run, not a new reproduction of the existing exhaustion exit-code finding. Existing `TR-1000976681059264` remains the owner.
6. The live service was independently checked for context: `/health` returned HTTP 200 but `stale=true`, loaded commit `78a6b14c0c6a`, repository commit `3b6e14bbd366`, and `/status` returned HTTP 200. This run did not mutate or restart the service; the stale-serving issue already has open board ownership (`TR-1000976681059254`).

## Performance

The headline CLI operation was measured with `/usr/bin/time`: seeded warm `spawn` 0.53, 0.63, and 0.64 s; estimate 1.03 s; chain dry-run 0.59 s. The host was at loadavg `18.45 16.88 16.58` on 16 cores during the first battery, so these are contextual wall numbers. No new PERF row was filed: the existing `PERF-2` already owns the user-visible outcomes-ledger tax, and this run did not reproduce a distinct hot path or provide a comparable same-configuration A/B. No flamegraph was justified for the subsecond spawn path.

## Install evidence

The prescribed detached install leg was launched with `bunker-qa.sh launch` against a fresh clone at the tested commit, using `bunker-las-03`. It created agent `9e2371cf`. Collection at the close of this tick reached 10 JSONL rows and remained RUNNING: target binding, launch, toolchain bootstrap, fresh-install, and lint N/A. The exact evidence path is `/home/kara/.hermes/cache/scratch/bunker-qa-evidence-task-router-20261127-2050.jsonl`. The agent was left intact; tests, UI, chaos, and teardown are not verified.

The bunker host was reachable and had 86% disk at the initial census, so filing `SKIPPED-install-bunker` would have been incorrect. A partial/in-flight install leg is not a pass.
