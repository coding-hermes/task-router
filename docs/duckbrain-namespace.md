# task-router — DuckBrain namespace

The deterministic task router. A task declares
its capability profile (+/- per category, -5..+5); the database finds every
model that clears ALL requirements; the chain = those models sorted by
effective price; provider gates (quota / health / circuit) filter at resolve
time. The scheduler foreman consumes this via `~/.hermes/scripts/router_spawn.py`
(board tasks TASK-ROUTER-001/002).

## The flow
1. Task / project has a profile: categories with levels (e.g. `reasoning=5 debug=3 vision=-2`)
2. DB finds models where tier(category) >= level for EVERY requirement
3. Chain = eligible models ORDER BY (plan_tier, normalized_price × token_factor)
4. Gate at resolve time: quota-state (GATED), health-state (DOWN/SLOW), circuit-state (OPEN) → skip
5. Head = first OPEN pair. PAYG (deepseek) is a legitimate fallback hop — subs first, PAYG where price ranks it.

## Data (tables/, JSONL canonical)
- `level_defs.jsonl` — the -5..+5 scale → percentiles of each category distribution
- `model_perf.jsonl` — (provider, model, category, perf): 24 categories × 59 models
- `category_levels.jsonl` — per-category thresholds (11 levels each)
- `model_tier.jsonl` — every model's signed level per category
- `task_profiles.jsonl` + `task_profile_requirements.jsonl` — P0_FORE / P5_VISION_E2E / P7_MOCK / P9_REVIEW + P1_CODING / P2_AGENTIC / P3_DOCS / P4_SECURITY (TR-003)

## Categories (24)
Coding: code_gen, debug, refactor, terminal, mechanical, test, schema
Reasoning: reasoning, math
Agentic: agent_tick, tool_use, delegation, long_horizon, guard
Perception/UI: vision (whole picture), e2e_vision (issues around the picture), ui_frontend
Language/content: long_doc, spec_docs, creative, multilingual, review, security, mock

## State (state/, refreshed hourly + on events)
- `health-state.json` — hourly pings per provider (provider_health_probe cron)
- `circuit-state.json` — open breakers per (provider, model) with exp backoff (router_circuit.py)
- `quota-state.json` — policy gates per provider (GATED + reason)

## Chains (chains/)
- `2026-08-26.md` — per-profile chains as of seed date

## Tools
- `~/.hermes/scripts/router_spawn.py <project> [--format json]` — the runtime lookup
- `~/.hermes/scripts/router_circuit.py record-failure|record-success|status`
- `~/.hermes/scripts/provider_health_probe.py` — hourly heartbeat (cron: provider-health-probe)
- `~/.hermes/scripts/router_seed.py` — rebuild the 24-category layer (data seeds; benchmarks are truth, profile tags are estimates)

## Scheduler integration (docs/integration.md)
- TASK-ROUTER-001: spawn-time resolution via router_spawn.py
- TASK-ROUTER-002: circuit breaker + retry/backoff + health exclusion
