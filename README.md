# task-router

Deterministic task-profile → chain resolution for the **coding-hermes fleet**.
A task declares its capability profile (signed levels −5..+5 per category); the
registry finds every model that clears ALL requirements; the chain = those
models sorted by effective price; provider gates (quota / health / circuit)
filter at resolve time. Built with Bane 2026-08-26.

## The flow

1. Task / project has a profile: categories with levels (e.g. `reasoning=5 debug=3 vision=-2`).
2. Registry finds models where `tier(category) >= level` for EVERY requirement (dominance rule — wasting capability is allowed, lacking it is not).
3. Chain = eligible models `ORDER BY (plan_tier, normalized_price × token_factor)`.
4. Gates apply at **resolve time, never in the chain**: quota-state (GATED), health-state (DOWN/SLOW), circuit-state (OPEN until timestamp) → skipped with reasons.
5. Head = first OPEN pair. PAYG (deepseek) is a **legitimate fallback hop** — subs first, PAYG where price ranks it.

Scale = **per-category percentiles**, not absolute scores: `−5`→q01 … `0`→q50 … `+5`→q99 of that category's perf distribution. Flat thresholds are WRONG (a 0.90 `++` was unreachable in `schema`, trivial in `mock`).

## Repo layout

| Path | What |
|---|---|
| `scripts/router_spawn.py` | THE runtime lookup — project → profile → chain → gates → head |
| `scripts/router_circuit.py` | circuit-breaker state per (provider, model), exp backoff 5m→1h |
| `scripts/provider_health_probe.py` | hourly provider pings (cron: provider-health-probe) |
| `scripts/router_seed.py` | rebuild the 24-category layer from perf data (routing ns) |
| `docs/integration.md` | scheduler integration spec (TASK-ROUTER-001/002) |
| `docs/chains-2026-08-26.md` | per-profile chains as of seed date |
| `docs/duckbrain-namespace.md` | the `task-router` DuckBrain namespace anatomy |
| `.coding-hermes/board/` | JSONL workboard (canonical, git-tracked) |

## The 24 categories

- Coding: `code_gen, debug, refactor, terminal, mechanical, test, schema`
- Reasoning: `reasoning, math`
- Agentic: `agent_tick, tool_use, delegation, long_horizon, guard`
- Perception/UI: `vision` (whole picture), `e2e_vision` (issues around the picture), `ui_frontend`
- Language/content: `long_doc, spec_docs, creative, multilingual, review, security, mock`

Seeded profiles: `P0_FORE` (default foreman), `P5_VISION_E2E`, `P7_MOCK`, `P9_REVIEW`.

## Runtime usage

```bash
# THE lookup — project → profile → chain → gates → head
~/.hermes/venvs/board/bin/python3 scripts/router_spawn.py <project> --format json
~/.hermes/venvs/board/bin/python3 scripts/router_spawn.py --profile-req 'reasoning=5 debug=3 vision=-2'
~/.hermes/venvs/board/bin/python3 scripts/router_spawn.py --list-profiles
~/.hermes/venvs/board/bin/python3 scripts/router_circuit.py status
~/.hermes/venvs/board/bin/python3 scripts/provider_health_probe.py
```

`router_spawn.py` is **fail-open**: any error → `{"error": ...}` + exit 0. The
scheduler must never be blocked by the router.

**Runtime installs** (this host): the live copies the scheduler + cron call are
`~/.hermes/scripts/`. This repo is the canonical source — keep them in sync
(see board task TR-004 for the symlink wiring).

## Data & state

- **Registry (DuckDB)**: `~/reports-repo/routing.duckdb` (`ROUTING_DB` env override)
- **Git-tracked tables + seed script**: `~/duckbrain/namespaces/routing/` (tables/, scripts/)
- **Thread-facing namespace**: `~/duckbrain/namespaces/task-router/` (README, tables/, state/, chains/, docs/)
- **Live state**: `~/.hermes/model-router/` (health-state.json, health.jsonl, circuit-state.json, quota-state.json, snapshot.json, ledger.jsonl)

## Quality gates

- `.gitreins/config.yaml` — secrets + tests guard (test_mode diff), deepseek-v4-flash evaluator, legacy pipeline (stages: []).
- Tests: `python3 -m pytest -q tests/` (smoke suite; skips if duckdb unavailable). Guard wrapper: `scripts/gitreins-guard-tests.sh` (SKIP when no tests/deps, FAIL only when tests actually run and fail).
- Commit style: Conventional Commits + `Co-authored-by: Alexis Okuwa <wojonstech@gmail.com>` trailer.

## Board

`.coding-hermes/board/` JSONL canonical (tasks.jsonl + events.jsonl git-tracked;
board.db / *.parquet untracked rebuildable caches). Fixtures: NEVER-DONE,
E2E-001, GITREINS-JUDGE. Task IDs: `TR-00N`.

## Scheduler integration

- TASK-ROUTER-001: spawn-time resolution via `router_spawn.py` (coding-hermes-scheduler board — worker live 2026-08-27)
- TASK-ROUTER-002: circuit breaker + retry/backoff + health exclusion (scheduler board)
- This project's own board tracks router-ops work (provider calibration, data quality, profile library, maintenance automation).

## Bane policies (do not contradict)

1. **PAYG (deepseek) is a legitimate fallback hop** — stays in chains where price ranks it; must NOT be the default primary when a healthy sub exists.
2. **The consuming project does its own integration** — router ops builds tools + data + spec; the scheduler foreman implements wiring via its board.
3. **vision ≠ e2e_vision** — two distinct axes.
4. **Categories are capability axes, not workloads** — workloads become profiles (rows of requirements).
