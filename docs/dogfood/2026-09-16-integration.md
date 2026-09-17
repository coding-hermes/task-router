# Dogfood integration report — task-router (2026-09-16/17)

Second field test (first: 2026-09-12, SHIPPABLE). The cron re-picked the
project, so this run went **delta-deep**: the surfaces added or changed since
09-12 (xKiro importer + 115 lanes, plan-tier economics, MCP bridge, web UI,
ledger lifecycle), plus the mandatory ephemeral-bunker install leg. Real use,
not test scripts: live resolves against production routing data, real circuit
and ledger transitions on synthetic pairs, real MCP client calls, and a
fresh-user install on a bare Debian agent.

## Promise under test

> A user can resolve any task to a price-ordered, gate-filtered model chain
> via `pip install -e .` + `router spawn`, with fail-open runtime, a
> read-only API/MCP server on :9092, a settings web UI on :9093, and state
> under a configurable data home.

## Verdict: SHIPPABLE (with P1 consistency debt)

The product is real and used: 1.18M hops in the metrics store, chains that
carry xKiro plan-tier economics end to end, an MCP bridge an external client
can actually drive, and a 5-second fresh install. The debt is that the
subcommands still disagree about which registry they read (TR-044 family,
now with a live repro), stderr telemetry still floods ad-hoc resolves, and
the wrapper's exit-code coercion hides CLI misuse from scripts. All filed as
board rows; none block the core workflow.

## Environment

- Control host: repo checkout at `e5eea36` (foreman committed `0a8df46`
  mid-run — healthy active foreman), board venv
  `~/.hermes/venvs/board/bin/router`, live API server already on :9092
  (read-only, pid 1161360).
- Bunker leg: las-bunker-03 agent `fde15ade`, ttl 2h, bare Debian user,
  Python 3.13.5, no sudo, no toolchains, no docker-compose plugin.
  Destroyed after the run (verified: `No agents found`).

## What was actually done and what happened

### Control-host real use

| Step | Command | Result |
|---|---|---|
| Overview | `router status --format json` | 119ms; 1047 rows, 415 active, 405 priced, 26 providers; health 18 ok / 8 prov down; quota 14 open / 4 gated |
| Ad-hoc resolve | `router spawn --profile-req 'reasoning=5 debug=3 min_context=100000' --format json` | valid chain (zai-glm head, 4 hops), full exclusions; **stderr flood: 760 lines, 479 tier=None** (TR-055) |
| Project resolve | `router spawn 9router --format json` + `GET /resolve?project=9router` | CLI and live server agree: head `clinepass/stealth/union-alpha` @ $0.0 (the TR-053 wired head), 4 hops, 14 exclusions |
| Estimate | `router estimate --project 9router --tokens-in 100000 --tokens-out 100000` | head cost $0, `price_basis` strings clean, subscription/PAYG annotated |
| Circuit loop | `record-failure dogfood-test test-model --class overload` → `record-success` → `clear` | OPEN 120s → CLOSED → cleared; status text/JSON consistent |
| Ledger loop | `ledger start --provider dogfood-test --model test-model ...` → `end --outcome success` | trace `tr-51c593e3`, in_flight 1 → 0, `last=success` |
| MCP bridge | `POST /mcp` initialize / tools/list / tools/call against the live :9092 | 15 tools mechanically derived from OpenAPI; `resolve` tool call round-trips; payload identical to REST `/resolve` |
| Read-only guard | `POST /circuit/record` on :9092 | HTTP 403 `{"error": "read-only mode"}` — mutations blocked without edit mode |
| Web UI | `TASK_ROUTER_HOME=<scratch> router web` on :9093 | 200 OK, title renders; `/api/preview?project=9router` returns the **same head as CLI/server** — "preview == reality" holds |
| Validate/gaps | `router validate --json`, `router gaps --top 5` | validate: 1 warning-level freshness issue (fallback_lanes 0s newer than registry); gaps: honest per-lane missing-data report |
| CLI/API arg parity | `router spawn P1_CODING` (CLI and `/resolve?project=`) | both dead-end with "project P1_CODING not in registry" — no hint that `--profile` is the right flag (TR-059) |
| Exit codes | `plan-sweep --dry-run` (invalid flag), `unknown-cmd`, `validate`, `spawn bad-project` | exits 0 / 0 / 1 / 0 — wrapper coercion hides argparse errors (TR-058) |

### Bunker fresh-user install leg (las-bunker-03, agent fde15ade)

| Step | Command | Result |
|---|---|---|
| Clone | `git clone https://github.com/coding-hermes/task-router.git ~/app` | ok, HEAD 0a8df46 (public clone, no credentials needed) |
| Install | `python3 -m venv .venv && .venv/bin/pip install -e .` | **INSTALL_BASE_SECONDS=5** |
| No-duckdb behavior | `router spawn --list-profiles`, `router spawn --profile-req …` | both work off committed `data/tables` (JSONL fallback); `router seed` fails **loudly and exits 1** exactly as the README documents |
| Fresh data home | `TASK_ROUTER_HOME=~/tr-home router status` | bootstrapped `quota-state.json` (all-open sample policy), registry marked unavailable with a clear note |
| duckdb | `.venv/bin/pip install duckdb` | 14s |
| Seed | `TASK_ROUTER_HOME=~/tr-home router seed` | 8s, wrote `~/tr-home/registry.json` (1.59MB) |
| First resolve | `router spawn my-project --format json` | 18-hop chain, head `clinepass/stealth/union-alpha` — **identical head to production** |
| Validate paradox | `router validate` | **[FAIL] registry.exists: missing ~/app/registry.json** — a correctly-seeded data-home install fails the integrity check (TR-057) |
| Estimate | `router estimate --project 9router …` | head `None` with no seeded-repo registry — same data-home family (TR-056) |
| Destroy | `bunker destroy fde15ade --server bunker-las-03` | ok, no agents left behind |

**Installability verdict:** total clone → first real resolve ≈ **27s**
(5s install + 14s duckdb + 8s seed) on a bare Python 3.13 box with zero
preinstalled tooling. The stdlib-only runtime claim is real. One docs-level
note: a compose path would need the compose plugin (not present on bare
Debian), but this project documents none — pip is the whole story.

### Test suite

`pytest -q tests/` on the control host: **321 passed in 321s**
(2026-09-12: 290 in 220s → +31 tests, +46% duration; still under the 10-minute
pain threshold, worth watching as xKiro-scale catalogs grow).

## New findings → board rows (TR-055…TR-059, filed 2026-09-17)

| ID | P | One line |
|---|---|---|
| TR-055 | P1 | ROUTER-MISS stderr flood: 760 lines / 479 tier=None per ad-hoc resolve (improved from 1004/724, goal 0) |
| TR-056 | P1 | wrapper exports data-home env for only 10/20 subcommands → status vs spawn read different registries from the same cwd (live repro) |
| TR-057 | P2 | seeded data-home install FAILS `router validate` (validate looks at `<repo>/registry.json`) — caught on the bunker leg |
| TR-058 | P2 | wrapper coerces all subcommand exits to 0, hiding argparse misuse from scripts (fail-open leaked beyond the fail-open trio) |
| TR-059 | P2 | bare profile id in `spawn`/`/resolve` dead-ends without a "use --profile" hint |

Corrections to the 2026-09-12 record: `router seed` without duckdb **does**
exit 1 (re-measured without a pipe — the earlier `exit 0` reading was
pipe-masked); the diagnostics' claim that the coercion is limited to
fail-open commands is wrong in practice — it covers every dispatched
subcommand (TR-058).

## Time-to-first-success

- Fresh machine (bunker): ~27s clone-to-resolve (with duckdb+seed); ~6s
  without seed (JSONL fallback path).
- Control host: first `router status` + resolve inside 5 seconds.

## Friction count: 7

1. stderr flood on every ad-hoc resolve (TR-055).
2. Two router surfaces disagreed about which registry they loaded (TR-056).
3. Fresh install failed `router validate` (TR-057).
4. Invalid flag / unknown subcommand exit 0 — unusable in scripts (TR-058).
5. `P1_CODING` as project arg dead-ends on both CLI and API (TR-059).
6. `--outcome` vocab is success/failure/error — `ok` rejected (minor,
   self-explanatory from the error).
7. `router web` ignores `--port`-shaped env nudges and always binds :9093 in
   data-home mode (had to use the flag on the script; cosmetic).

## What this project is worth

The chain is the product and the chain is honest: eligibility, plan-tier
ordering, gates, exclusions, and provenance all behaved exactly as documented
across three surfaces (CLI, REST/MCP, web preview) and two machines. The
scheduler already runs on it. The debt is consistency, not capability.
