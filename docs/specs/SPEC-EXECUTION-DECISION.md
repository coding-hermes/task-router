# SPEC-EXECUTION-DECISION — just-in-time route receipt (ACP-ROUTER-001)

**Status:** v1 closed contract, 2026-10-17
**Authority:** `/home/kara/bunker-specs/hermes-foreman-a2a-order-flow-2026-10-16.md`
(E2 seam), goal `AUTO-CODE-001` G2.
**Code:** `scripts/router_decision.py` (Python + CLI), `POST
/api/v1/execution-decision` and `POST /api/v1/execution-decision/verify`
(HTTP + OpenAPI + MCP), one resolver: `router_spawn.resolve()`.

## What this is

Immediately before every execution attempt the Hermes foreman sends Task
Router the complete routing-relevant task/step context and receives a
**canonical, digestable, expiring route receipt**: the exact ordered
model/provider chain, the exact exclusions, the gate evidence used, and the
policy-bound expiry. A stale decision is re-resolved, never reused.

## Authority boundary (non-negotiable)

Task Router remains **model/provider-only**. This surface:

- never chooses or names a harness, connector, tool, workspace, Bunker, Crier
  recipient or board row (the receipt's `constraints.selected_*` are explicit
  nulls — a non-null value would be a forged selection);
- never dispatches, creates sessions, writes outcomes, appends metrics or
  mutates live routing state (a decision call is read-only end to end);
- never reorders the resolver's candidates (`candidates` keep resolver order
  and hop numbers);
- never serves the requirements-unmet always-run fallback as a route —
  `refusal.code = DEGRADED_FALLBACK_REFUSED`, `head = null`, `candidates = []`,
  and the fallback lanes stay visible as `fallback-refused` exclusions;
- echoes `required_connector_capabilities` as informational constraints only —
  they never filter, rank or select anything here.

## Request (v1, closed)

Unknown fields are **rejected** (`INVALID_REQUEST`), never ignored. An
explicit `null` is rejected where a typed value is expected; ABSENT means "not
declared" and stays distinct from any explicit value (absence is visible in
`request_digest`). Numbers are normalized to JSON floats where the contract
says so; timestamps are normalized to UTC second precision `...Z`.

| Field | Req | Type / domain | Meaning |
|---|---|---|---|
| `version` | yes | int `1` | contract version |
| `parent_run_id` | yes | id | parent execution run |
| `step_id` | yes | id | the step being dispatched |
| `generation` | yes | int ≥ 0 | retry generation |
| `brief_digest` | yes | sha256 hex | canonical execution-brief digest |
| `required_levels` | yes* | `{category: int −5..+5}` | raw per-step levels, canonical category vocabulary (validated against `category_levels.jsonl`/`level_defs.jsonl` through the resolver's own ad-hoc validator). `min_context` is **not** accepted here |
| `requester` | yes | id | foreman identity reference |
| `context_refs` | no | `[{name, digest}]` | context **reference digests** — references only; content is never fetched or embedded |
| `required_context_limit` | no* | int > 0 | context requirement (the only home of `min_context`) |
| `required_data_class` | no | label | exact-match eligibility constraint vs the lane's registry `data_class` (no lattice invented; an unknown lane class never satisfies a declared class) |
| `policy_labels` | no | labels | validated, echoed, informational |
| `required_connector_capabilities` | no | labels | validated, echoed as constraints, never resolved |
| `budget_ceiling_usd` / `budget_remaining_usd` | no | number ≥ 0 | remaining must be ≤ ceiling |
| `attempt_deadline` | no | RFC3339 | bounds `valid_until`; must be after the decision clock (`ATTEMPT_DEADLINE_PASSED` otherwise) |
| `valid_for_s` | no | int > 0 | requested window, clamped to the policy ceiling |
| `previous_route_digest` | no | sha256 hex | reconciliation: the receipt the previous attempt used |

`id` = `[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}`; `label` =
`[a-z0-9][a-z0-9._-]{0,63}`; digests = 64 lowercase hex.
\* At least one requirement (`required_levels` and/or `required_context_limit`)
must be declared.

## Receipt (v1, closed)

Every key is always present (`RECEIPT_KEYS`):

| Key | Meaning |
|---|---|
| `version`, `receipt_id`, `receipt_digest` | identity + integrity |
| `request_digest`, `brief_digest`, `parent_run_id`, `step_id`, `generation`, `previous_route_digest`, `context_refs` | what was decided for |
| `resolved_at`, `valid_until`, `valid_for_s` | the decision window |
| `eligible`, `refusal`, `head` | the verdict — head only when eligible **and** OPEN |
| `candidates`, `exclusions`, `gate_reasons` | the exact ordered chain + exact exclusions |
| `requirements`, `constraints` | requirement echo + connector capability echo with explicit null selections |
| `source`, `registry`, `policy`, `fallback_used`, `degraded_fallback`, `warnings` | provenance: registry `{source, version, digest}`, policy = `quota-state.json` `{source, version, digest}` (null digest = file absent — unknown is not empty) |
| `gate_evidence` | `gates_loaded` + verbatim `quota`/`health`/`circuit` rows for exactly the lanes considered |
| `signature`, `signature_alg` | **authentication**: HMAC-SHA256 (`hmac-sha256-v1`) over the canonical receipt body, key held server-side |

Candidate: `{hop, provider, model, price{list_usd_1m, in_per_m, out_per_m,
cache_read_per_m, cache_write_per_m, normalized, effective, basis},
context_limit, data_class, eligibility{requirements[{category,
required_level, tier, tier_effective}], context{required, limit, rule, note},
data_class{required, lane, met}}}`. Resolver null/zero semantics are preserved
verbatim (`0.0` stays free, `null` stays unknown).

Exclusion: `{hop, provider, model, why, codes}` — the resolver's gate
exclusions first, **exactly** as produced (reason prose + machine codes), then
decision-stage exclusions (`data-class-mismatch`, `fallback-refused`).

## Canonicalization and digests

    canonical_json(obj) = json.dumps(obj, sort_keys=True,
                                     separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False)

- `request_digest` = sha256 over the **normalized** request (only declared
  fields; key order irrelevant).
- `receipt_digest` = sha256 over the receipt **without `receipt_digest`
  itself** — nothing else is excluded, so every authority-bearing field is
  covered.
- **Volatile fields included in the digest by design:** `receipt_id`,
  `resolved_at`, `valid_until`. `receipt_id` is deterministic — sha256 over
  `{request_digest, resolved_at, valid_until}` — so the same request at the
  same clock re-issues byte-identically (reconciliation-friendly) and tampering
  with expiry or issuance time is a digest mismatch.
- Deterministic across input key order **and process restart**.

## Policy

`POLICY_DEFAULT_TTL_S = 300`, `POLICY_MAX_TTL_S = 900`. `valid_until =
min(resolved_at + ttl, attempt_deadline)`; a caller-requested `valid_for_s` is
clamped to the ceiling (and the clamp is a warning on the receipt). The clock
is injectable (`now=` / `--now` / `ROUTER_DECISION_NOW`) for deterministic
replay and tests; the HTTP surface never takes it from a request body.

## Verification (`verify_receipt` / `POST .../verify` / `router decide --verify`)

**Integrity AND authentication.** The canonical digest is unkeyed, so it can be
recomputed by anyone over a receipt they invented — it proves integrity only.
Authentication therefore requires two independent facts, both of which must
hold for `ok=true`:

- the receipt carries a valid `signature` (`hmac-sha256-v1`) over the canonical
  body, computed with the server key at
  `<state>/decision-receipt.key` (32 bytes, mode 0600, created exactly once) —
  refused as `RECEIPT_UNSIGNED` / `RECEIPT_SIGNATURE_INVALID` /
  `RECEIPT_KEY_UNAVAILABLE`;
- a matching issuance record exists in `<state>/decision-receipts/<YYYYMM>.jsonl`
  for the receipt's digest, and its `receipt_id`, `request_digest`,
  `parent_run_id`, `step_id`, `generation` and `valid_until` equal the
  receipt's — refused as `RECEIPT_NOT_ISSUED` / `RECEIPT_ISSUANCE_MISMATCH`.

Consequence: a receipt is verifiable only on the host that issued it (the key
and ledger are local authority, never shipped inside the receipt). Refusals
also cover any change to candidate order, exclusions, source, requirements,
chosen head or expiry (`DIGEST_MISMATCH`), at/after `valid_until` (`EXPIRED`,
verifier clock), an unknown or missing receipt key, a `valid_until` that is not
after `resolved_at` or exceeds the policy ceiling, and internally inconsistent
receipts (`HEAD_MISMATCH`, `ELIGIBILITY_MISMATCH`, `MALFORMED_RECEIPT`,
`VERSION_UNSUPPORTED`).

## Writes

A decision writes exactly two things, both inside the router state dir: the
signing key (once) and one issuance record per issued receipt. It never writes
quota, health, circuit, ledger, registry or table state. If the issuance record
cannot be written the receipt is **not** returned (`RESOLVER_ERROR`): an
unrecorded receipt could never be verified later.

## Response semantics

| Case | Payload | HTTP | CLI exit |
|---|---|---|---|
| eligible route | receipt, `eligible=true`, `head` set | 200 | 0 |
| no candidate / all gated | receipt, `eligible=false`, `refusal=NO_ELIGIBLE_CANDIDATE` | 200 | 0 |
| fallback offered | receipt, `eligible=false`, `refusal=DEGRADED_FALLBACK_REFUSED` | 200 | 0 |
| invalid request / deadline passed | error doc `INVALID_REQUEST` / `ATTEMPT_DEADLINE_PASSED` | 400 | 2 |
| resolver/infrastructure failure | error doc `RESOLVER_ERROR` (fail **closed**, never a fabricated route) | 500 | 1 |

## Compatibility

`router spawn` is untouched: its fail-open contract (`{"error": ...}` + exit 0,
never blocking the scheduler) and its output schema remain byte/schema
compatible. The decision surface is additive.
