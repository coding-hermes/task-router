# soft gate integration

This document describes the caller contract for the **soft concurrency gate**
(TR-032) in the task-router. The gate is intentionally **not enabled by default**;
the scheduler-side integration documented here must be wired in the caller
(the `coding-hermes-scheduler` Go process, per AGENTS.md) before the gate has
any effect.

## Goal

When a gateway request fails with a concurrency/rate-limit signal (HTTP 429,
`quota_window`, `overload`), the scheduler should:

1. Record the failure against the lane that was attempted.
2. Re-resolve the task through `router_spawn.py` so the next chain entry
   becomes the head.
3. Continue until a non-busy lane is found or the fallback lane is reached.

No hard rejection from in-flight counts is ever issued; `router_spawn.py` always
exits 0 with a chain (possibly the always-run fallback chain).

## Gate semantics

- `quota-state.json` may contain `"soft_gate": true`. If absent or `false`, the
  gate is **off** and in-flight counts have **no effect** on resolve output.
- When on, `router_spawn.py` reads `ledger.jsonl` via `scripts/router_ledger.py`
  semantics: a trace whose latest row is `outcome='started'` and is newer than
  the 30-minute stale window counts as in-flight.
- Per-model limit sources, in order of precedence:
  1. `quota-state.json["models"]["<provider>/<model>"]["concurrency_limit"]`
  2. `quota-state.json["diversity"]["model_concurrency_limit"]`
  3. No limit (model always eligible).
- A model at or over its limit is excluded from the resolved chain with reason
  `model busy (N in-flight >= limit L)`. Other models from the same provider
  remain eligible.

## Lifecycle commands

### 1. Mark a session as starting

```bash
ROUTER_STATE_DIR=/var/lib/hermes/model-router \
  ~/.hermes/venvs/board/bin/python3 /home/kara/task-router/scripts/router_ledger.py start "<session-id>"
```

The scheduler must call this immediately before handing a job to a gateway.

### 2. Mark a session as complete

```bash
ROUTER_STATE_DIR=/var/lib/hermes/model-router \
  ~/.hermes/venvs/board/bin/python3 /home/kara/task-router/scripts/router_ledger.py end "<session-id>" ok
```

Call this when the job finishes normally. If it failed, replace `ok` with
`fail` (or the failure class).

### 3. Record a concurrency/rate-limit failure and re-resolve

When a gateway call returns HTTP 429 or an `overload`/`quota_window` error:

```bash
export ROUTER_STATE_DIR=/var/lib/hermes/model-router

# Record the failure against the lane that was attempted
~/.hermes/venvs/board/bin/python3 /home/kara/task-router/scripts/router_circuit.py \
  record-failure --provider "<provider>" --model "<model>" \
    --class {overload|quota_window} \
    [--reason "optional short reason"]

# Re-resolve; the previously attempted lane should now be excluded and the head
# will advance to the next chain entry
~/.hermes/venvs/board/bin/python3 /home/kara/task-router/scripts/router_spawn.py \
  --profile P1_CODING --format json
```

Use `--class overload` for HTTP 429/overcapacity and `--class quota_window` for
explicit quota-window failures. These are short-cooldown breaker classes
(120 s and 300 s respectively); `api_down` and `out_of_credit` are long
provider-level cooldowns and are **not** appropriate for transient concurrency.

### 4. Provider-level breaker (TR-014)

`router_circuit.py record-failure --class api_down` increments a cross-model
counter in `circuit-state.json["v2"]["provider_breakers"]["<provider>"]`.
When the counter reaches 3, a provider-level breaker opens for 1800 s and
`router_spawn.py` excludes **every** lane of that provider with reason
`circuit OPEN (provider-level, api_down) until <ts>`.

## Important constraints

- `router_spawn.py` always exits 0; a missing or corrupt `quota-state.json`,
  `circuit-state.json`, or `ledger.jsonl` is treated as fail-open, not fatal.
- The scheduler must **not** use in-flight counts to reject jobs before calling
  `router_spawn.py`; the strict busy-count pre-limit is **off** by default and
  will never hard-reject.
- The `soft_gate` knob is read from `quota-state.json`; keep the default
  `false` until the scheduler has wired `router_ledger.py` start/end calls
  around all spawns.

## Cross-repo note

The spawn-ledger integration is the responsibility of the scheduler process; see
AGENTS.md: task-router owns the gate state and the resolve contract, but does
not implement scheduler-side caller wiring.

## References

- `scripts/router_spawn.py` — resolve engine (provider-level exclusion + soft gate)
- `scripts/router_circuit.py` — circuit breaker v2 (provider-level + pair-level)
- `scripts/router_ledger.py` — session ledger start/end semantics
- `tests/test_soft_gate.py` — acceptance tests for TR-032 + TR-014 wiring
