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
- **Two router surfaces disagree** — **FIXED (TR-056; re-verified 2026-09-20).**
  `status` / `validate` / `estimate` / `diff` / `metrics` / `server` / `web` now
  receive the same `ROUTING_*` / `ROUTER_STATE_DIR` exports that `spawn` gets, so
  one invocation reads one registry (`router status` and `router spawn` both
  resolve under `$TASK_ROUTER_HOME`). Keep checking `fallback_used` /
  `bootstrap` / `warnings` before trusting gates: with no seeded
  `registry.json` the resolver serves the committed `data/tables` sample and
  says so.
- **Exit codes ARE meaningful for the `router` wrapper** — **FIXED (TR-058;
  re-verified 2026-09-20):** `validate` → 1, unknown subcommand → 2, bad
  `circuit` flags → 2, bad `ledger end --outcome` → 2. Automation can trust
  `$?` (parsing the JSON payload still works and is more precise).
- **`quota` writes FLEET state, not the data home.** **Confirmed 2026-09-20:**
  with `TASK_ROUTER_HOME` set, `router quota set zai-glm "reason" <reset_at>`
  reported `state: ~/.hermes/model-router/quota-state.json` — a real gate on the
  live fleet, while the scratch home's `quota-state.json` has no
  `quota_exhausted` key at all. Deliberate (TR-060: the fleet spawn path reads
  the script default), so pass `--state-file <path>` when you must gate inside a
  scratch sandbox. Same class: `router probe` / `probefix` / `plan-sweep` read
  and write fleet locations.
- **`circuit record-failure` takes POSITIONALS.** The invocation printed in
  `docs/soft-gate-integration.md` (`--provider/--model/--reason`) exits 2.
  Correct: `router circuit record-failure <provider> <model> "<reason>" --class
  overload|quota_window|api_down|out_of_credit` (TR-084).
- **The proxy ladder does NOT advance on a successful call** — **measured
  2026-09-20 (TR-081):** a request with `x-router-max-hops: 2` returned a
  `_router.ladder` with a SINGLE entry at `hop 4` (the served pair) while the
  chain's hop 1 was never attempted, and the ladder still reads as a complete
  walk. Compare `served_by` against the chain; never treat `ladder` as the list
  of attempts.
- **The classifier key name in the docs does not exist in the fleet env.**
  `docs/integration.md` says `ROUTER_CLASSIFIER_KEY_ENV=ZAI_GLM_API_KEY`; the
  fleet `~/.hermes/.env` has `ZAI_API_KEY` / `ZAI_DEFAULT_API_KEY`. Wrong name =
  silent `complexity_source: "default"` (visible in `degrade_reason`, so check it).
- **"Seed then validate is green" is not true on a fresh install** —
  **reproduced 2026-09-20 on two boxes** (control host Python 3.11, fresh Debian
  13 / Python 3.13.5): immediately after `router seed`, `router validate` exits 1
  with `[FAIL] freshness: stale registry (warning-level): fallback_lanes.jsonl is
  0s newer than registry.json`. Second re-run in a row is green. Do not treat
  that single FAIL as a broken install (TR-082).
- **Bare profile ids dead-end in `spawn`/`/resolve`.** **FIXED (TR-059;
  re-verified 2026-09-20):** `router spawn P1_CODING --format json` now resolves
  that profile and reports `resolved_as: "profile"` with
  `hint: "use --profile P1_CODING"`.
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
- **Profile version pinning DOES NOT EXIST — retagging shadows the old id.**
  **Measured 2026-09-25 (TR-131):** with `P3_DOCS` (v1) and `P3_DOCS_V2`
  (v2, both `tag: P3_DOCS`) seeded, `--profile P3_DOCS` resolves to V2 (tag
  first), `--profile P3_DOCS:1` / `@1` → `PROFILE_NOT_FOUND`, and the v1
  row's exact id is unreachable. The README's "pinned old versions still
  resolve by version" is a false promise. Safe pattern: unique tag per
  version (`P3_DOCS_V1`, `P3_DOCS_V2`), callers reference explicit tags —
  never let two rows share a tag while you need both reachable.
- **A provider_mappings rule is a report, not routing.** **Measured
  2026-09-25 (TR-132):** a models lane under external provider `eu-openai`
  + a literal `eu-openai -> <canonical>` rule seeds with a clean
  reconciliation line, but the registry row KEEPS the external id and the
  lane appears in zero chains. A renamed lane routes only if a real
  `providers.jsonl` row also carries the external id. The seed docstring's
  "keeps resolving to its canonical registry provider" describes the report,
  not the resolver.
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
