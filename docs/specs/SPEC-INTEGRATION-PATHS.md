# SPEC-INTEGRATION-PATHS — side channel (declared complexity) and router proxy (classified complexity), with fallback chains

Status: SPEC (design authority) · 2026-09-19 · Bane directive
Board rows: TR-066 (P1, side channel) · TR-067 (P1, proxy + classifier — **absorbs TR-050**)
Related: docs/integration.md, TR-051 (agentic-system research, complete), TR-065 (stats v2)

## 1. Principle

Two integration paths, one contract. **The task board already holds the
complexity** (profiles / category levels), so the cheap path must never require
a classifier, and the expensive path must never be required for a caller that
already knows its profile. Both paths must be additive — no Hermes-internal
changes, no fork of the router.

## 2. Path A — side channel (declared complexity, zero inference)

R1. **Request shape (contract face).** The caller asks the router for a chain
    with the complexity it already has:
    - named profile: `POST /api/v1/spawn {"project": "..."}` (existing) or
      `router_spawn <project> --profile P1_CODING`
    - ad-hoc set: `--profile-req code_gen=-2,test=0,debug=-3`
    - sort rule: `--sort predicted_cost_per_task|tokens_per_task|turns_per_task|
      wall_time_per_task|price|ratio:<expr>` (TR-065 §4), plus `--window-h`,
      `--backend`, `--merge-backends`.

R2. **Response carries everything needed to execute without the router.**
    Per hop: provider, model, effective price, `complexity_sig`, bucket
    `n_samples`, metric values, `stats_fallback`, and the gates applied
    (health/quota/circuit/slow). The caller (Hermes/scheduler) then calls the
    lane directly and reports the outcome back.

R3. **Outcome write-back closes the loop.** After each attempt the caller POSTs
    to `/api/v1/outcomes` (existing c1 endpoint) with
    `{source_system, session_id, profile_id|required_categories, provider, model,
    turns, tokens_*, cost_usd, wall_time_s, success}`. Validation errors are
    reported to the caller (existing `ingest()` contract), never swallowed.

R4. **Fallback chain is the caller's loop over the returned chain:** try hop 1;
    on transport/HTTP failure mark that lane's pair breaker (router-side,
    existing circuit mechanics) and try hop 2; stop at the first success; record
    an outcome row per attempt. `max_hops` is a caller parameter with a
    documented default. Content-level dissatisfaction is NOT a retry trigger
    (it is an outcome with `success=false`, which feeds the averages).

R5. **Idempotency:** `session_id + model` dedupes outcome rows, so a retried
    POST cannot double-count a task.

## 3. Path B — router proxy (classified complexity, callers unchanged)

R6. **Endpoint mirror.** The router exposes a Hermes-compatible face so the
    scheduler (or any client) submits a task to the ROUTER instead of the
    gateway; only `base_url` changes. The proxy must accept the same request
    body the caller already sends and return the same response shape.

R7. **Classifier hop = data, not code.** A versioned prompt file (e.g.
    `data/classifier/prompt-<version>.md`) plus a configurable model
    (default a fast sub lane; per doctrine, subscription-first) turns the
    incoming request into a **complexity matrix**: the per-category levels the
    task requires. Output is schema-validated: unknown categories rejected,
    levels clamped to the vocabulary range, and a confidence field recorded.
    Classifier prompt version + model are recorded on every row it produces.

R8. **Chain construction inside the router.** The matrix is converted to a
    complexity set (TR-065 R1) and the router builds the chain with the
    two-stage selection (eligibility → stats sort). Nothing is inferred about
    the task beyond the validated matrix.

R9. **Execution + fallback chain inside the router.** The router calls Hermes
    with hop 1's lane; on transport/HTTP failure it advances to the next hop
    (existing breaker accounting), bounded by `max_hops`, and returns the final
    response to the caller as if it had called Hermes directly. The per-attempt
    trail (lane, reason, latency, cost) is returned in response metadata and
    written as outcome rows.

R10. **Classifier cost is measured like any other task:** its own outcome row
     (`profile_id: "CLASSIFIER"`), so the proxy's overhead is visible in the
     same stats it feeds. Classifier failures degrade to the caller's declared
     profile when present, else to the default profile — never to an
     uncontrolled default lane.

R11. **Never silent, never spurious:** if the proxy cannot classify, it reports
     the degrade reason; if a hop fails on content (not transport), the response
     is returned as-is with `success=false` recorded — the router does not
     silently re-ask a different model for a "better" answer.

## 4. Acceptance criteria

- AC1 (Path A): e2e — declared profile → chain → caller executes hop 1 with a
  stub provider → outcome POST accepted → averages include the row; a second
  call on the same session is deduped (R5).
- AC2 (Path A): fallback — hop 1 forced to 500, hop 2 succeeds; both attempts
  produce outcome rows; the breaker for hop 1 is open afterwards.
- AC3 (Path B): request in → classifier fixture out → validated matrix →
  chain; a malformed classifier output is rejected with a visible reason and
  degrades per R10.
- AC4 (Path B): the caller's request/response shapes are byte-compatible with
  the Hermes gateway contract (contract test against captured fixtures).
- AC5: proxy failure injection — 3-hop ladder, hop 3 succeeds, `max_hops`
  respected, response metadata names every attempt.
- AC6: docs updated (`docs/integration.md` + both specs) with copy-pasteable
  examples for both paths, incl. the scheduler's integration recipe.

## 5. Non-goals

- No changes inside the Hermes gateway (proxy is additive; base_url swap only).
- No prompt-based routing decisions other than the complexity matrix.
- No automatic re-ask on content dissatisfaction (R11).
- TR-051's per-system drivers (OpenCode/Claude Code/Cursor/…) remain follow-on
  work; this spec defines the surface they will target.

## 6. Dependencies

- TR-065 (stats v2) for the ordering Path B consumes.
- TR-064 for tier coverage (classification is worthless if lanes lack tiers).
- Existing pieces: `/api/v1/spawn`, `/api/v1/outcomes`, circuit/health/quota
  gates, `docs/outcomes-schema.md`.
