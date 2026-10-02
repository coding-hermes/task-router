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

## 5. L3 — policy (the three consumers)

1. **Pacing / spacing** — the thing that stops "over-running the provider at once".
   Admit a call only if `spent_in_window + est_cost ≤ limit × safety_margin`, where the pacing rate is
   `remaining / time_to_reset` (smooth spend) rather than "spend until 429". Soft-spacing (delay a hop) is
   preferred over hard refusal; an exhausted window refuses **explicitly** with the window named, and the
   refusal is counted (`_ADMISSION` already has the shape for this). Cross-account rotation is a *policy knob*
   here, not a new mechanism: when account A is paced out, the chain may move to account B **only** if B's
   own window has headroom.
2. **Chain preference** — lane ordering gains one term: prefer lanes whose window has headroom, deprioritise
   lanes near their ceiling, and keep the existing rules intact (plan lanes before PAYG, measured cost before
   price when coverage allows). Quota is a *tie-breaker and a guard*, never a silent re-rank of the fleet.
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
- **Live smoke** (opt-in, bounded): hit the readback endpoints for providers where we have keys; assert the
  response parses and the value is plausible; never run in CI (live-only harness, same rule as the acceptance
  battery).

## 8. Non-goals

- Not a billing system: we never assert what a provider will invoice, only what we observed and derived.
- Not a rate-limiter replacement: per-minute request limits stay where they are; this layer is about
  *windows* (5h/week/month) and pools.
- No hard-coded limits in code, ever. If it is not in the data table, it is unknown.
