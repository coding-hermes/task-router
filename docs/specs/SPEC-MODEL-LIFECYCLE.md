# SPEC — Model lifecycle (announced → active → retiring → retired)

Status: SPEC (design authority) — implementation tracked by board row TR-069 on the
task-router board. The date-semantics defect this spec depends on is ALREADY FIXED
(`row_is_retired` + tests, commit at filing time); everything below it is the feature.

## Problem (Bane, 2026-09-19)

The router can only be told two things about a model's existence today: nothing (it is
routable) or a permanent human switch (`disabled` + `disabled_reason`). There is no way to
say:

- "this model is announced for 2026-10-01 — keep it out of chains until then, then let it in
  automatically, because I already know it is coming";
- "this model is being decommissioned on 2026-11-15 — keep routing until then, warn me as
  the date approaches, and hide it by itself afterwards".

Both are DATE knowledge we already have (or can get from models.dev / provider
announcements). Today that knowledge is either thrown away or applied by hand, and a hand
switch is permanent: it hides a model that is merely early, and it cannot resurrect a model
that has shipped.

Two related facts measured at filing time:

1. **`valid_to` was presence-tested, not date-tested.** Eligibility read
   `valid_to is not None`, so the FIRST future-dated retirement stamp would have hidden that
   lane immediately — the announced-decommission case would shrink chains weeks early.
   FIXED in this change: `row_is_retired(row, today=None)` date-compares, is the single rule
   for both models.jsonl consumers (primary + fallback eligibility, and `router_gaps`), and
   is pinned by `tests/test_lifecycle.py`.
2. **An announced-but-unreleased model is routable today.** Nothing reads a release date;
   `valid_from` is populated on 814 rows as a catalog/price start, NOT a release gate, so it
   must not be reinterpreted. The pre-release half of this feature needs its own field.

## Design law

**State is DERIVED from dates; it is never hand-typed.** A status column that a human can
set can disagree with the dates, and then nothing is authoritative. The registry stores
dates + provenance; the resolver computes the state on every read. Correctness therefore
needs NO cron job: a state changes when the clock passes a date boundary, deterministically
and testably (frozen clock).

## Fields (additive on `models.jsonl`; nothing existing is repurposed)

| field | meaning | required |
|---|---|---|
| `available_from` | release date; NULL = already available | no |
| `valid_to` | retirement date (existing field, now date-compared) | no |
| `lifecycle_source` | where the date came from: `provider-announcement:<url>`, `models.dev:release_date`, `registry:estimate` | with any date |
| `lifecycle_checked_at` | when the date was last confirmed, so a stale announcement is visible | with any date |
| `replaced_by` | successor model id, so a retirement names its re-point target | no |

`valid_from` keeps its current meaning. Existing rows get `available_from = NULL`
(today's behaviour preserved on day one — no surprise chain changes).

## States (derived)

- `announced` — `available_from` > today → NOT routable, no price required, visible as "coming soon".
- `active` — no dates, or `available_from` ≤ today < `valid_to` (or `valid_to` null) → routable as today.
- `retiring` — `valid_to` within `WARN_DAYS` (default 14) → STILL routable and STILL ranked honestly;
  every hop note and listing carries `{state, retiring_at, replaced_by}`.
- `retired` — `valid_to` ≤ today → NOT routable, hidden by default, still visible on request.

## Rules

- **R1 Announced lanes are excluded from chains** and from every default listing.
- **R2 Retiring lanes stay eligible.** A warning never silently removes capacity; it names the date
  and the replacement.
- **R3 Retired lanes are excluded on the day** (`valid_to` ≤ today), and never before.
- **R4 No anonymous dates.** A lifecycle date without `lifecycle_source` is invalid and the audit
  reports it — the same provenance law as tier evidence (TR-064): a claim that cannot name its
  evidence does not exist.
- **R5 Human `disabled` wins.** Lifecycle never un-disables a lane, and disabling is never
  auto-reverted by a date passing.
- **R6 Nothing vanishes silently.** Default listings hide non-active rows but ALWAYS report them as
  counts by state, and `--include-lifecycle` (or `--all`) prints them with state + reason + dates.
  A differential count of eligible lanes before/after a date boundary must reconcile to the state
  transitions, never to "fewer rows".
- **R7 Unknown = NULL.** Missing release/retirement knowledge is stored as NULL, never invented;
  a guess is stored as `registry:estimate` and reads as such.
- **R8 Alerts are the only human touchpoint:** a digest names (a) models arriving,
  (b) models retiring within WARN_DAYS with the lanes and projects whose chains would lose a hop
  and the `replaced_by` target, (c) models retired since the last digest with any lane that was
  still routing them.

## Surfaces to change

- `scripts/router_spawn.py` — eligibility (announced/retired exclusion, retiring flag in hop notes).
- `scripts/router_audit.py` — lifecycle state per lane, default-hidden + explicit include, state counts.
- `scripts/router_gaps.py`, `scripts/router_status.py` — same hide-by-default + counts.
- `scripts/router_seed.py` — carry the new fields through the registry build (data in, data out).
- A digest entry on the existing hourly/daily refresh path — no new scheduler.

## Acceptance criteria (all testable with a frozen clock)

- A1 A lane with `available_from` tomorrow is absent from chains today and PRESENT tomorrow.
- A2 A lane with `valid_to` in 5 days is still in the chain, flagged `retiring` with the date + `replaced_by`.
- A3 A lane with `valid_to` today or earlier is absent.
- A4 Every listing hides non-active lanes by default, shows them with the explicit flag, and reports
  counts by state either way.
- A5 A lifecycle date with no `lifecycle_source` is reported by the audit as a defect.
- A6 `disabled=true` on a lane with a future `available_from` stays disabled past that date.
- A7 Differential reconciliation: the eligible-lane count before/after each date boundary changes
  exactly by the lanes whose derived state changed, with names.
- A8 `router audit-tiers` and `router status` surface the state; a hop whose lane is `retiring`
  carries it in the resolve output.

## Out of scope

- No auto-repoint of chains to `replaced_by` (report it; the human/policy decides).
- No removal of the `disabled` switch, and no auto-re-enable of retired lanes.
- No price behaviour change: an announced lane needs no price until it is available.
