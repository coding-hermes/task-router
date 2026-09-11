# TR-035 — weekly research lane 1/3: model-catalog freshness (2026-09-10)

Scope of this lane: **model data only** (catalog/capability/alias freshness).
Lane 2 = provider + plan terms, lane 3 = rankings/misc. Ran on tick
`task-router-2026-09-11-02-03-07`.

## Sources checked (all live, 2026-09-10 21:00–21:20 -05)

| source | what it gave us |
|---|---|
| `https://models.dev/api.json` | 213 providers / 7,663 models (cache `~/.chimera/models-dev-cache.json`, fetched 21:04) |
| `GET {base_url}/models` — every enabled lane in `data/tables/probe_providers.jsonl` | 20 lanes HTTP 200; `kimi` (moonshot.cn) 401 = known wrong-key lane (kimi.com coding key), not a regression |
| direct chat probe (`max_tokens: 4`) on **every** id the live listing omitted | 48 probes → this is what separates "delisted" from "dead" |

## Per-lane result

| lane | GET /v1/models | registry rows (live) | served-but-absent | listed-absent-but-alive | confirmed dead |
|---|---|---|---|---|---|
| clinepass | 200 (437 ids) | 447 | 60 (id-form: registry bare vs vendor-org) | 0 | 0 |
| crof | 200 (17 ids) | 25 | 1 | 8 | 1 |
| deepseek | 200 (2 ids) | 2 | 0 | 0 | 0 |
| deepseek-foreman | 200 (2 ids) | 3 | 1 | 2 | 0 |
| deepseek-duckbrain-sync | 200 (2 ids) | 1 | 2 | 1 | 0 |
| grok-build | 200 (12 ids) | 8 | 4 (imagine image/video — not routed) | 0 | 0 |
| groq | 200 (14 ids) | 9 | 7 (whisper/tts/guard — not routed) | 0 | 2 |
| kimi-for-coding | 200 (4 ids) | 5 | 0 | 0 | 0 (+1 unknown) |
| meta-model | 200 (7 ids) | 5 | 2 (image/voice — not routed) | 0 | 0 |
| minimax | 200 (8 ids) | 9 | 1 | 2 | 0 |
| neuralwatt | 200 (23 ids) | 30 | 3 | 7 | 1 (+2 unknown) |
| ollama-cloud | 200 (20 ids) | 23 | 2 | 0 | 2 (+3 unknown) |
| openai-codex | 200 (138 ids) | 39 | 60 (embedding/image/legacy — not routed) | 0 | 0 |
| opencode-go | 200 (37 ids) | 35 | 2 | 0 | 0 |
| opencode-go-2 | 200 (37 ids) | 24 | 13 | 0 | 0 |
| stepfun | 200 (6 ids) | 5 | 3 (tts/asr/image — not routed) | 0 | 2 |
| synthetic | 200 (11 ids) | 13 | 5 | 0 | 7 |
| zai-glm | 200 (10 ids) | 18 | 0 | 4 | 2 (+2 rate-limited) |
| commandcode / gw-deepseek / muse-spark / myrouter:zai-glm | not probed (no key / not in probe_providers) | — | — | — | — |
| sambanova | disabled by config (dead key, documented) | — | — | — | — |

## Finding 1 — `GET /v1/models` is NOT the routable set (24 of 48 "omitted" ids answered 200)

A model missing from the listing is not a dead model. The zai-glm coding endpoint
(`/api/coding/paas/v4/models`, 10 ids) still serves `glm-4.5-flash`, `glm-4.5v`,
`glm-4.6v` and `glm-4.7-flashx` at HTTP 200; crof serves 8 ids the listing omits;
neuralwatt serves 7. **Rule adopted: retire only on `GET /models` omission AND a
direct chat probe that rejects the id (404/410).** Without the second probe this
tick would have wrongly retired 24 live rows.

## Finding 2 — 18 rows retired (delisted + probe-rejected)

| provider | model | probe |
|---|---|---|
| neuralwatt | deepseek-v4-pro | 404 Model not found |
| neuralwatt | moonshotai/Kimi-K2.5, kimi-k2.5-fast | 410 deprecated → kimi-k2.7 |
| ollama-cloud | gemma-4:31b, nemotron-3-nano | 404 |
| ollama-cloud | kimi-k2.5, minimax-m2.5 | 410 retired 2026-07-31 |
| groq | llama-3.1-8b-instant, llama-3.3-70b-versatile | 404 |
| stepfun | step-1-32k, step-2-16k | 404 |
| crof | deepseek-v4-pro-lightning | 404 Model Not Known |
| synthetic | hf:MiniMaxAI/MiniMax-M3, hf:moonshotai/Kimi-K2.7-Code, hf:Qwen/Qwen3.6-27B | 404 "no longer supported" |
| deepseek | deepseek-v4-flash, deepseek-v4-flash-vision-exp | stale-name rows re-added by the catalog after the provider rename |

Retired as `valid_to: 2026-09-10` (never deleted), matching the `ox-alpha-free`
precedent. `neuralwatt/deepseek-v4-pro` was the only *routable* one (not disabled,
$1.00/M) — the rest were already `disabled`.

## Finding 3 — ESCALATION: the ollama-cloud lane is billing-blocked (402), account-wide

`GET https://ollama.com/v1/models` → 200, but **every** chat completion returns
`402 usage credits auto reload payment failed, update your payment method`
(probed: `deepseek-v4.1-flash`, `deepseek-v4-pro:0813`, `glm-5.2`, `kimi-k2.6`).
This is not model-level and not fixable from this repo — it is the account's
payment method. The lane is unusable until Bane fixes billing at ollama.com.
Note `ollama-cloud` is also a fallback hop in several chains; the router's
health-gate (hourly probe) already shows the lane DOWN.

## Finding 4 — DeepSeek renamed the lineup; sibling lanes lagged

Live `api.deepseek.com/v1/models` now lists exactly `deepseek-flash` +
`deepseek-v4-pro` (the 19:26 commit `6e78368` renamed the main lane but left the
siblings). This tick renamed `deepseek-v4-flash → deepseek-flash` on
`deepseek-foreman`, `deepseek-duckbrain-sync` and `gw-deepseek`, and added the
resolution row `model_aliases.jsonl: deepseek-v4-flash → deepseek-flash` so the
old id keeps resolving for embedders.
The catalog still carries the old ids, so the sync re-added them as unpriced rows —
both retired in the same pass (Finding 2, last row).

## Finding 5 — `opencode-go-2` was a 27 % partial mirror

The 2nd-account lane carried 24 of `opencode-go`'s 35 rows, all byte-identical in
price+evidence. 13 rows added (11 priced mirrors + the 2 new live ids), so both
accounts now expose the same 37 models (verified: 37/37).

## Finding 6 — new live-served models imported (unpriced → lane-2 pricing gaps)

`opencode-go/deepseek-v4.1-flash`, `opencode-go/hy3-preview`,
`crof/deepseek-v4.1-flash`, `ollama-cloud/deepseek-v4.1-flash`,
`ollama-cloud/deepseek-v4-pro:0813`, `neuralwatt/glm-5.3-flex`,
`neuralwatt/qwen-3.8-27b-flex`, `neuralwatt/qwen3.6-35b-flex` (+ their
opencode-go-2 mirrors) — rows added with `normalized_price: null` and
`price_evidence: live-listing-2026-09-10`, i.e. **flagged gaps for lane 2 /
the reprice pass**, exactly as TR-019 prescribes. They are inert until priced.

## Finding 7 — the actual defect this lane existed to catch (now fixed in code)

`router_modelsdev.py sync` filled `context_limit`/`vision`/`thinking` only on rows
it ADDED; existing rows kept `null` forever and provider capability changes never
propagated (24 live rows had a null context although the catalog carried one).
`968fc28` makes the sync refresh those fields on existing rows (fill-when-null,
overwrite-on-diff, retired rows frozen, price fields untouched, `refreshed` +
`refreshed_rows` in the JSON summary, 10 hermetic tests). First live run refreshed
8 fields on 4 rows (`grok-build/grok-imagine-image-2.0`,
`groq/qwen/qwen3.8-27b`, `opencode-go/muse-spark-1.3-contributor`,
`opencode-go/omen-alpha`).
The remaining ~20 nulls have **no** catalog value for their exact
(provider, model) pair (reseller lanes whose upstream ids the catalog stores
under a different name) — they stay visible in `router_gaps.py` for lane 2/3.

## Other observations (not fixed here)

- Pre-existing duplicate live keys in `models.jsonl`: `groq/openai/gpt-oss-120b`,
  `groq/openai/gpt-oss-20b`, `groq/qwen/qwen3.6-27b`, `synthetic/hf:openai/gpt-oss-120b`.
- CI on `main` is **RED** since `6e78368` (4 tests still index the pre-rename
  deepseek id). Filed as **TR-038** with the reproduction.
- Every commit in this repo is authored `CI Test <ci@test.local>` (repo-local git
  config), not the fleet's standard author identity — flagged for hygiene, not
  changed here (it is repo-wide, not tick-scoped).

## Closeout

- registry: 786 → 809 rows (26 appended, 3 renamed, 18 retired, 4 capability-refreshed)
- generated path respected: `router_modelsdev.py sync` → `router_seed.py` → export to
  BOTH ns repos (`routing` + `task-router`) → `registry.json` rebuilt. No hand-edited
  ns table.
- chain-head sanity: `router_spawn.py coding-hermes-scheduler` and `hivemind-work`
  both resolve a head (non-empty chain) after the change.
- Evidence artefacts: `/tmp/lane1_live.json` (per-lane listings),
  `/tmp/lane1_retire_probe.json` (48 chat probes), `/tmp/mdev-dryrun.json`.
