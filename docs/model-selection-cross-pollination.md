# Model selection & provider understanding — cross-pollination from Chimera

Study notes (2026-08-27, Bane directive) mapping Chimera v2's model-selection
machinery onto the task-router. Chimera = `~/chimera-v2` (github.com/
totalwindupflightsystems/chimera), skills `development/chimera` +
`development/chimera-development`. These are IDEAS to keep in mind — the
task-router's core semantics (dominance rule, percentile scale, price-ordered
chains, fail-open) stay as designed; anything below is an extension candidate.

## Selection architecture ideas

1. **Provider diversity cap** (`selector.select_diverse()`, at most one model
   per provider). Task-router chains currently have NO diversity constraint —
   the 3 cheapest healthy hops can all belong to one provider, so one outage
   kills hops 1–3. Candidate: optional `max_per_provider` at resolve time
   (e.g. 2) or a `diverse: true` profile flag. Cheap, high value.
2. **Cost-weighted effectiveness with a sensitivity knob**:
   `effectiveness = quality / (cost ^ price_sensitivity)`, where 0.0 = pure
   quality, 1.0 = pure bang-for-buck (Chimera `selector.py`, per-call
   override). Task-router sorts by price after dominance filtering; a
   per-profile `price_sensitivity` would let premium profiles (security
   review) prefer quality over the cheapest healthy hop. Do NOT replace the
   dominance rule — blend only among eligible models.
3. **Parent-path fallback**: Chimera's 32-path tree falls back to parent
   paths when the exact leaf isn't scored. Task-router's 24 flat categories
   have no hierarchy; sparse categories (guard/mock/multilingual — TR-002)
   could get a parent-axis fallback (coding / reasoning / agentic /
   perception / language) so models with only a parent-axis signal are still
   comparable.
4. **Keyword → category lexicon** (Chimera `PATH_PATTERNS`, 5 categories:
   code/reasoning/analysis/design/audit, regex keyword tables). Reusable as a
   seed lexicon for ad-hoc profile inference from task text in the 24-category
   space. `references/selector-module.md` has the full keyword tables.
5. **Budget-tier boost** (+15% for budget tier) ≈ task-router's `plan_tier`
   primary sort key — already covered, no action.
6. **Per-model enabled/disabled flag** (Chimera `enabled: bool`) ≈ registry's
   `archive`/`valid_to` — already covered.

## Provider understanding (feeds TR-001 calibration + probe design)

7. **Response-field quirks — the probe MUST extract from 3 fields, not just
   `content`**: DeepSeek V4 → `message.reasoning_content`; MiniMax M3 / Kimi
   K2.7 → `message.reasoning`; Z.AI GLM-5.2 → content may be null with output
   in `reasoning_details[].text`. A probe that only reads `content` reads
   healthy models as empty → false DOWN.
8. **Health-ping call shape**: `temperature=1` (Anthropic rejects 0.0),
   timeout ≥ 15s (DeepSeek cold starts), `max_tokens ≥ 100` for reasoning
   models (Gemini/GLM consume the whole budget on thoughts and return empty
   visible `parts` — a `max_tokens=10` ping reads as MAX_TOKENS/no content).
9. **Auth ≠ inference**: a key can pass `/models` (200) and still fail chat
   (Z.AI coding-plan vs main-endpoint 429 balance). Probe must do a real chat
   completion; classify 429 (balance/capacity) separately from 401/403/400
   (misconfig — task-router skill's existing rule, confirmed by Chimera).
10. **Endpoint/version mismatches look like outages**: Google v1beta serves
    only gemini-2.5 (gemini-3.x are Vertex/OpenRouter-only → 404 on direct
    ping); Z.AI coding plan = `api.z.ai/api/coding/paas/v4` (quota-reset), main
    = `open.bigmodel.cn` (may be 429 balance); OpenRouter privacy guardrails
    block premium models with "No endpoints available matching your guardrail
    restrictions" (auth-level exclusion, not DOWN).
11. **Routing changes latency class**: DeepSeek via OpenRouter ≈ 125s vs
    direct ≈ seconds. SLOW thresholds must know the intended route per
    provider, or healthy direct routes get misclassified.
12. **Key inventory (from Chimera 2026-07-05 + current health-state)**:
    DeepSeek/OpenRouter/OpenAI/xAI/Gemini valid; Z.AI coding-plan works;
    ANTHROPIC absent from `~/.hermes/.env`. Current probe: 8/14 DOWN with
    fast auth errors (401/403/400) — config work, not outages: clinepass 500,
    kimi-for-coding 403, opencode-go 403, groq 403, openai-codex 401, stepfun
    401, minimax 401, synthetic 400.

## Catalog discipline

13. **Verify before adding a model** (Chimera model-catalog-maintenance):
    OpenRouter page exists; provider release announcement; pricing available;
    not a duplicate naming; no "coming soon" / pulled models (Claude Mythos 5
    launched Jun 9, pulled Jun 12). Apply to every model_perf seed — TR-002.
14. **models.dev as pricing source**: `https://models.dev/api.json` (145
    providers, $/MTok; cache 24h; entry field is `cost.input`/`cost.output`,
    divide by 1000 → $/1k). Task-router's `normalized_price` could sync from
    it; `openrouter.ai/api/v1/models?sort=top-weekly` for production ranking.
15. **Canonical-copy sync**: Chimera keeps chimera.yaml/example/docker in
    sync after every catalog change — the task-router analog is routing ns
    tables ↔ task-router ns tables ↔ repo (TR-004/TR-005 scope).

## Resilience

16. **CLOSED→OPEN→HALF_OPEN breaker** (Chimera `circuit_breaker.py`):
    task-router's circuit is OPEN/closed with exp backoff but no HALF_OPEN
    probe — it waits for manual `record-success` or expiry. Candidate for
    TR-006: after cooldown expiry, admit one probe request; success closes,
    failure re-opens. Keeps chains self-healing.

## Alignment guardrails

- Task-router percentiles (−5..+5 → q01..q99) are NOT Chimera's absolute
  0-100 scores — the percentile scale stays (flat absolute thresholds were
  proven wrong in skewed categories).
- Chimera's dispatcher picks models by blended score; task-router's dominance
  filter + price order stays the contract. Extensions only.
- PAYG-as-legitimate-fallback and subs-first doctrine stay (both systems
  agree price ranks within eligibility).
