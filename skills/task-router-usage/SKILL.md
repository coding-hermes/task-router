---
name: task-router-usage
description: Use the task-router CLI to resolve model chains, gates, and pricing
version: 1.1.0
category: software-development
---

# Task-Router Usage Skill

How to actually use `task-router` (deterministic model router: task profile →
eligible provider/model pairs → price-sorted chain → runtime gates). This
skill is what an agent should read before driving the router for real.

## What it does

Resolves "which model should run this task" deterministically: a task profile
declares signed capability requirements per category (−5..+5, e.g.
`reasoning=5 debug=3`); every registry (provider, model) pair that clears ALL
requirements is sorted by plan tier then effective price; runtime gates
(quota, health, circuit breaker, diversity) then filter the chain, and you
get the first open pair plus explicit exclusion reasons. Resolve is
**fail-open**: errors come back as `{"error": ...}` with exit 0 — treat a
missing head as "use your own fallback", never as a crash.

## Entry points

- CLI: `router <subcommand>` (installed by `pip install -e .`; dispatches
  `scripts/router_<name>.py` via runpy, so the installed CLI tracks the repo).
- HTTP: `router server --mode read-only` (:9092; `ROUTER_EDIT_API_KEY=...`
  for edit mode; `POST /mcp` speaks JSON-RPC 2.0 with tools derived from
  `/openapi.json`).
- Web UI: `router web` (:9093, resolve preview + settings editor).
- Scheduler integration: `~/.hermes/scripts/router_spawn.py <project>`
  (subprocess consumer; fail-open contract).

## Run commands (the right way)

```bash
# One-time setup (fresh clone)
python3 -m venv .venv && . .venv/bin/activate
pip install -e . && pip install duckdb      # duckdb is REQUIRED for seed
export TASK_ROUTER_HOME=/tmp/tr-home        # isolate state (or leave default)
router seed                                 # build registry.json from data/tables

# Daily use
router spawn my-project --format json --quiet    # head + chain + exclusions
router spawn --profile-req 'reasoning=5 debug=3 min_context=100000' --format json
router spawn --list-profiles                     # P0_FORE, P1_CODING, ...
router status --format json                      # overview (see pitfalls!)
router validate                                  # integrity; exit 1 + issues
router estimate --project my-project --tokens-in 100000 --tokens-out 100000
router circuit status --json                     # breaker state

# After real spawns: record outcomes so breakers learn
router circuit record-failure <provider> <model> --class overload   # or api_down|out_of_credit|quota_window
router circuit record-success <provider> <model>
```

## Reading the spawn output

- `head` — the pair to use now (`provider`, `model`, `usd_1m` effective).
- `chain` — ordered fallback hops.
- `exclusions` — why everything else lost (price rank, circuit OPEN until,
  training-terms opt-out, quota). This is the audit trail; trust it over the
  stderr noise.
- `source` + `fallback_used` + `warnings` — which registry actually loaded.
  If `fallback_used: true`, registry.json was missing and committed
  `data/tables` were used (works, but stale vs your latest seed).

## Common pitfalls

- **`No module named duckdb`** — `router seed` needs duckdb; `pip install
  duckdb`. Not declared anywhere; see TR-045.
- **Hundreds of `ROUTER-MISS` stderr lines** — per-lane exclusion telemetry;
  `tier=None` means the lane has no model_tier rows (TR-043). Not an error;
  use `--quiet` and read `exclusions` in the JSON.
- **Two router surfaces disagree** (server vs CLI head, validate "registry
  missing") — they loaded different registries. Only spawn/circuit/ledger/
  maintain/seed/gaps/pricing/modelsdev/clinepass/probefix honor
  `TASK_ROUTER_HOME` via the `task_router/cli.py` wrapper; validate/status/
  estimate/diff/metrics/server/web read repo-relative or global paths
  (TR-044, still open 2026-09-16). One-line repro: from the repo cwd,
  `router status` reports `<repo>/registry.json` while `router spawn 9router`
  reports `~/.local/share/task-router/registry.json` (fallback=true).
  Force consistency with an explicit
  `ROUTING_REGISTRY=/path/to/registry.json` for scratch work, and check
  `fallback` / `bootstrap` in the spawn payload before trusting gates.
- **Exit codes are only meaningful for the scripts, not the `router`
  wrapper.** A dispatch error (unknown subcommand, invalid flag) prints the
  child's usage plus `router: <cmd> exited 2 — fail-open (coerced to 0)` and
  exits 0 — for every subcommand, including `validate` (TR-058; measured
  2026-09-16). If your automation needs real exit codes, call
  `scripts/router_<cmd>.py` directly, or parse the JSON payload instead.
  (Note: `scripts/router_seed.py` without duckdb fails loudly AND exits 1 —
  the loud-failure claim in the README is about the script behavior and is
  accurate.)
- **Bare profile ids dead-end in `spawn`/`/resolve`.** `router spawn
  P1_CODING` → `{"error": "project P1_CODING not in registry"}` even though
  P1_CODING is a valid profile. Profiles ride the `--profile` /
  `--profile-req` flags (or live under a project row in
  `data/tables/projects.jsonl`) (TR-059).
- **`ledger end --outcome` vocab is `success|failure|error`** — `ok` is
  rejected (argparse tells you immediately; no need to guess twice).
- **Scratch seeds stay out of the live DuckBrain mirror** — the `router` CLI
  exports `ROUTING_NS` under the data home for the seed subprocess, and
  `router_maintain.py` setdefaults its seed child the same way (setdefault
  semantics: an explicit `ROUTING_NS` wins; TR-045). Only direct
  `scripts/router_seed.py` runs need care: with `ROUTING_NS` unset they
  resolve under the data home's scratch ns, never the fleet mirror (TR-048)
  — set `ROUTING_NS` explicitly to place them elsewhere.
- **`quota-state.json` bootstrapped all-OPEN is sample policy** — first-run
  bootstrap writes every provider `open`; gate for real before trusting
  gates.
- **Never hand-edit `data/tables/*.jsonl`** — generated files; edits are
  clobbered by the next seed. Rebuild via `router seed` and commit.
- **Fail-open means silent degradation** — always check `fallback_used` and
  `warnings` before believing a chain reflects current policy.

## Verification

```bash
router validate            # expect: all [ok], exit 0
router spawn my-project --format json --quiet | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(d["head"])'
```

A healthy resolve: exit 0, non-empty `chain`, `fallback_used: false` after a
seed, and `source: registry.json`.
