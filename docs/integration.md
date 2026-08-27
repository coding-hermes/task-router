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

## Diversity + concurrency (TR-007, Bane design 2026-08-27)

Two diversity knobs prune the price-ordered eligible chain AFTER gates; both are
global defaults with per-profile overrides, and no caps configured = output
identical to pre-TR-007:

- Where the knobs live:
  - Global: `~/.hermes/model-router/quota-state.json` → optional `"diversity"`
    key: `{"max_consecutive_per_provider": N|null, "max_total_per_provider":
    N|null, "model_concurrency_limit": N|null}` (`null`/absent = unbounded) and
    optional per-pair limits under `"models"`:
    `{"<provider>/<model>": {"concurrency_limit": N}}` (explicit pair limit
    beats the global model_concurrency_limit).
  - Per-profile overrides: `task_profiles.max_consecutive_per_provider /
    max_total_per_provider` columns (NULL = fall back to global). Profile beats
    global.
- Semantics: applied as PRUNING — router_spawn.py walks the price-ordered
  survivor chain, drops violators, reports each drop in `exclusions` +
  `gate_reasons` (`'consecutive cap N'`, `'chain cap N'`). Price order among
  survivors is preserved. NEVER a provider-wide pre-filter.
- Busy-skip semantics (per-MODEL, not per-provider): a model at its concurrency
  limit is skipped individually like a circuit exclusion (`'model busy (k
  in-flight >= limit N)'`); the provider's other models stay eligible. A busy
  model NEVER removes a cheap provider whose sibling is free.
- Ledger contract for the scheduler (TASK-ROUTER-002 call side):
  1. Before spawn: `router_ledger.py start --provider P --model M [--project X]
     [--profile R] [--hop N] [--reason R]` → prints a trace_id on stdout;
     capture it.
  2. After the spawn settles: `router_ledger.py end --trace-id T --outcome
     success|failure|error [--latency-ms N] [--error-class E] [--tokens-in N]
     [--tokens-out N]`. Invalid outcome exits 2; unknown trace_id warns but
     still appends (fail-open).
  3. In-flight counts derive from ledger.jsonl: a trace whose last row is
     outcome='started' is in flight; 'started' rows older than 30 minutes are
     stale (crash without `end`) and do not count. `router_ledger.py status
     [--provider P] [--json]` shows per-(provider, model) in-flight + last
     outcome.
- Every routed call should get a ledger row (schema v2 subset — fields present
  only when known; never fabricated).

## Verification
- Tick spawns show the resolved model/provider in scheduler.log.
- Force a failure on a head pair → next spawn hops to chain hop 2; breaker file shows the open entry.
