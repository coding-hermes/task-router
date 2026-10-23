# Dogfood integration — task-router fresh install (2026-10-23, run 39)

Target commit: 6376b1f (main). Tick: task-router-dogfood-2026-10-23-00-27-32.
Lane: task-router-dogfood (stand-in workdir), executor session eb4ada7b.

## What was tested

A real fresh-install consumer pass: clone → venv → `pip install -e .` → duckdb
→ `router seed` → `router spawn` → `router status` → `router validate` →
error-path probes, on a scratch tree and on a bunker-mvp agent
(tr-df-1023, clone-equivalent tree from github.com/coding-hermes/task-router).

## Verified working (local leg, measured)

- venv 2.93s · pip -e . 3.70s (task_router-0.2.0 editable wheel) · duckdb 2.0.0 4.34s
- `router seed` 12.91s → registry.json 4640.8 KB, 3954 model_perf rows, 29 providers
- `router spawn P1_CODING --format json`: cold 0.23s / warm 0.24s
- `router status` 0.32s · `router validate` 0.19s, ALL 14 CHECKS PASS
- unknown-project spawn: rc=0 fail-open JSON error, `{"error": "project ... not in registry"}`
  (TR-235 contract holds — the scheduler can never be blocked by the router)
- resolve engine correct: profile → eligibility → price sort → gates → head hop;
  26-lane chain, gate=OPEN, head xkiro/openai/gpt-5.6-terra $0.04/task (plan-effective),
  PAYG lanes present in fallback positions per the AGENTS.md rule.

## Bunker leg (fresh machine)

Documented path ran end-to-end on the agent (clone → install → seed 22.61s →
spawn 0.26s → validate 0.12s, all green). Harness-only failure: the leg's final
`jq` summary crashed (`jq: command not found`) — the product itself succeeded.
Agent TTL'd before a second harvest; per-step numbers come from the leg
transcript (session DB proc_1619eeb92484).

## The one trap found: TR-345 (P2, board)

On a fresh install with README's own isolation recipe (`ROUTER_STATE_DIR=/tmp/…`
alone, README.md:148), spawn resolves an EMPTY chain (`gate: NO-OPEN-HOP`, every
hop excluded `policy-gate-missing-row: provider not listed in quota-state.json`)
while `router status` reports `quota.present: false` — the first-run bootstrap
(quota-state.json, README.md:81) wrote the file to the data-home ROOT
(`task_router/cli.py:443` → `paths.quota_state_path()` = `resolve_data_home()`),
but the spawn state resolver (`scripts/state_dir.py`) reads `ROUTER_STATE_DIR`,
a different path. The product's own convention works in both of its arms:

- ARM B — TASK_ROUTER_HOME only (CLI exports state_dir = data home): 26-lane chain, gate OPEN.
- ARM C — no env, isolated HOME: bootstrap lands in `$HOME/.local/share/task-router/`, real chain.

Only the mixed convention (ARM A: TASK_ROUTER_HOME + a non-data-home
ROUTER_STATE_DIR) starves the gate — and README's isolation recipe is exactly
that mix. Filed as TR-345 with fix directions (bootstrap honors ROUTER_STATE_DIR,
or the CLI exports the shared path; plus a status signal for "bootstrapped file
not where spawn reads"). Until fixed: when driving an isolated install, either
set BOTH env vars to the same tree or none — and check `gate` in the spawn
payload, not just rc.

## Perf verdict

No PERF row: every CLI operation measured 0.12–0.32s wall (spawn cold 0.23s,
warm 0.24s); `router seed` is a 12.9s one-shot admin op. Nothing a user would
feel as slow.

## Run record

DuckBrain: namespace `task-router`, key
`/Dogfood/2026-10-23/task-router-dogfood-2026-10-23-00-27-32`.
Board: `.coding-hermes/board/tasks.jsonl` row TR-345 (commit ab51bdd).
Raw local-leg logs: `/tmp/task-router-df-20261023-002732/` (ephemeral).