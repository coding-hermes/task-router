# Failure contract — class → caller action (TR-242)

One table, enforced by tests (`tests/test_failure_contract.py`). Every failure
class the router can report maps to EXACTLY ONE caller action — the same class
must mean the same thing in the envelope (`_router`), the ledger row
(`failure_reason`), and on the client. This document is the arbitration point:
when code, ledger and this table disagree, the table wins and the code is the
bug.

Precedents this contract closes:

- **TR-096** — a `role:developer` rejection upstream burned the whole ladder: a
  client-fault class was treated as retryable, so a payload no upstream would
  accept was attempted lane after lane. Client faults are never retried now
  (rule C1).
- **TR-136** — a real 502 produced a failure envelope with `served_by` /
  `usage` / `cost` / `session` / `wall_time` all null while the ledger held the
  chain, the hops and their latencies. Standard (rule E1): a failure envelope
  is never LESS explainable than the ledger row for the same request.
- **TR-137** — every deadline kill reported `transport-failure` / status 0, so
  a slow-but-ALIVE model was indistinguishable from a dead one. Standard
  (rule T1): slow-but-alive and dead stay distinguishable end to end.

## The three actions

- `retry-after` — the condition is transient and the router says how long to
  wait. The caller MUST honour the `Retry-After` header (mirrored as
  `_router.retry_after_s` in the body) before retrying. Retrying sooner is a
  contract violation; retrying in a loop without re-evaluating is a retry storm.
- `advance-the-ladder` — internal only. The router moves to the next eligible
  lane and the caller never sees this class as an instruction: do nothing,
  wait for the router's terminal answer. If the WHOLE ladder exhausts, the
  terminal envelope (502/503 + `_router.terminal_reason`) is itself a
  stop-and-escalate object under rule E1 — the escalation is driven by the
  exhausted-ladder state, never by re-deciding the per-hop class.
- `stop-and-escalate` — do not retry. The answer was a deliberate refusal with
  a ledger row. Escalate: file a board row (or page an operator) carrying the
  terminal envelope; E1 guarantees it names the cause.

## THE TABLE

| Class | Where it comes from | What it means | Caller action | Contract notes |
|---|---|---|---|---|
| `idle-timeout` | hop attempt — `HOP_FAILURE_REASONS` / `_classify_hop_failure` (message match first), scripts/router_server.py | the gateway SSE idle deadline fired: no real event inside the idle budget — slow-but-ALIVE (TR-137) | `advance-the-ladder` | ladder records `outcome: timeout`, `timeout_kind: idle-timeout` + elapsed silence vs budget; a fleet client whose own timeout is below the router's idle budget is a detectable misconfiguration, not a router fault |
| `hop-wall-timeout` | hop attempt — `HOP_FAILURE_REASONS` / `_classify_hop_failure`, scripts/router_server.py | the transport wall budget expired — slow, not necessarily dead (TR-137) | `advance-the-ladder` | `timeout_kind: hop-wall-timeout` + the expired budget in `reason_detail`; never relabelled to transport-error |
| `transport-error` | hop attempt — `HOP_FAILURE_REASONS` / `_classify_hop_failure`, scripts/router_server.py | the lane is DEAD: connection refused / DNS / reset / TLS, no HTTP status | `advance-the-ladder` | detail carries the exception name (`Type: text`); a dead lane must never read as a timeout (T1) |
| `upstream-4xx` | hop attempt — `HOP_FAILURE_REASONS` / `_classify_hop_failure`, scripts/router_server.py | CLIENT FAULT: auth, params, wire shape — the upstream rejected the request itself | `stop-and-escalate` | NEVER retried, never burns the ladder (TR-096, rule C1); retrying an invalid payload N times is N× the cost for the same 400 — fix the request, don't repeat it |
| `upstream-5xx` | hop attempt — `HOP_FAILURE_REASONS` / `_classify_hop_failure`, scripts/router_server.py | upstream-side server error: this lane, right now | `advance-the-ladder` | the HTTP code is in `reason_detail`; a 5xx that echoes a usage block is never priced (failed hops record usage `None` on purpose) |
| `unservable-2xx` | hop attempt — 2xx carrying an error envelope and no `choices`, scripts/router_server.py (TR-120) | a green status that serves garbage | `advance-the-ladder` | counted failed so the ladder advances instead of serving an empty stream with a 200 |
| `429-capacity` | hop attempt — TR-288 taxonomy (scripts/router_hop_taxonomy.py), refined from HTTP 429 by body text | concurrency/rate pressure on THIS model — the provider keeps serving its other models | `advance-the-ladder` | BLAST RADIUS: model — demotes only the (provider, model) pair (circuit `quota_window`); the ladder never re-sends the payload to the same hop |
| `429-quota-window` | hop attempt — TR-288 taxonomy: 429 whose body names a quota/plan window (probe business codes 1308/1310/1316/1317, `access_terminated`) | the plan/weekly window is exhausted — a provider-wide, time-bounded condition | `advance-the-ladder` | BLAST RADIUS: provider — opens the provider breaker immediately (`--provider-hard`) and writes the plan-window quota gate; resets when the window refills |
| `402-no-credit` | hop attempt — TR-288 taxonomy (HTTP 402 or credit/budget body text) | credit exhausted — every hop on this provider would burn a failure | `advance-the-ladder` | BLAST RADIUS: provider — opens the provider breaker immediately + the quota gate; no retry, no threshold |
| `401-403-auth` | hop attempt — TR-288 taxonomy (HTTP 401/403 or auth body text) | auth/permission misconfig — the account, not the lane, is refused | `advance-the-ladder` | BLAST RADIUS: provider — opens the provider breaker immediately (class `api_down`); an operator fix, not a retry |
| `404-405-route` | hop attempt — TR-288 taxonomy (HTTP 404/405) | wrong endpoint or model id for this provider | `advance-the-ladder` | BLAST RADIUS: model — the named (provider, model) lane is bad; the provider's other models are untouched |
| `408-timeout` | hop attempt — TR-288 taxonomy (HTTP 408) | the upstream itself reported a timeout | `advance-the-ladder` | transient — the ladder retries the SAME hop once (`retry_policy: retry-same`), then advances |
| `5xx-overloaded` | hop attempt — TR-288 taxonomy (HTTP 5xx incl. 503) | transient server-side capacity — the probe's OVERLOADED vocabulary | `advance-the-ladder` | transient — the ladder retries the SAME hop once, then advances; the probe's one-retry 5xx rule and the ladder's now agree |
| `context-length-exceeded` | hop attempt — TR-288 taxonomy (context-length body text on 400/413) | the payload exceeds the model's context | `advance-the-ladder` | never re-sends the same payload (`retry_policy: shrink-or-skip`): shrink or skip to the next hop |
| `gated-by-policy` | envelope `exclusions` / `gate_reasons` (TR-081 self-auditing chain); contract-named class (TR-137 plan, TR-242) — no literal `failure_reason`, a gated lane is excluded BEFORE the chain exists | quota / health / circuit policy excluded the lane | `advance-the-ladder` | the ladder advances past gated lanes invisibly and `exclusions[].why` names each one; the caller does not retry the same lane; when EVERY lane is gated the terminal reason is `no-hops` |
| `overloaded` | admission refusal — `_ProxyOverloaded` → 429, `failure_reason: overloaded`, `route_outcome: rejected`, scripts/router_server.py (TR-172) | the proxy bounds work-in-flight and queue depth, and refuses rather than accept load it cannot serve | `retry-after` | 429 + `Retry-After` header + `_router.retry_after_s` (default 5s); honour it once, then re-evaluate — repeated refusals are an escalation, not a retry loop |
| `no-hops` | empty chain with the registry present — `failure_reason: no-hops`, `route_outcome: no-hops`, 503, scripts/router_server.py | nothing eligible cleared the matrix — a deliberate answer with a ledger row, never a silent drop | `stop-and-escalate` | envelope carries full E1 parity plus `exclusions` / `gate_reasons`, so the escalation names WHICH policy emptied the chain; do not retry; when the resolver itself says nothing was ever ELIGIBLE for the rating, the class is `unservable-rating` (next row) instead |
| `unservable-rating` | empty chain + the resolver payload says nothing was ever ELIGIBLE (the structured error doc / a chainless success shape with no exclusions) — `failure_reason: unservable-rating`, `route_outcome` stays `no-hops`, 503, scripts/router_server.py (TR-194) | the RATING is unsatisfiable: no lane in the registry clears its conjunction and no always-run lane could serve — the 2026-09-26 incident shape | `stop-and-escalate` | named so the escalation can tell a DATA gap (the rating outruns the fleet — TR-185; reclassify or relax the matrix, an owner decision) from a gate storm (`no-hops`); the row keeps the rating evidence (`required_categories` + `complexity_source`); do not retry |
| `registry-missing` | empty chain + resolver provenance says registry.json is absent — `failure_reason: registry-missing`, `registry_missing: true`, 503 (TR-235) | every resolve falls back to the committed sample tables; no hop is eligible | `stop-and-escalate` | fix = run `scripts/router_seed.py`, then restart / wait for the freshness cron; do not retry — the router is answering honestly about its own broken state |

## Rules

**C1 — client faults never retry (TR-096).** `upstream-4xx` is a fault in the
REQUEST (auth, params, wire shape), not in the lane. No fleet client retries
it; the router never re-attempts the same lane with the same payload. The
chain walk advances to the NEXT lane by design — fallback is not retry — and
the TR-096 burn (the same payload rejected lane after lane) is fixed at the
wire layer (`_normalize_developer_role` rewrites `role:developer` before hop
one, disclosed in `_router.developer_role_rewrites`). The caller-facing rule
stands on its own: a 4xx-classed terminal answer is final.

**T1 — slow vs dead, end to end (TR-137).** `idle-timeout` (slow-but-alive:
elapsed silence inside the idle budget) and `transport-error` (dead:
connection refused / DNS / reset / TLS) stay distinguishable at every surface —
hop reason, envelope ladder, ledger row, client terminal reason. Each names
either the elapsed silence or the expired budget (`timeout_kind`,
`reason_detail`).

**E1 — envelope parity (TR-136).** A failure envelope is never less
explainable than the ledger row for the same request. Every failure exit
carries at minimum: `terminal_reason`, the ordered `ladder` with per-hop
outcome / reason / latency, `hops_attempted` + `steps`, `usage` (+ `usage_reason`
when null), `cost_usd` (+ `cost_reason`), `session_id` (+ `gateway_session_id` /
`gateway_session_reason`), `parent_session_id`, and `wall_time_s`. A null must
carry its reason — never a bare null.

## Closure law

The class set is CLOSED. Adding a class means: emit site or
`HOP_FAILURE_REASONS` entry in code, one row in THE TABLE above, and a pin in
`tests/test_failure_contract.py` — in the same change. An unmapped class is a
loud test failure, never a silent "failed".

Fleet client obligations (the shipped clients): the scheduler's retry reads
the `Retry-After` hint; every router-pointed client states its patience and
keeps it above the router's idle budget, because a client that gives up before
the router's own deadline turns every slow hop into a client-side mystery.
