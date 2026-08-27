# Scheduler integration spec — TASK-ROUTER-001/002 (2026-08-26)

Filed on the coding-hermes-scheduler board by router ops. The scheduler foreman
implements; router ops owns the tools + data.

## TASK-ROUTER-001 — spawn-time model/provider resolution
- Before building the gateway POST (`/v1/responses`), call:
  `~/.hermes/venvs/board/bin/python3 ~/.hermes/scripts/router_spawn.py <project> --format json`
- Use the returned `head.provider` / `head.model` in the request body.
- Router errors → fall back to current per-project model/provider behavior (fail-open, log warning).
- PAYG (deepseek) is a LEGITIMATE fallback hop — subs first by price order; never force PAYG as
  default primary when a healthy sub exists.
- Per-project foreman profiles: routing registry `projects.profile` (P0_FORE default) → requirements.

## TASK-ROUTER-002 — circuit breaker + retry/backoff + health exclusion
- Spawn/tick failure → `router_circuit.py record-failure <provider> <model> [reason]`
- Success → `router_circuit.py record-success <provider> <model>`
- Before spawn: pairs with OPEN circuit (open_until future) or provider DOWN/SLOW in
  health-state are excluded — router_spawn.py already applies both (--health default on).
- Do NOT retry the same pair while its breaker is open — advance to the next chain hop.
- Existing fallback_model / fallback_provider / consecutive_failures columns stay the
  last-resort fallback when the router is unavailable.
- Breaker cooldowns: 5m, double per consecutive failure, cap 1h (router_circuit.py).
- Max 1 spawn attempt per hop per tick.

## Verification
- Tick spawns show the resolved model/provider in scheduler.log.
- Force a failure on a head pair → next spawn hops to chain hop 2; breaker file shows the open entry.
