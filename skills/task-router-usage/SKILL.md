---
name: task-router-usage
description: Use the task-router CLI to resolve model chains, gates, and pricing
version: 1.0.0
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
  maintain/seed/gaps/pricing/modelsdev/clinepass honor `TASK_ROUTER_HOME`;
  validate/status/estimate/diff/metrics/server/web read repo-relative or
  global paths (TR-044). Force consistency with an explicit
  `ROUTING_REGISTRY=/path/to/registry.json` for scratch work.
- **Scratch `router seed` on the fleet control host exports into the live
  DuckBrain mirror** (`ROUTING_NS` default is hardcoded; the CLI does not
  redirect it). Set `ROUTING_NS` explicitly for scratch seeds (TR-045).
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
