# Quota layers — spec (design authority, write-once/reuse-everywhere)

Status: L0 (declaration table + validator) IMPLEMENTED — TR-206, 2026-10-01
(`data/tables/provider_quota.jsonl` + `scripts/validate_provider_quota.py`).
L1–L3 are not implemented yet; the research wave filled the data table.
Scope: how the router *knows* how much quota each lane has left, *spends* it without over-running a provider,
and *finds* spare subscription quota to burn before it expires.

## 0. The problem, stated honestly

Three different failures get conflated as "quota":

1. **We don't know the limit.** (declaration gap — data, not code)
2. **We can't see how much is left.** (readback gap — some providers expose it, most don't)
3. **We spend it badly.** (pacing gap — N calls in a burst, then a 429 wall)

Each gets its own layer with its own failure mode and its own tests. No layer pretends to be a lower one.

## 1. Layer map

| Layer | What it is | Store | Failure mode | Reuse |
|---|---|---|---|---|
| **L0 declaration** | which windows a provider has: kind, unit, limit, scope, reset | `data/tables/provider_quota.jsonl` (data, not code) | unknown limit → NULL + reason | every consumer reads this; nothing hard-codes limits |
| **L1 readback** | what the provider actually tells us is left | adapters, no store | no readback → falls to L2 derivation, labelled | one adapter per mechanism, not per provider |
| **L2 accounting** | spent / remaining / resets_at per (account, window) | **a VIEW over the existing outcome ledger** — no second store | unmeasured → `estimated`, never `observed` | the ledger already has tokens+cost per row; window sums are a query |
| **L3 policy** | pacing, chain preference, burn-surplus | in-memory decisions + admission counters | refusal is explicit, never silent | admission gate, router chain, UI all consume L2 |

The single most important design choice: **L2 is a view, not a counter.** We already write per-row tokens and
cost. A rolling 5h window is then `sum(usage) where ts > now-5h` — computed on demand, impossible to drift,
and it reuses data we are already paying to collect. A separate "quota counter" would be a second source of
truth that can disagree with the ledger, and when it did, we would not know which one lied.

## 2. L0 — declaration (data only)

One row per **(provider, window, scope)**. Windows are rows, not enum entries, because reality is not an enum:
`5h + wk + mo` (clinepass, opencode-go), `5h + 7d` (xkiro, ollama-cloud, synthetic, minimax), `weekly pool
(unpublished)` (grok-build), `monthly credits, rollover` (commandcode), `energy mWh` (neuralwatt).

```
{ "provider_id": "zai-glm", "account": "default", "scope": "account",
  "window_kind": "rolling_5h",            # rolling_5h | rolling_daily | weekly | monthly | pool | per_request
  "unit": "tokens",                        # tokens | usd | credits | requests | messages | energy_mwh
  "limit": 28000, "limit_text": "28K/5h",
  "reset_kind": "rolling", "reset_anchor": null,
  "source": {"kind": "docs"|"observed"|"policy", "url": "..."},
  "confidence": "verified"|"partial"|"unknown",
  "valid_from": "2026-09-27", "archive": false }
```

Rules inherited from this repo's doctrine:

- **Unknown is NULL with a reason** (`reason: not-published | no-readback | plan-not-disclosed`), never `0`.
- **A pool is its own scope** (`scope: "pool:free"`), because some plans give free models an *independent*
  budget while others draw the paid window down at a discount. Same provider can have both.
- **An alias does not own quota.** `gw-deepseek` and `myrouter:zai-glm` are gateway aliases: they carry
  `alias_of: <provider>` and **inherit** the parent's windows. Without this they double-count.
- **Multi-account is per account** (`account: "xkiro"` vs `"xkiro-2"`). Aggregation is a display choice;
  the accounting is per account. Two accounts of the same plan never merge into "2× the window" unless the
  provider's own docs say the limit is per-person.
- **Concurrency is a different axis** from quota (already in `providers.jsonl`) — the pacing layer treats it
  as a second ceiling, not as quota.

## 3. L1 — readback ladder (never silently degrade)

In strict priority order. Whichever rung answers, its identity is recorded on the observation.

1. **Response headers on calls we already make** — free, exact, per-request:
   `anthropic-ratelimit-unified-5h-utilization`, `-7d-utilization`, `-status`, `-reset` (Anthropic-family);
   `x-ratelimit-remaining-requests`, `-remaining-tokens`, `-reset-requests`, `-reset-tokens`, `retry-after`
   (OpenAI/Groq/Fireworks-family). Exact names per provider come from the research table.
   *Implemented 2026-10-02 (TR-207): `scripts/router_quota_readback.py` — one adapter per header mechanism,
   recorded-response fixtures in `tests/test_quota_readback.py`.*
2. **A usage/balance endpoint**, polled on a bounded schedule with a cache: credits/balance APIs, org usage
   APIs, quota APIs. Exact method+URL+fields per provider come from the research table.
3. **Local client state** where it is the *only* signal (e.g. a coding CLI's own auth/session files).
4. **Derivation from L2** — `limit − sum(ledger usage in window)`. This is the honest fallback that makes
   dashboard-only providers usable at all. It is **labelled `estimated`** and carries `basis:
   derived-from-ledger`; it is never promoted to `observed` without rung 1/2 evidence.

Every observation: `{provider_id, account, window_kind, remaining, limit, unit, observed_at, source_rung,
source_detail, confidence}`. The UI is required to show the rung — an estimate rendered like a reading is a lie.

## 4. L2 — accounting (a query, not a store)

Per (account, window) at time T:

```
spent      = sum(unit-equivalent usage) over rows in the window
remaining  = limit − spent                      (limit NULL ⇒ remaining NULL, reason preserved)
resets_at  = rolling: oldest in-window row + window  |  calendar: next anchor  |  unknown
headroom   = remaining / seconds_to_reset       (the pacing budget)
```

Unit equivalence is the hard part and lives in **one** place: tokens → dollars uses the *same* price table
(`public_price`/`normalized_price`) the cost layer already uses. If a provider's window is denominated in
dollars and our ledger is denominated in tokens, the conversion is that one function, tested once.

**Per-provider (not per-lane).** A provider with 498 clinepass lanes and one $10 credit window is a *single*
budget; lane count is irrelevant to it. This is why the layer keys on provider+account, not on model.

*Implemented 2026-10-09 (TR-209): `scripts/router_quota_accounting.py` — the view over
`data/state/outcomes.jsonl`, reached as `router quota accounting`; the limit side of this L2 layer is a
simple per-provider `{window, limit_usd}` JSON (`data/quota_limits.json`, `$ROUTER_QUOTA_LIMITS_FILE`),
read at query time, never stored state. The L0 `provider_quota.jsonl` research table remains the
limits-of-record for provider plan terms; the L2 config exists because L0 carries per-window *plan* limits
in token/credit units, while the L2 ledger query is denominated in dollars.*

## 5. L3 — policy (the three consumers)

1. **Pacing / spacing** — the thing that stops "over-running the provider at once".
   Admit a call only if `spent_in_window + est_cost ≤ limit × safety_margin`, where the pacing rate is
   `remaining / time_to_reset` (smooth spend) rather than "spend until 429". Soft-spacing (delay a hop) is
   preferred over hard refusal; an exhausted window refuses **explicitly** with the window named, and the
   refusal is counted (`_ADMISSION` already has the shape for this). Cross-account rotation is a *policy knob*
   here, not a new mechanism: when account A is paced out, the chain may move to account B **only** if B's
   own window has headroom.

   *Implemented 2026-10-10 (TR-210): `scripts/router_quota_pacing.py` — the gate as a PURE decision over
   the L2 view (`PacingGate.decide` / `PacingGate.pick_account`), reached as `router quota pacing`. The
   module writes nothing (no second store) and never sleeps: it COMPUTES a delay, and a delay past
   `wait_cap_s` becomes a refusal instead of a stall. Counters are the caller's own `_ADMISSION`-shaped
   object, incremented exactly once per decision. NOT yet wired onto the live request path — the gate is
   the policy, admission wiring is a separate change. Consumers 2 and 3 are TR-213 / TR-211.*

   *Implemented 2026-10-10 (TR-211): `scripts/router_quota_burn.py` — the projection as a PURE
   function over the L2 view (`project_window` / `find_surplus` / `find_surplus_ledger`), reached as
   `router quota burn`. Elapsed time comes from the SAME window arithmetic the L2 view uses; the
   module writes nothing, calls no network, spawns nothing — the report (and its `--json` document)
   goes to stdout only. Every entry is labelled `estimated` with `basis: derived-from-ledger`; a
   no-limit or unpriceable window is verdict `unknown` with the L2 reason travelling (NULL, never 0);
   and every entry carries `cost_basis` / `subscription_covered` sourced from the limit config
   `plan_kind` or an explicit `--plan-kind` override — undeclared means the marginal cash is NULL
   with the reason, never guessed to $0 (burning is still the owner's call per project).*
2. **Chain preference** — lane ordering gains one term: prefer lanes whose window has headroom, deprioritise
   lanes near their ceiling, and keep the existing rules intact (plan lanes before PAYG, measured cost before
   price when coverage allows). Quota is a *tie-breaker and a guard*, never a silent re-rank of the fleet.

   *Implemented 2026-10-13 (TR-213): the demote slot lives inside `router_spawn._legacy_sort_key` AFTER the
   effective-price term — a lane whose L2 headroom (`headroom_usd_per_s` from `router quota accounting`,
   limits config `data/quota_limits.json` + outcome ledger) is KNOWN and below the guard
   (`ROUTER_QUOTA_ORDER_GUARD`, default 0.01 USD/s) sorts behind otherwise-comparable lanes (same PAYG
   bucket, plan tier, priced-ness, price), so the `(bucket, plan_tier, price)` composite stays
   non-decreasing and the no-silent-re-rank invariant passes by construction. UNKNOWN headroom (no limit
   configured, unpriceable spend, no reset) is never "low" — NULL with a reason, never 0. The resolve
   output states the term: per-hop `headroom_usd_per_s`, `deprioritised_for_low_headroom` +
   `deprioritised_reason` on lanes that actually lost a position, and a top-level `quota_ordering` block
   (enabled/guard/reason/deprioritised/paths). No re-rank when the inputs are absent — inert by default.*
3. **Burn-surplus** (your use: "find a project to burn it on") — for each window compute
   `expected_unused = remaining − (current_rate × time_to_reset)`. Positive and about to reset ⇒ surface it:
   provider, window, surplus in its unit, and what spending it would cost us in real cash (for a subscription
   window: usually $0 marginal — that is the whole point). Output goes to a report and a UI panel, and can
   seed a dispatch suggestion. It never auto-spends: burning is your call per project.

## 6. Honesty contract (enforced, not promised)

- No fake zeros: unlimited/unknown is `NULL` + reason, never `0` remaining and never `∞`.
- An `estimated` remainder can never be rendered as an observed one; the confidence travels with the number.
- A window with `confidence: unknown` limit contributes **no pacing constraint** and says so — silence is
  visible, not treated as infinite capacity.
- Every pacing refusal, and every 429 that still happens, is recorded with the window that caused it, so
  "why did we get a 429" is answerable from our own data.

## 7. Tests (what makes this real)

- **L0**: schema validation; a window row with no source URL is rejected; alias rows must resolve to a parent;
  unknown-limit rows must carry a reason (no silent NULLs).
- **L1**: recorded-response fixtures per header mechanism; a provider with no readback must fall through to
  `estimated` and say `basis: derived-from-ledger`; the ladder must not skip a rung silently.
- **L2**: window sums on a synthetic ledger (rolling boundary, calendar boundary, DST, out-of-order rows);
  unit conversion uses the price table and returns NULL (not 0) when the price is unknown.
- **L3**: pacing refuses at the ceiling and never above it (fuzz: random traffic, assert
  `spent ≤ limit × margin` always); burn-surplus selection on synthetic windows (expiring vs plenty-left);
  a paced-out account does not silently move to a sibling account with no headroom.
  *(The pacing half is implemented and enforced by `tests/test_quota_pacing.py`, 31 tests: the fuzz
  invariant, the exact margin line, soft-spacing vs the wait cap, counted refusals that name the window,
  cross-account rotation, unknown-limit visibility, counter reuse, and the CLI. The burn-surplus half is
  implemented and enforced by `tests/test_quota_burn.py`: the exact projection (rolling and calendar),
  expiring-with-surplus vs plenty-left-with-time, negative surplus => no-burn, no-limit => NULL with the
  reason, writes-nothing/no-network/no-spawn, cost-basis honesty (subscription-covered vs metered vs
  unknown, never free money unnamed), and the `router quota burn` CLI surface.)*
- **Live smoke** (opt-in, bounded): `TR215_LIVE=1 python3 scripts/router_quota_readback.py --live`
  reads the data-owned `provider_usage_endpoints.jsonl` table, makes at most five read-only requests,
  uses a ten-second timeout, and never writes router state or quota tables. It refuses with exit 2 whenever
  `CI` is set, and also requires the explicit `TR215_LIVE=1` opt-in. Endpoint JSON is retained as mapped
  evidence; it is never promoted to an observation unless the response headers provide a recognized L1
  signal. Missing headers produce the explicit `derived-from-ledger` fall-through. Recorded-header fixtures
  remain the CI-safe test path. Keys are read from the table's environment-variable names and never emitted.
  This is a live-only harness, under the same rule as the acceptance battery.

The live harness is deliberately diagnostic: it does not update `provider_quota.jsonl`, `usage-cache.json`,
quota state, the outcome ledger, or any other router store. A provider with no configured key is reported as
`no-key-in-env`; a timeout, HTTP failure, malformed JSON, or oversized body is reported with a finite degraded
basis rather than a fabricated zero.

## 7a. Live smoke operator contract (TR-215)

Use a real provider key only in the process environment, never in arguments or fixtures:

```bash
TR215_LIVE=1 python3 scripts/router_quota_readback.py --live --provider openrouter
```

The provider selector is optional. Selection is deterministic, one endpoint per provider, and hard-capped at
five providers. CI and all automated tests use recorded headers only. The live leg may show an empty observation
list when the provider exposes no recognized response headers; in that case the output must contain the ladder's
fall-through basis rather than treating a JSON balance field as a header observation.

## 7b. Verification tests

`tests/test_quota_readback.py` covers CI refusal, explicit opt-in, the five-provider cap, no-write behavior,
plausibility/mapped-field parsing, and honest fall-through. None of those tests makes a network request or
requires credentials.


## 8. Non-goals

- Not a billing system: we never assert what a provider will invoice, only what we observed and derived.
- Not a rate-limiter replacement: per-minute request limits stay where they are; this layer is about
  *windows* (5h/week/month) and pools.
- No hard-coded limits in code, ever. If it is not in the data table, it is unknown.
