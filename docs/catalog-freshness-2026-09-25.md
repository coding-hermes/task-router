# New-model registry refresh — 2026-09-25 (grok-4.7 · mimo-v2.6 · gpt-6 · claude-opus-5.5 · +9)

Requested: "make sure task router files are up to date with the new models that have come
out the last few days — grok, mimo, openai, anthropic and others — so we can route to those."

Three separate gaps were found. Only the first one is "are the rows there"; the other two
are why a model can be *in* the registry and still unroutable or invisible.

---

## 1. Rows + prices: live catalogs vs registry (measured, all carriers)

Method: every fleet carrier's live `/models` pulled with its own key from `~/.hermes/.env`,
diffed against the live rows in `data/tables/models.jsonl`; models.dev synced and diffed
independently (`router_modelsdev.py fetch|sync --dry-run`).

**Added 46 lanes** (each with a catalog or vendor-sticker price — no unpriced guesses):

| carrier | lanes added | examples |
|---|---|---|
| xkiro (`--preset xkiro`) | 25 | `openai/gpt-6-luna`, `openai/gpt-6-sol`, `anthropic/claude-opus-5.5`, `x-ai/grok-4.7`, `xiaomi/mimo-v2.6-flash|pro`, `anthropic/claude-sonnet-4|4.5`, 15 × `cohere/*` |
| commandcode (new preset) | 14 | `gpt-6-luna`, `gpt-6-sol`, `gpt-6-astra`, `xai/grok-4.7`, `xiaomi/mimo-v2.6-*` (3), `claude-opus-5-5`, `Qwen/Qwen3.8-Omni-Flash`, `meituan/LongCat-2.0`, `stepfun/Step-5-Preview`, `z-ai/glm-5.3-flashx`, `stealth/pixel-canary`, `stealth/space-bunny-alpha` |
| opencode-go-2 (new preset) | 5 | mirror account had drifted behind its primary: `gpt-6-luna`, `grok-4.7`, `mimo-v2.6-flash|pro`, `space-bunny-free` |
| openrouter (`--preset openrouter`) | 2 | `perceptron/perceptron-mk1.5`, `mistralai/mistral-large-2512` |

Price refreshes applied in the same pass: openrouter 456 lanes updated (real market moves —
e.g. `deepseek/deepseek-v4-flash` 0.0809 → 0.0470, `deepseek-v4-pro` 0.907 → 0.419),
xkiro 116 (Anthropic list moved; `:free` window costs preserved by the F3 guard),
commandcode 68 (context fix), models.dev capability/context refresh on 4 rows.

**Carriers still absent from models.dev** are exactly the ones the daily refresh could not
reach (commandcode, xkiro, the opencode-go mirror) — see §5.

## 2. Evidence: 130 live probes (the "we can route to those" part)

Registry fact that changed the picture: every model added on 09-24/25 had **zero tier rows**,
so it read tier −1 and failed every profile bar at level ≥0 (`P0_FORE` needs agent_tick ≥2,
delegation ≥2, schema ≥1, reasoning ≥0, long_doc ≥0). Rows present, routing impossible.

Method (`/home/kara/model_bench/probe_router_candidates.py`, source
`live-probe-2026-09-25`): the same 10-category small-n battery the 2026-09-16 union-alpha
onboarding used — agent_tick, code_gen, debug, delegation, long_doc, long_horizon, reasoning,
schema, test, tool_use — one deterministic probe per category, score = `0.85 × checks_passed`.
13 model strings × 10 categories = 130 rows landed in `data/tables/benchmarks.jsonl` via a new
`BENCH_OVERLAY` pattern, plus 16 `model_aliases.jsonl` rows so the carrier spellings
(`gpt-6-luna`, `xai/grok-4.7`, `claude-opus-5-5`, `Qwen/Qwen3.8-Omni-Flash`, …) inherit them.

Result: **27/29 model spellings now clear every P0_FORE bar** (was 0/29). Score spread is
discriminating, not flat: 107 × 0.85, 11 × 0.64, 7 × 0.57, 2 × 0.21.

Two grader defects were caught **before landing** (worth recording):
- the reasoning probe's answer key was wrong (expects 12:10, my first key said 11:10) — it
  scored four correct answers as failures; fixed, cells re-run.
- the long_doc probe required line-anchored labels, so a model answering all three questions
  correctly on one line scored 0.57; content checks are now unanchored, format is 1 of 4 checks.
Also: `anthropic/claude-opus-5.5`'s agent_tick probe is refused by an upstream content filter on
this lane reprompt-original wording (finish_reason `content_filter`, 0 usage, twice) — the row
says so, and the rephrased prompt is recorded in the source string.

## 3. Routing impact (before/after, live resolver)

- `P0_FORE` for `9router` and `crier`: **0 hops → 12 hops**, and every hop is new-model:
  `xkiro/cohere/command-a-plus $0.00` → `xkiro/mistralai/mistral-large-2512 $0.00` →
  `xkiro/openai/gpt-6-luna` → `xkiro/xiaomi/mimo-v2.6-flash` → `…pro` → `xkiro/x-ai/grok-4.7`
  → `xkiro/openai/gpt-6-sol` → `xkiro/anthropic/claude-opus-5.5` → `xkiro/openai/gpt-6-astra`
  → `openai-codex/gpt-6-luna` …
- `P1_CODING` (coding-hermes-scheduler): head unchanged (`stepfun/step-3.7-flash`), chain 9 → 5 hops.

**Decision for you (the one thing I did not decide alone).** The P0_FORE fixed-point head moved
off `zai-glm/glm-5.3-flash` (the 2026-09-16 golden) onto two **zero-price xkiro lanes**. That is
the registry's own rule (plan_tier first, then price — a $0 plan lane wins), and the xkiro plan
terms say free lanes cost nothing against the pool. But their measured `tool_use` is weak
(`cohere/command-a-plus` 0.21/1.0) and **P0_FORE's bar set has no tool_use requirement**, so a
lane that is poor at tool calls can head the foreman chain on price alone. Options:
(a) add a `tool_use ≥ 2` bar to P0_FORE; (b) stamp a window cost on the zero-price cohere lanes;
(c) accept it. I left the data as measured and the suite's golden head will need the deliberate
update (§6) — say which way and it is a one-line change.

## 4. Two live gate defects found and repaired on the way

1. **openrouter was gated OFF fleet-wide.** Its probe row pointed at `OPENROUTER_API_KEY`, which
   is *expired* (HTTP 401) → provider read DOWN → **all 466 openrouter lanes excluded from every
   chain**, while `GITREINS_OPENROUTER_KEY` answers 200 (verified on `openai/gpt-6-luna`).
   Fixed in `probe_providers.jsonl` (key_env switched) — openrouter now probes OK, 11/11 lanes.
2. **opencode-go family probes lacked the carrier's mandatory session header.** Without
   `x-opencode-session` every ping returns `HTTP 400 MissingSessionID`, which reads as
   provider-DOWN and gated all 85 lanes. With the header the same ping returns
   `HTTP 429 Account budget exceeded` — the real, temporary state: **both opencode-go accounts
   are out of their $12/5h window right now**, so those lanes stay correctly gated until reset.
3. **Provider-breaker cascade (live incident, repaired).** The proxy resolves `--profile P0_FORE`,
   gets no hops, and records that as `record-failure none none "no open hop"`; that also bumps the
   per-provider failure counter, so 8 providers opened PROVIDER-level `api_down` breakers at
   03:49–04:03Z whose reasons quote the proxy's own 503 text — not a provider failure. Those
   breakers then excluded the hops that could have served P0_FORE: self-sustaining emptiness.
   Verified live before acting (api.xkiro.com 200 / 4.2s; 200 on `openai/gpt-6-luna`) and removed
   ONLY the provider-level breakers (pair circuits, e.g. the real clinepass 429, left to cool).
   P0_FORE went 0 → 12 hops immediately. Backup:
   `~/.hermes/model-router/circuit-state.json.bak-breaker-repair-20260925T230639`.
   Filed as **TR-164** (probe `--only` also rewrites the whole provider map — 25 providers
   vanished from `health-state.json` on a partial run).

## 5. Deliberately NOT done (held back, with why)

- **No retirement stamps.** Openrouter's live catalog no longer lists 11 lanes we carry live
  (`anthropic/claude-3-haiku`, `anthropic/claude-opus-4`, `deepseek/deepseek-v4-flash-0731:free`,
  `nex-agi/nex-n2.5-*`, `stealth/union-alpha`, `z-ai/glm-5.2:free`, …), xkiro dropped
  `openai/gpt-5.3-codex-spark`, commandcode dropped `meituan/LongCat-2.0:free`, the mirror dropped
  `union-alpha`. Absence is not a date (doctrine) — filed as **TR-166** for the weekly research
  pass to classify (stamp / remap / whitelist).
- **The still-unevidenced lanes**: `-pro`/`:batch`/ultraspeed SKUs, both stealth lanes
  (`pixel-canary` has no models.dev sticker at all → unpriced gap), `perceptron-mk1.5`,
  `typesafe/jev-router`, `fireworks/ember-1`, `stepfun/step-5-preview`, the 18 new xkiro
  anthropic/cohere spellings → **TR-165**.
- **Adding the four presets to the daily refresh cron** (that is why 46 lanes needed a hand
  edit today) → **TR-167**; **5 duplicate `(provider, model)` pairs** in models.jsonl (pre-existing,
  groq ×3 + synthetic ×2) → **TR-168**.
- Nothing was reverted or hand-tuned to keep a chain head stable; no `perf_*` column was stamped
  from a sibling model (TR-044).

## 6. Suite state

`pytest -q tests/` full run — see the commit message for the result. The golden fixed-point test
is the expected mover: it encodes `P0_FORE = zai-glm/glm-5.3-flash` (2026-09-16) and the measured
fixed point is now `xkiro/cohere/command-a-plus` (§3). Updated deliberately, with the rationale in
the test comment — not pasted from live output.

## Repro

```bash
cd ~/task-router
python3 scripts/router_provider_import.py --preset openrouter --dry-run     # diff only
python3 scripts/router_provider_import.py --preset xkiro|commandcode|opencode-go-2 --dry-run
python3 scripts/router_modelsdev.py fetch && python3 scripts/router_modelsdev.py sync --dry-run
python3 ~/model_bench/probe_router_candidates.py --workers 5               # evidence battery
~/.hermes/venvs/board/bin/python3 scripts/router_seed.py                   # rebuild derived tables
~/.hermes/venvs/board/bin/python3 scripts/router_spawn.py 9router --format json
```

Snapshots of the pre-change tables: `snapshots/2026-09-25/`. Probe results:
`/tmp/probe_2026-09-25.jsonl` (+ `.pre-grader-fix`).

---

## 7. TR-166 classification (2026-10-13) — every §5 candidate closed

This file was removed in cc9ba34 ("accidentally-tracked artifacts") and restored
verbatim (git show cc9ba34^) as the audit source for TR-166; §5's held-back list
is the task's candidate list. Each candidate classified with fresh live evidence
(probes 2026-10-13, keys from ~/.hermes/.env, results below and in
data/lifecycle.jsonl lifecycle_source strings):

| candidate (as filed) | verdict | evidence (2026-10-13 unless noted) |
|---|---|---|
| openrouter anthropic/claude-3-haiku | STAMP | absent from live /models (462 ids); chat 404 quotes the provider's own date: "deprecated on Sep 10, 2026 … migrate to Claude Haiku 4.5"; successor listed + chat 200. valid_to=2026-09-10. |
| openrouter anthropic/claude-opus-4 | STAMP | absent from live /models; chat 404 "No endpoints found". No provider date → valid_to=2026-10-13 (first measured absence); replaced_by claude-opus-5.5 (listed, registry rows). |
| openrouter deepseek/deepseek-v4-flash-0731:free | REMAP | chat 404 names the slug: "use this slug instead: deepseek/deepseek-v4-flash-0731" (listed; registry rows). valid_to=2026-09-29 (TR-217 first absence). |
| openrouter inclusionai/ling-3.0-flash-vl:free | REMAP | chat 404 "use this slug instead: inclusionai/ling-3.0-flash-vl" (listed; paid chat 429 upstream rate-limit = route resolves). valid_to=2026-09-29. |
| openrouter kwaipilot/kat-coder-pro-v2 | already stamped | valid_to=2026-09-29 from TR-217 (absent 460-id catalog + chat 404). Re-verified absent today; v2.5 also absent upstream now — tracked under TR-165 pricing evidence, not re-stamped here. |
| openrouter nex-agi/nex-n2.5-mini(:free) / -pro(:free) | STAMP ×2 + REVERSAL ×2 | paid ids are BACK: listed (462 ids) + chat 200 → the 2026-09-26 DEAD-ID disables on nex-n2.5-mini / nex-n2.5-pro reversed (disabled=false, dated provenance). Both :free forms still retired: chat 404 "use this slug instead: <paid id>" → valid_to=2026-09-26, replaced_by the paid twin. |
| openrouter stealth/union-alpha | REMAP (retired) | chat 404 names the successor: "This model was Unbiased's Pareto. Use it now: …/unbiased/pareto" (pareto-26.9 listed + chat 200). valid_to=2026-10-13, disabled, supersedes the 2026-09-16 endpoint-broken note. |
| openrouter z-ai/glm-5.2:free | REMAP | chat 404 "use this slug instead: z-ai/glm-5.2" (listed + chat 200). valid_to=2026-10-13 (first measured absence; the 09-26 stamp was the age pass, not provider evidence). |
| xkiro openai/gpt-5.3-codex-spark | STAMP (both seats) | absent from live /models on both seats (129 ids each); original 2026-09-18 evidence catalog-absence + chat 404 "does not exist". valid_to=2026-09-18, no successor on the carrier. |
| commandcode meituan/LongCat-2.0:free | REMAP (both seats) | chat 403 quotes the provider: "The free LongCat 2.0 tier has been retired. LongCat 2.0 is now a paid model … (meituan/LongCat-2.0)" (paid id listed both seats; registry rows). valid_to=2026-09-29. |
| opencode-go-2 union-alpha | STAMP (both Go seats) | absent from live opencode.ai/zen/go/v1/models (39 ids, seat 2 identical); no union/stealth successor on the carrier. valid_to=2026-10-13, disabled (supersedes the 2026-09-16 chat-500 note); openrouter successor lane stamped separately. |

Detector whitelist decision: NO new BY_DESIGN pattern warranted. The detector's
`absent` class exists for lanes like these, and every candidate is now either
retired (valid_to set → excluded by `row_is_retired`), disabled, or re-listed.
The only pattern-shaped candidate (union-alpha → pareto) is a one-off promo
retirement with a named successor, not a dynamic route — pattern-whitelisting it
would hide future real retirements of the same id shape.

Post-classification detector state (`router_lifecycle.py`, models.dev cache
2026-10-13): zero of the twelve candidates reappear as `absent` active lanes.
Remaining `absent` entries are the models.dev-lag set TR-166's reasoning already
excludes (same lanes answer on the carriers' own live catalogs — verified for
xkiro/opencode-go/commandcode seats in this pass).

### Record provenance

The classification overlays and this section first landed inside a sibling
cleanup commit (17096cb, bare commit swept the staged paths) without naming
this task in its message. This commit is the TR-166 traceability record: the
content at HEAD is the verified deliverable — 16 overlays in
data/lifecycle.jsonl, 16 updated lanes in data/tables/models.jsonl, all
classified with 2026-10-13 live probes, full suite 2216 passed / 1 skipped.
