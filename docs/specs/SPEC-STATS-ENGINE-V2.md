# SPEC-STATS-ENGINE-V2 — complexity-set-keyed rolling averages + compound pluggable sort rules

Status: SPEC (design authority) · 2026-09-19 · Bane directive
Board row: TR-065 (P1) · **absorbs the remainder of TR-049 c6+**
Related: `docs/outcomes-schema.md` (c2), `scripts/router_outcomes.py` (c1–c5),
`scripts/outcomes_averages.py` (c3), profile_signature (1b02902)

## 1. Problem

TR-049 shipped the store, ingest, rolling averages and pluggable sort keys — but
the averages are **keyed per model**, with a single optional `complexity` scalar
slot that is NULL for every imported row. Bane's requirement is different:

> complexity is the **categories** of the task — one average per model **per
> complexity set**, where a task may require one or many categories at set
> levels. Sorting must support tokens/task, turns/task, cost/task, wall-time/task
> and compound ratios, with as little hardcoding as possible.

Without this, `predicted_cost_per_task` cannot distinguish "a P1 code task on
glm-flash" from "a P4 security audit on glm-flash" — the exact decision the
selector exists to make.

## 2. Goal

The selector becomes two-stage and stats-aware:

1. **Eligibility by complexity** — the existing per-category tier matching
   (unchanged; that is the reference Bane means).
2. **Ordering by measured stats for THAT complexity set** — rolling averages
   keyed by (model × complexity set), with pluggable metrics and compound rules
   parsed generically.

## 3. Data model

R1. **Complexity set = canonical signature of the declared requirements.**
    `complexity_sig(requirements) = sha1(canonical_json({category: min_level}))`
    — dict-order independent, integer-normalized, full dict retained alongside
    the hash. A task with `{code_gen: -2, debug: -3, refactor: -5, test: 0}`
    is a different bucket from `{security: 2, review: 0, guard: 0}` even on the
    same model. `profile_signature()` already emits this dict from
    `task_profile_requirements`; ad-hoc profiles use their explicit levels.

R2. **Outcome rows carry the set.** `complexity_sig` + `required_categories`
    (the dict) + `profile_id` (when a named profile was used). Imported gateway
    rows stay honest: NULL until a classifier or caller declares the set.

R3. **Average rows are keyed by (source_system, provider, model,
    complexity_sig)** with `required_categories` retained for readability.
    Existing per-model rows remain available for callers that do not declare a
    set (keyed `complexity_sig: null`).

R4. **Metrics per bucket, all decay-weighted at 1d/3d/7d (extensible):
    `avg_cost_task`, `avg_tokens_in_task`, `avg_tokens_out_task`,
    `avg_tokens_total_task`, `avg_turns_task`, `avg_wall_time_task`,
    `n_samples`, `n_completed`, `success_rate` (NULL when unknown — never
    inferred from merely having returned tokens).

R5. **Thin/empty buckets never fabricate.** Lookup order for a declared set:
    exact signature → (optional, OFF by default) declared superset rule →
    per-model null-signature row → price. Every fallback is reported in the
    resolve output as `stats_fallback: <kind>`, never silent.

## 4. Sort rules (minimal hardcoding)

R6. **Metric registry, not a closed list.** Sort specs are strings parsed by a
    small expression parser:
    - `predicted_cost_per_task`, `tokens_per_task`, `turns_per_task`,
      `wall_time_per_task`, `price` (existing)
    - compound: `ratio:0.7*cost+0.3*turns`, `ratio:1*tokens_in+2*turns`,
      `ratio:0.5*price+0.5*wall_time`
    - metrics resolve through a name→field map built from the averages row
      schema itself (adding a metric to R4 makes it sortable without code
      edits); weights are floats, any count, no hardcoded weight table.
    - unknown metric or malformed expression ⇒ **visible warning + degrade to
      price** (never a silent guess, never a crash — fail-open is sacred).

R7. **Compound rule semantics are specified, not implied:** each term is
    `weight * metric_value_within_bucket`; missing metric in a bucket ⇒ term
    dropped with a reported note for that lane; all terms missing ⇒ lane falls
    back per R5.

R8. **Selection is two-stage and honest:** stage 1 eligibility, stage 2 sort.
    The resolve output carries, per hop, the bucket used (`complexity_sig`,
    `n_samples`) and the metric values that produced the order — a chain must
    be explainable without re-running it.

## 5. Acceptance criteria

- AC1: identical requirement dicts in different key orders produce the same
  `complexity_sig`; different levels produce different sigs (unit tests).
- AC2: averages computed per (model × set) — two profiles requiring different
  categories on one model yield two buckets with independent decayed values.
- AC3: parser tests: every named metric, 3-term ratio, unknown metric, malformed
  string, zero-weight term; each asserts the documented degrade path.
- AC4: live demo — same project, two different declared profiles on one model
  produce different orderings, with the resolve output naming bucket + n.
- AC5: `docs/outcomes-schema.md` updated with R1–R4 shapes; sample rows committed
  (store itself stays uncommitted).
- AC6: full suite green; no regression to `--sort price` default behaviour.

## 6. Non-goals

- No new capability claims (that is TR-064's evidence ladder).
- No gateway proxy (TR-067) and no classifier (TR-066/067) in this spec.
- No cross-model normalization of metrics; averages stay per-lane per-bucket.

## 7. Dependencies

- TR-064 for trustworthy tiers (stage 1 must be real for stage 2 to matter).
- `scripts/outcomes_averages.py` refresh cadence (hourly owner required).
