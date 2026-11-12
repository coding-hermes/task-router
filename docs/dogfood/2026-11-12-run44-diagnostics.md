# Task-router dogfood diagnostics — 2026-11-12 run 44

Tick: `task-router-dogfood-2026-11-12-13-41-25`
Tested HEAD: `af84c2e6477d5ebab99d45c68dde0a6d3f685d3f`
Evidence root: `/home/kara/.hermes/cache/scratch/tr-df-20261112-134125-r2`

Commands and results

1. Target resolution and parity

- Scheduler DB row `task-router-dogfood` resolves to `/home/kara/.hermes/stand-in/dogfood/task-router`, repository URL `https://github.com/coding-hermes/task-router`, target `/home/kara/task-router`.
- `git ls-remote --symref origin HEAD` resolved the canonical branch as `main`.
- `git fetch origin main`; `git rev-list --left-right --count HEAD...origin/main` was `0 0` after fast-forwarding the product checkout from `c98efd14` to `af84c2e6`.
- No live worker, collector, guard, or judge owned the checkout. The long-lived `router_server.py` processes were classified as read-only daemons; the old `next-index-3357414.lock` is a stale file from 2026-09-17 with no holder.

2. Fresh local install and seeded use

- Scratch clone: `/home/kara/.hermes/cache/scratch/tr-df-20261112-134125-r2/current`.
- `pip install -e .`: 1.37 s. `pip install duckdb`: 0.86 s.
- `router seed --allow-unsatisfiable`: exit 0, 12.80 s; generated `state-current/registry.json` and reported the known six unroutable math ratings held by disabled/unpriced `ollama-cloud/qwen3.5:397b`.
- `router validate --json`: exit 0; `valid=true`, empty issues.
- Effort low/high/max each returned exit 0, a non-empty head with the requested `reasoning_effort`, `source=registry.json`, `fallback_used=false`, `measured_lanes=5`, and `proxy_lanes=98`.
- Unknown `bogus` effort returned exit 0, emitted `WARNING: --effort 'bogus' ignored ...`, and returned an effort-absent default result. This was recorded as observed fail-open behavior, not filed as a new defect.

3. Served-plane evidence

- `GET http://127.0.0.1:9092/health` returned `stale=true`, loaded commit `6cbc6aefa573`, loaded at `2026-11-11T19:31:52+00:00`, while the repo was `af84c2e6`.
- `GET /resolve?project=task-router&effort=high` returned HTTP 200 with `effort.requested=null`, `source=absent`, `warnings=[]`; this is the live reproduction owned by `TR-1000976681059253`.
- The served process source digest matched its own on-disk stale copy, not the fresh product HEAD. This is deployment drift owned by `TR-1000976681059254`.

4. Performance evidence

- Loadavg beside the cold reading: `3.31 4.26 4.97`.
- Cold `router spawn task-router --effort high --format json --quiet`: 0.06 s wall.
- Warm `hyperfine --warmup 2 --runs 6`: 62.4 ms ± 2.6 ms, range 58.5–65.8 ms.
- Warm `router status --format json`: 241.5 ms ± 5.6 ms.
- No user-noticeable latency and no profiling pass; no PERF row.

5. Prescribed install harness

- `bunker --version`: 0.3.0.
- `~/.bunker/config.yaml`: absent.
- `bunker status`: exit 1, `no servers configured; run 'bunker connect' first`.
- `PATH=/home/kara/.hermes/scripts:$PATH BUNKER_QA_SERVER=bunker-mvp bunker-qa.sh launch /home/kara/task-router`: exit 2, `target host bunker-mvp not configured in ~/.bunker/config.yaml -- refusing to run`.
- No agent was spawned, so no teardown census was needed. Row `TR-1000976681059256` records this explicit skipped bunker leg.

6. Board write proof

- Minter command: `python3 ~/.hermes/scripts/board_next_id.py /home/kara/task-router/.coding-hermes/board/tasks.jsonl TR --count 1` returned `TR-1000976681059256`.
- Pre-append exact-id count was zero. Post-append `jq` count is one task row and one `task_created` event; the whole row contains no tick source field, but its reasoning includes the exact tick id.
- Board size after append: 520 task lines and 1050 event lines. Duplicate task-id count remains the standing value 5; the new id is unique.
- `boardctl -C /home/kara/task-router validate` exits 1 with `RESULT: FAIL (458 error(s), 603 warning(s))`. The errors are the pre-existing mixed event-id ordering debt; the new row is not named in validator errors. This is not reported as a green validator.

No product source files were edited.
