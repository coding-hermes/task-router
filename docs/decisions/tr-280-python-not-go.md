# ADR TR-280: The Router Stays Python — Go Rewrite Rejected

**Status:** accepted
**Date:** 2026-10-29
**Owner:** task-router foreman
**Supersedes:** none
**Superseded by:** (none)

## Context

Owner directive 2026-10-03: "all this or we start planning for rewriting this in golang since we randomly wrote this in python but lets stick with python and work on improving the code". The task-router is a deterministic model router for the coding-hermes fleet: a task declares capability requirements, the registry finds every model that clears them, the chain = eligible models sorted by effective price, provider gates filter at resolve time. The implementation is ~15 Python scripts under `scripts/`, an installable CLI (`pip install -e .`), and a data-driven registry (`data/tables/*.jsonl` + `registry.json`).

Three measured cost drivers motivated the "why not Go?" question:

1. **Per-spawn 4.3 MB registry parse.** `router_spawn.py` loads `registry.json` (≈1.5 MB on disk, ≈4.3 MB in memory as parsed JSON) on every resolve. The scheduler Go client execs `router_spawn.py` per spawn, so this cost is paid per request.
2. **Thread-per-request serving.** `router_server.py` (the OpenAPI + proxy surface) uses stdlib `http.server.ThreadingHTTPServer` — one thread per request, no async.
3. **Per-request JSONL reads.** `router_outcomes.py`, `router_ledger.py`, and the quota/circuit/health state files are read from disk on every resolve.

## Decision

**The router stays Python.** The Go rewrite is rejected. The chosen path is to improve the Python implementation in place via the staged quality ladder (TR-267 → TR-274 async → TR-277/278/279 cache + measurement → TR-280 this ADR).

### Why not Go

The Go alternative carries real, non-trivial costs that outweigh the theoretical speedup:

1. **Full rewrite of `scripts/`.** 15 Python scripts, ~8,000 lines, with intricate JSONL handling, subprocess orchestration, env-var resolution, and fail-open contracts. A Go port is not a translation — it is a re-implementation of every behaviour, including the fail-open law (`router_spawn.py` must NEVER block the scheduler; any error → `{"error": ...}` + exit 0).
2. **pip-installed CLI + repo-relative runtime contract.** The current CLI is `pip install -e .` with a `console_scripts` entry point (`router spawn|circuit|ledger|...`). Fresh-clone proof is a first-class acceptance criterion (TR-010, TR-045, TR-046): a user on a clean venv can install and resolve in 3-5 seconds. A Go binary changes the install story (no pip, no entry point, different data-home resolution) and breaks every dogfood proven on the current path.
3. **Data-driven tables.** The registry is 11 JSONL tables (`data/tables/*.jsonl`), seeded from `router_seed.py` via an in-memory DuckDB engine, exported to both namespaces. The data shape is the product; the code is a thin resolver over it. Rewriting the resolver in Go does not change the data, but it doubles the surface that must stay in sync with the seed.
4. **Existing test suite.** 700+ pytest tests covering chain invariants, golden fixed-point heads, gate semantics, registry cross-table integrity, stdlib fallback, seed idempotency, CLI contracts, proxy wire compat, driver surfaces, and the quality artifact. A Go port starts at zero coverage.
5. **Fleet integration surface.** The scheduler Go client calls `router_spawn.py` via subprocess; the gateway points its model lanes at the proxy (`ROUTER_PROXY_UPSTREAM`); the foreman reads `router status --format json`; cron jobs call `router maintain`, `router modelsdev`, `router pricing-audit`. Every integration is wired to the Python CLI's exact stdout/stderr/exit-code contract.

### The chosen path (in place)

The cost drivers are addressed by staged Python improvements, not a rewrite:

- **TR-274 (async):** replace thread-per-request with `asyncio` + `aiohttp` in `router_server.py`.
- **TR-277 (cache — seed):** one shared session build for read-only consumers; subprocess only where inputs mutate.
- **TR-278 (cache — stress):** prove the cache at fleet concurrency (p50/p99, reload counts, zero torn reads).
- **TR-279 (cache — scans):** bounded index for JSONL reads; live appends still visible.
- **TR-281 (quality artifact):** single machine-readable quality artifact (type hints, coverage, wiring) consumed by gitreins, the board, and the UI — so improvements are measured, not asserted.
- **TR-280 (this ADR):** record the decision so no later tick re-opens a Go rewrite on a hunch.

Measurement is the acceptance criterion for each stage: if the async path does not measurably reduce p99 at fleet concurrency, the stage is not done.

## Re-open Conditions

This decision is revisited **only** if one of the following falsifiable conditions is met:

1. **p99 through the new async path (TR-274) still exceeds the caller's declared budget at the fleet's real concurrency** (measured via TR-278 stress harness at the fleet's observed request rate, not a synthetic benchmark). "Exceeds" means the proxy's p99 latency is above the caller's `x-router-deadline` header for >5% of requests over a 24h window.
2. **The async/cache work (TR-274/277/278/279) cannot land without a new runtime dependency** that is unacceptable for the fleet (e.g. a dependency that requires a system package not present on bunker agents, or a dependency that breaks the fresh-clone 3-5s install proof).
3. **A measured cost driver grows 10x** (e.g. registry parse exceeds 40 MB, or JSONL scans exceed 100k rows per request) AND the Python-side fixes are exhausted (documented in this ADR's "Exhausted Python fixes" section, to be filled as stages land).

Until one of these conditions is met, **no Go-rewrite row is filed on any board** (task-router, coding-hermes-scheduler, or any fleet project).

## Consequences

- **Positive:** the existing test suite, CLI contract, data-driven registry, and fleet integrations are preserved. Improvements are incremental and measurable.
- **Positive:** the fresh-clone proof (3-5s install, spawn EXIT=0, head from data/tables) remains the acceptance bar.
- **Negative:** the Python runtime carries a per-spawn cost that a Go binary would not. This is accepted as the price of the integration surface.
- **Negative:** the async migration (TR-274) is a non-trivial refactor of `router_server.py`. It must preserve the fail-open contract and the proxy ladder semantics (TR-081).
- **Neutral:** the Go alternative remains a valid architectural option for *new* fleet components that do not carry the existing integration surface. This ADR applies only to the task-router.

## Exhausted Python Fixes

(To be filled as TR-274/277/278/279 land. Each stage that lands without meeting its measurement target is recorded here with the measured numbers, so the re-open conditions have a factual basis.)

---

**Verification:**

```bash
$ git ls-files docs/decisions | grep -i tr-280
docs/decisions/tr-280-python-not-go.md

$ grep -ci 'go rewrite' docs/decisions/tr-280-python-not-go.md
7

$ grep -c 'TR-274' docs/decisions/tr-280-python-not-go.md
4
```

ch:trace:TR-280:adr:docs-only
