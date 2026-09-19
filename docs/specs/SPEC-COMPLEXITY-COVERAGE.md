# SPEC-COMPLEXITY-COVERAGE — every routable lane carries a complete, provenance-stamped complexity profile

Status: SPEC (design authority) · 2026-09-19 · Bane directive
Board row: TR-064 (P1)
Related: TR-044 (family-fill guard), docs/category-data-quality.md, TR-054 (evidence-class law)

## 1. Problem

The registry can only route what it can measure. Today most lanes are
**chain-invisible**: `model_tier` has no row for them in the categories a
profile requires, so eligibility fails before price is ever considered. On the
09-17 xKiro slice this was 54 of 57 live-proven lanes. The fleet consequence is
silent: a lane that works, is free, and is fast never gets selected — and
nothing reports that it was skipped for lack of evidence rather than for cause.

## 2. Goal

Every active, priced lane in `models.jsonl` carries a **complete complexity
profile** across all routable categories, OR an explicit
`tier_source: none` marker with a reason. No lane is silently tier-less.

## 3. Requirements

R1. **Coverage audit command.** `router audit-tiers [--json] [--provider P]`
    prints, per lane: categories with a tier, categories missing, tier_source,
    eligible-profile count. Exit non-zero when any active lane has
    `tier_source: none` without a reason (CI-usable).

R2. **Provenance on every tier row.** `model_tier` rows gain
    `tier_source ∈ {measured, bench, family, none}` plus `source_ref`
    (commit/URL/probe-run id). A tier whose provenance cannot be named does not
    exist.

R3. **Evidence ladder** (highest wins; never averaged together):
    1. `measured` — our own battery/probe run on that exact lane (calibrated
       scale; binary pass/fail scores MUST NOT enter `perf_*` — TR-054).
    2. `bench` — public benchmark rows for that exact model in
       `benchmarks.jsonl` via `BENCH_OVERLAY` patterns.
    3. `family` — derivation from the same model served by another provider,
       **only when a `PROFILE_MODELS` mapping entry exists** (the registry's
       documented mechanism; TR-044 guard stays: no tier without a named
       mapping + source).
    4. `none` — no evidence: lane is excluded from requirement-bearing chains
       and audited by R1, never guessed.

R4. **Family derivation must be declared.** Adding a `family` tier requires a
    mapping entry AND the derived tier is capped at the source lane's tier
    (never upgraded by derivation).

R5. **Saturation guard.** Battery/derivation runs that would add a cluster of
    identical top scores must be checked against the percentile ladder first;
    if `category_levels` quantiles would collapse (q90 → 1.0), the run is
    rejected and the evidence recorded in `benchmarks.jsonl` only (TR-054).

R6. **Weekly regression.** The audit runs in the weekly lane; its report names
    lanes that lost coverage (evidence rotted, model renamed, provider
    dropped) — coverage loss is a tracked regression, not silence.

## 4. Acceptance criteria

- AC1: `router audit-tiers --json` on the current registry returns a machine-
  readable report; the count of active lanes with `tier_source: none` is
  visible and non-zero lanes are listed with reasons.
- AC2: every `model_tier` row in the seeded registry has non-null
  `tier_source` and `source_ref`.
- AC3: N unit tests: ladder precedence, family cap (derived ≤ source), missing
  mapping ⇒ no tier, saturation rejection, audit exit codes.
- AC4: a coverage report committed to `docs/` for the run date, listing
  per-provider coverage %.

## 5. Non-goals

- Not a capability guess: no tier may be produced without one of R3's four
  provenances.
- Not a re-pricing exercise; pricing is orthogonal (see TR-063).
- No changes to profile requirements or category vocabulary.

## 6. Dependencies

- TR-054 evidence-class law (binary scores never in `perf_*`).
- `BENCH_OVERLAY` + `PROFILE_MODELS` in `scripts/router_seed.py`.
- `task_profile_requirements` table (the category vocabulary source).
