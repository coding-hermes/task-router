# Last-quarter model lifecycle prune — task-router registry — 2026-09-26

Bane directive 2026-09-26: keep the routable model list inside roughly the last
quarter (~90 days), keeping the most recent model of the same tier by lineage name or
by cost; retire the lanes that are surely superceded. DATA pass — no code changed,
no generated table hand-edited.

Policy executed: `models-dev-registry-crosscheck` → section **LAST-QUARTER LIFECYCLE
POLICY (Bane 2026-09-26)**, applied literally, including its never-stamp classes.

## Result in one line

**200 lanes stamped retired** (each with a named successor and a models.dev-sourced
date), bringing the digest from `live 1584 / retired 25` to `live 1383 / retired 226`.
No profile head moved. No pinned lane touched. 353 old lanes were deliberately NOT
stamped because they have no newer same-tier sibling (the policy's own rule).

## The two cases Bane named (confirmed with real dates, not assumed)

| case | catalog date | age | newer same-lineage sibling | successor carried+enabled? | verdict |
|---|---|---|---|---|---|
| `stepfun/step-3.5-flash` | models.dev stepfun `release_date 2026-01-29` | 240d | `step-3.7-flash` (2026-05-29), `step-5-preview` (2026-09-16) | yes for 3.7 (in-plan); 5-preview is DISABLED in our registry | **STAMPED** → `stepfun/step-3.7-flash` |
| `xkiro/qwen/qwen3-coder-plus:free` | xkiro is NOT on models.dev → mirror date `2025-07-23` (8/8 catalog providers agree) | 430d | coder line: `qwen3-coder-next` (2026-02-03) — **not carried on xkiro**; plus tier: `qwen3.7-plus:free` (2026-06-02) | `qwen3.7-plus:free` yes ($0 ↔ $0, same plus tier) | **STAMPED** → `xkiro/qwen/qwen3.7-plus:free` (track B, cost tier equal) |

Both were confirmed from dates, and both are now visible as `retired` in the digest.

## MANDATORY COUNTS

| counter | value |
|---|---|
| registry rows (all) | 1609 |
| **scanned** — ENABLED lanes (not archive, not disabled, no valid_to) | **945** |
| …of which carry a catalog release_date (own provider or agreeing mirror) | 906 |
| …**unresolved dates** (no catalog row anywhere → never stamped) | **39** |
| …inside the 90-day gate (young, not eligible) | 321 |
| …older than 90 days | 585 |
| …older AND having a newer same-tier sibling (candidates) | 232 |
| **stamped** (retirement overlays written) | **200** |
| skipped — ID-SHAPE / TAG-SHAPE drift (remap candidates, never stamped) | 32 |
| retained — no newer sibling at all | 132 |
| retained — newer sibling in a different tier/size | 97 |
| retained — id carries no generation (cannot be ordered) | 90 |
| retained — newer date but not a newer generation | 15 |
| retained — newer sibling exists but is OFF in our registry | 8 |
| retained — role generalized but a different cost tier | 7 |
| retained — sibling has a different role/modality | 4 |
| retained — PINNED (would have been stamped otherwise) | 0 |

Date provenance of the 200 stamped rows: models.dev openrouter ×104, models.dev mirror ×58, models.dev amazon-bedrock ×13, models.dev opencode-go ×10, models.dev neuralwatt ×5, models.dev ollama-cloud ×3, models.dev mirror (single provider) ×2, models.dev xai ×1, models.dev groq ×1, models.dev minimax ×1, models.dev stepfun ×1, models.dev zai ×1.

Retention by pins — the pin surfaces checked, and why nothing was blocked: every pinned
pair is either not a candidate at all or was already newer than the gate:

| pin surface | pinned lane(s) | effect on this pass |
|---|---|---|
| `~/.hermes/fleet.toml` (22 lanes) | `deepseek-foreman/deepseek-v4-pro` ×21, `deepseek-foreman/deepseek-v4-flash` ×1 | not candidates (deepseek-foreman lanes are young/current) |
| `scheduler.db → projects.model/provider` | `deepseek-foreman/deepseek-v4-flash`, `deepseek-foreman/deepseek-v4-pro` | same pair as above — untouched |
| `scheduler.db → projects.worker_model/provider` | `openai-codex/gpt-5.6-sol`, `zai-glm/glm-5.3-flash` | `gpt-5.6-sol` is 79d old (inside the gate); `glm-5.3-flash` is the newest of its lineage |
| systemd `SCHEDULER_FOREMAN_MODEL` | `xkiro/z-ai/glm-5.3-flash` | retained (newest glm flash on xkiro) — and 3 stamped lanes now POINT AT it |
| systemd `SCHEDULER_FOREMAN_FALLBACK_MODEL` | `deepseek-foreman/deepseek-flash` | not a candidate |
| `~/.hermes/config.yaml` default + fallback chain | `glm-5.3-flash`, `deepseek-flash`, `deepseek-v4-flash`, `z-ai/glm-5.3-flash` | not candidates (all current) |
| profile `eduos-e2e-tester/config.yaml` | `minimax/MiniMax-M1` | names a lane that is NOT enabled in the registry (M1 is absent; M2/M2.1/M2.5/M2.7/M3 are) — no effect |
| `~/.hermes/scripts/{dispatch-worker,sched-tick630-wave}.sh` | `glm-5.3-flash` | successor target, not a candidate |
| `~/.hermes/scripts/trouble-wave-redispatch.sh` | `cmc/deepseek/deepseek-v4-flash` | 9router alias, not a registry lane |
| `task-router-proxy.service` | `ROUTER_CLASSIFIER_MODEL=deepseek-flash` | not a candidate |

One name-level near-miss, kept deliberately and disclosed: `neuralwatt/deepseek-v4-flash`
is stamped (`→ neuralwatt/deepseek-v4.1-flash`, both carried on neuralwatt); the pin
names the *provider pair* `deepseek-foreman/deepseek-v4-flash`, which is a different lane
and is untouched. The pinned lane itself is not a candidate.

## The rule as implemented (what "same tier" and "surely superceded" meant in code)

1. **Date** — models.dev `release_date` for the lane's own provider when the provider is
   on models.dev; otherwise a model-identity mirror: the same model id under other
   catalog providers must AGREE (mode ≥60% of mirrors) before the date is used.
   `valid_from` was never used as a release date. No date → not stamped (39 lanes).
2. **Lineage** — parsed from the model id itself (namespace + base name), because
   models.dev `family` is `null` for whole families (stepfun `step-*`,
   `qwen3-coder-plus`); `family` was used only as corroboration.
3. **Same tier** — qualitative tier tokens must match exactly (`mini/nano/pro/plus/
   flash/flex/fast/max`), numeric sizes must pair up within ±40% (`gemma-3-27b` →
   `gemma-4-31b` is the same tier; `8b` → `70b` is not).
4. **Same role** — modality roles (`vl/vision/audio/image/embed/guard/rerank/omni`)
   can never be dropped by a successor; only `coder/code/thinking/reasoning` may be
   generalized away, and then only at an equal cost tier (this is the "by cost" clause).
   A trailing `v` (`glm-4.5v`) is VISION, not part of the version — the naive parse
   wrongly offered text `glm-5.3` as its successor; fixed before stamping.
5. **Newer generation** — the successor must be a strictly newer generation, not merely
   a newer date (`gpt-4-turbo` vs `gpt-3.5-turbo-0613`: the 2024 snapshot is NEWER by
   date and OLDER by generation — 15 lanes are held back by this check alone).
6. **Successor must be an enabled lane of the SAME provider** — otherwise retiring
   removes a capability with nothing to route to (8 lanes are held back for this and are
   listed below as enable-candidates).
7. **Never stamped** — the digest's ID-SHAPE/TAG-SHAPE/LISTING-DRIFT rows (32),
   anything pinned, and any lane with an unknown date.
8. **Plan value preserved** — a plan-included lane is only stamped when its successor is
   ALSO plan-included (%d such stamps, all satisfied; this is the 2026-09-16
   plan-inclusion doctrine applied to the new policy rather than re-litigated).

## Stamped lanes (200) — grouped by provider

### openrouter — 106

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `openai/gpt-3.5-turbo` | 2023-03-01 | 1305d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-3.5-turbo-instruct` | 2023-09-28 | 1094d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-4` | 2023-11-06 | 1055d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-4-turbo` | 2023-11-06 | 1055d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-3.5-turbo-0613` | 2024-01-25 | 975d | `openai/gpt-5.5` | 2026-04-23 | A |
| `anthropic/claude-3-haiku` | 2024-03-13 | 927d | `anthropic/claude-haiku-4.5` | 2025-10-15 | A |
| `google/gemma-2-27b-it` | 2024-07-13 | 805d | `google/gemma-4-31b-it` | 2026-04-02 | A |
| `meta-llama/llama-3.1-70b-instruct` | 2024-07-23 | 795d | `meta-llama/llama-3.3-70b-instruct` | 2024-12-06 | A |
| `sao10k/l3.1-euryale-70b` | 2024-08-28 | 759d | `sao10k/l3.3-euryale-70b` | 2024-12-18 | A |
| `qwen/qwen-2.5-7b-instruct` | 2024-10-16 | 710d | `qwen/qwen3.5-9b` | 2026-02-23 | A |
| `qwen/qwen-2.5-coder-32b-instruct` | 2024-11-11 | 684d | `qwen/qwen3.8-27b` | 2026-08-14 | B |
| `mistralai/mistral-large-2407` | 2024-11-19 | 676d | `mistralai/mistral-large-2512:batch` | 2025-12-02 | A |
| `openai/o1` | 2024-12-05 | 660d | `openai/o3` | 2025-04-16 | A |
| `openai/o3-mini` | 2024-12-20 | 645d | `openai/o4-mini` | 2025-04-16 | A |
| `openai/o3-mini-high` | 2025-02-12 | 591d | `openai/o4-mini-high` | 2025-04-16 | A |
| `google/gemma-3-27b-it` | 2025-03-12 | 563d | `google/gemma-4-31b-it` | 2026-04-02 | A |
| `mistralai/mistral-small-3.1-24b-instruct` | 2025-03-17 | 558d | `mistralai/mistral-small-3.2-24b-instruct` | 2025-06-20 | A |
| `openai/o1-pro` | 2025-03-19 | 556d | `openai/o3-pro` | 2025-06-10 | A |
| `deepseek/deepseek-chat-v3-0324` | 2025-03-24 | 551d | `deepseek/deepseek-v3.2` | 2025-12-01 | A |
| `qwen/qwen3-32b` | 2025-04-01 | 543d | `qwen/qwen3.8-27b` | 2026-08-14 | A |
| `openai/gpt-4.1` | 2025-04-14 | 530d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-4.1-mini` | 2025-04-14 | 530d | `openai/gpt-5.4-mini` | 2026-03-17 | A |
| `openai/gpt-4.1-nano` | 2025-04-14 | 530d | `openai/gpt-5.4-nano` | 2026-03-17 | A |
| `qwen/qwen3-14b` | 2025-04-28 | 516d | `qwen/qwen3.5-9b` | 2026-02-23 | A |
| `qwen/qwen3-30b-a3b` | 2025-04-28 | 516d | `qwen/qwen3.6-35b-a3b` | 2026-04-17 | A |
| `qwen/qwen3-8b` | 2025-04-28 | 516d | `qwen/qwen3.5-9b` | 2026-02-23 | A |
| `mistralai/mistral-medium-3` | 2025-05-07 | 507d | `mistralai/mistral-medium-3.1` | 2025-08-13 | A |
| `anthropic/claude-opus-4` | 2025-05-14 | 500d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `anthropic/claude-sonnet-4` | 2025-05-22 | 492d | `anthropic/claude-sonnet-5` | 2026-06-30 | A |
| `google/gemini-2.5-pro-preview` | 2025-06-05 | 478d | `google/gemini-3.1-pro-preview` | 2026-02-19 | A |
| `google/gemini-2.5-flash` | 2025-06-17 | 466d | `google/gemini-3.8-flash` | 2026-09-02 | A |
| `google/gemini-2.5-flash-lite` | 2025-06-17 | 466d | `google/gemini-3.5-flash-lite` | 2026-07-21 | A |
| `google/gemini-2.5-pro` | 2025-06-17 | 466d | `google/gemini-3.1-pro-preview` | 2026-02-19 | A |
| `minimax/minimax-m1` | 2025-06-17 | 466d | `minimax/minimax-m3` | 2026-06-01 | A |
| `moonshotai/kimi-k2` | 2025-07-11 | 442d | `moonshotai/kimi-k3` | 2026-07-16 | A |
| `qwen/qwen3-coder-flash` | 2025-07-28 | 425d | `qwen/qwen3.8-flash` | 2026-08-26 | B |
| `z-ai/glm-4.5` | 2025-07-28 | 425d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `qwen/qwen3-30b-a3b-instruct-2507` | 2025-07-29 | 424d | `qwen/qwen3.6-35b-a3b` | 2026-04-17 | A |
| `anthropic/claude-opus-4.1` | 2025-08-05 | 417d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `openai/gpt-5` | 2025-08-07 | 415d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-5-mini` | 2025-08-07 | 415d | `openai/gpt-5.4-mini` | 2026-03-17 | A |
| `openai/gpt-5-nano` | 2025-08-07 | 415d | `openai/gpt-5.4-nano` | 2026-03-17 | A |
| `z-ai/glm-4.5v` | 2025-08-11 | 411d | `z-ai/glm-5v-turbo` | 2026-04-01 | A |
| `deepseek/deepseek-chat-v3.1` | 2025-08-21 | 401d | `deepseek/deepseek-v3.2` | 2025-12-01 | A |
| `google/gemini-2.5-flash-image` | 2025-08-26 | 396d | `google/gemini-3.1-flash-image` | 2026-05-28 | A |
| `qwen/qwen3-30b-a3b-thinking-2507` | 2025-08-28 | 394d | `qwen/qwen3.6-35b-a3b` | 2026-04-17 | B |
| `moonshotai/kimi-k2-0905` | 2025-09-04 | 387d | `moonshotai/kimi-k3` | 2026-07-16 | A |
| `qwen/qwen3-max` | 2025-09-23 | 368d | `qwen/qwen3.8-max-0902` | 2026-09-02 | A |
| `anthropic/claude-sonnet-4.5` | 2025-09-29 | 362d | `anthropic/claude-sonnet-5` | 2026-06-30 | A |
| `z-ai/glm-4.6` | 2025-09-30 | 361d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `openai/gpt-5-pro` | 2025-10-06 | 355d | `openai/gpt-5.5-pro` | 2026-04-23 | A |
| `openai/gpt-5-image` | 2025-10-14 | 347d | `openai/gpt-5.4-image-2` | 2026-04-21 | A |
| `minimax/minimax-m2` | 2025-10-27 | 334d | `minimax/minimax-m3` | 2026-06-01 | A |
| `moonshotai/kimi-k2-thinking` | 2025-11-06 | 324d | `moonshotai/kimi-k2.5` | 2026-01-01 | B |
| `openai/gpt-5.1` | 2025-11-13 | 317d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-5.1-codex` | 2025-11-13 | 317d | `openai/gpt-5.3-codex` | 2026-02-05 | A |
| `anthropic/claude-opus-4.5` | 2025-11-24 | 306d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `z-ai/glm-4.6v` | 2025-12-08 | 292d | `z-ai/glm-5v-turbo` | 2026-04-01 | A |
| `openai/gpt-5.2-chat` | 2025-12-10 | 290d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-5.2` | 2025-12-11 | 289d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-5.2-codex` | 2025-12-11 | 289d | `openai/gpt-5.3-codex` | 2026-02-05 | A |
| `openai/gpt-5.2-pro` | 2025-12-11 | 289d | `openai/gpt-5.5-pro` | 2026-04-23 | A |
| `google/gemini-3-flash-preview` | 2025-12-17 | 283d | `google/gemini-3.8-flash` | 2026-09-02 | A |
| `z-ai/glm-4.7` | 2025-12-22 | 278d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `bytedance-seed/seed-1.6` | 2025-12-23 | 277d | `bytedance-seed/seed-2-1-turbo` | 2026-08-12 | A |
| `minimax/minimax-m2.1` | 2025-12-23 | 277d | `minimax/minimax-m3` | 2026-06-01 | A |
| `moonshotai/kimi-k2.5` | 2026-01-01 | 268d | `moonshotai/kimi-k3` | 2026-07-16 | A |
| `z-ai/glm-4.7-flash` | 2026-01-19 | 250d | `z-ai/glm-5.3-flash` | 2026-08-26 | A |
| `stepfun/step-3.5-flash` | 2026-01-29 | 240d | `stepfun/step-3.7-flash` | 2026-05-29 | A |
| `anthropic/claude-opus-4.6` | 2026-02-05 | 233d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `qwen/qwen3-max-thinking` | 2026-02-09 | 229d | `qwen/qwen3.6-max-preview` | 2026-04-20 | B |
| `minimax/minimax-m2.5` | 2026-02-12 | 226d | `minimax/minimax-m3` | 2026-06-01 | A |
| `z-ai/glm-5` | 2026-02-12 | 226d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `qwen/qwen3.5-plus-02-15` | 2026-02-16 | 222d | `qwen/qwen3.7-plus` | 2026-06-02 | A |
| `anthropic/claude-sonnet-4.6` | 2026-02-17 | 221d | `anthropic/claude-sonnet-5` | 2026-06-30 | A |
| `aion-labs/aion-2.0` | 2026-02-23 | 215d | `aion-labs/aion-3.5` | 2026-09-23 | A |
| `qwen/qwen3.5-27b` | 2026-02-23 | 215d | `qwen/qwen3.8-27b` | 2026-08-14 | A |
| `qwen/qwen3.5-35b-a3b` | 2026-02-23 | 215d | `qwen/qwen3.6-35b-a3b` | 2026-04-17 | A |
| `qwen/qwen3.5-flash-02-23` | 2026-02-25 | 213d | `qwen/qwen3.8-flash` | 2026-08-26 | A |
| `google/gemini-3.1-flash-lite-preview` | 2026-03-03 | 207d | `google/gemini-3.5-flash-lite` | 2026-07-21 | A |
| `inception/mercury-2` | 2026-03-04 | 206d | `inception/mercury-2.5` | 2026-09-08 | A |
| `openai/gpt-5.4` | 2026-03-05 | 205d | `openai/gpt-5.5` | 2026-04-23 | A |
| `openai/gpt-5.4-pro` | 2026-03-05 | 205d | `openai/gpt-5.5-pro` | 2026-04-23 | A |
| `z-ai/glm-5-turbo` | 2026-03-16 | 194d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `minimax/minimax-m2.7` | 2026-03-18 | 192d | `minimax/minimax-m3` | 2026-06-01 | A |
| `qwen/qwen3.6-plus` | 2026-04-02 | 177d | `qwen/qwen3.7-plus` | 2026-06-02 | A |
| `z-ai/glm-5.1` | 2026-04-07 | 172d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `meta/muse-spark-1.1` | 2026-04-08 | 171d | `meta/muse-spark-1.3` | 2026-09-02 | A |
| `anthropic/claude-opus-4.7` | 2026-04-16 | 163d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `x-ai/grok-4.3` | 2026-04-17 | 162d | `x-ai/grok-4.7` | 2026-09-21 | A |
| `qwen/qwen3.6-max-preview` | 2026-04-20 | 159d | `qwen/qwen3.8-max-0902` | 2026-09-02 | A |
| `tencent/hy3-preview` | 2026-04-20 | 159d | `tencent/hy4-preview` | 2026-08-28 | A |
| `moonshotai/kimi-k2.6` | 2026-04-21 | 158d | `moonshotai/kimi-k3` | 2026-07-16 | A |
| `qwen/qwen3.6-27b` | 2026-04-22 | 157d | `qwen/qwen3.8-27b` | 2026-08-14 | A |
| `xiaomi/mimo-v2.5-pro` | 2026-04-22 | 157d | `xiaomi/mimo-v2.6-pro` | 2026-09-22 | A |
| `deepseek/deepseek-v4-flash` | 2026-04-24 | 155d | `deepseek/deepseek-v4.1-flash` | 2026-09-10 | A |
| `qwen/qwen3.5-plus-20260420` | 2026-04-27 | 152d | `qwen/qwen3.7-plus` | 2026-06-02 | A |
| `qwen/qwen3.6-flash` | 2026-04-27 | 152d | `qwen/qwen3.8-flash` | 2026-08-26 | A |
| `google/gemini-3.1-flash-lite` | 2026-05-07 | 142d | `google/gemini-3.5-flash-lite` | 2026-07-21 | A |
| `perceptron/perceptron-mk1` | 2026-05-12 | 137d | `perceptron/perceptron-mk1.5` | 2026-09-25 | A |
| `google/gemini-3.5-flash` | 2026-05-19 | 130d | `google/gemini-3.8-flash` | 2026-09-02 | A |
| `qwen/qwen3.7-max` | 2026-05-21 | 128d | `qwen/qwen3.8-max-0902` | 2026-09-02 | A |
| `anthropic/claude-opus-4.8` | 2026-05-28 | 121d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `anthropic/claude-fable-5` | 2026-06-09 | 109d | `anthropic/claude-fable-5.1` | 2026-09-01 | A |
| `z-ai/glm-5.2` | 2026-06-13 | 105d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `z-ai/glm-5.2:free` | 2026-06-13 | 105d | `z-ai/glm-5.3` | 2026-08-14 | A |

### xkiro — 33

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `anthropic/claude-sonnet-4` | 2025-05-22 | 492d | `anthropic/claude-sonnet-5` | 2026-06-30 | A |
| `qwen/qwen3-coder-plus:free` | 2025-07-23 | 430d | `qwen/qwen3.7-plus:free` | 2026-06-02 | B |
| `z-ai/glm-4.5` | 2025-07-28 | 425d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `z-ai/glm-4.5-flash` | 2025-07-28 | 425d | `z-ai/glm-5.3-flash` | 2026-08-26 | A |
| `z-ai/glm-4.5v` | 2025-08-11 | 411d | `z-ai/glm-5v-turbo` | 2026-04-01 | A |
| `qwen/qwen3-omni-flash:free` | 2025-09-15 | 376d | `qwen/qwen3.8-omni-flash:free` | 2026-09-17 | A |
| `qwen/qwen3-max:free` | 2025-09-23 | 368d | `qwen/qwen3.8-max:free` | 2026-08-03 | A |
| `anthropic/claude-sonnet-4.5` | 2025-09-29 | 362d | `anthropic/claude-sonnet-5` | 2026-06-30 | A |
| `z-ai/glm-4.6` | 2025-09-30 | 361d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `minimax/minimax-m2:free` | 2025-10-27 | 334d | `minimax/minimax-m3:free` | 2026-06-01 | A |
| `anthropic/claude-opus-4.5` | 2025-11-24 | 306d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `z-ai/glm-4.6v` | 2025-12-08 | 292d | `z-ai/glm-5v-turbo` | 2026-04-01 | A |
| `z-ai/glm-4.7` | 2025-12-22 | 278d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `minimax/minimax-m2.1:free` | 2025-12-23 | 277d | `minimax/minimax-m3:free` | 2026-06-01 | A |
| `z-ai/glm-4.7-flash` | 2026-01-19 | 250d | `z-ai/glm-5.3-flash` | 2026-08-26 | A |
| `anthropic/claude-opus-4.6` | 2026-02-05 | 233d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `minimax/minimax-m2.5:free` | 2026-02-12 | 226d | `minimax/minimax-m3:free` | 2026-06-01 | A |
| `z-ai/glm-5` | 2026-02-12 | 226d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `minimax/minimax-m2.5-highspeed:free` | 2026-02-13 | 225d | `minimax/minimax-m2.7-highspeed:free` | 2026-03-18 | A |
| `qwen/qwen3.5-plus:free` | 2026-02-16 | 222d | `qwen/qwen3.7-plus:free` | 2026-06-02 | A |
| `anthropic/claude-sonnet-4.6` | 2026-02-17 | 221d | `anthropic/claude-sonnet-5` | 2026-06-30 | A |
| `qwen/qwen3.5-flash:free` | 2026-02-23 | 215d | `qwen/qwen3.7-flash:free` | 2026-07-15 | A |
| `openai/gpt-5.4` | 2026-03-05 | 205d | `openai/gpt-5.5` | 2026-04-23 | A |
| `z-ai/glm-5-turbo` | 2026-03-16 | 194d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `minimax/minimax-m2.7:free` | 2026-03-18 | 192d | `minimax/minimax-m3:free` | 2026-06-01 | A |
| `qwen/qwen3.5-omni-flash:free` | 2026-03-30 | 180d | `qwen/qwen3.8-omni-flash:free` | 2026-09-17 | A |
| `qwen/qwen3.6-plus:free` | 2026-04-02 | 177d | `qwen/qwen3.7-plus:free` | 2026-06-02 | A |
| `z-ai/glm-5.1` | 2026-04-07 | 172d | `z-ai/glm-5.3` | 2026-08-14 | A |
| `anthropic/claude-opus-4.7` | 2026-04-16 | 163d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `qwen/qwen3.6-max-preview:free` | 2026-04-20 | 159d | `qwen/qwen3.8-max:free` | 2026-08-03 | A |
| `qwen/qwen3.7-max:free` | 2026-05-21 | 128d | `qwen/qwen3.8-max:free` | 2026-08-03 | A |
| `anthropic/claude-opus-4.8` | 2026-05-28 | 121d | `anthropic/claude-opus-5.5` | 2026-09-22 | A |
| `z-ai/glm-5.2` | 2026-06-13 | 105d | `z-ai/glm-5.3` | 2026-08-14 | A |

### commandcode — 18

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `stepfun/Step-3.5-Flash` | 2026-01-29 | 240d | `stepfun/Step-3.7-Flash` | 2026-05-29 | A |
| `MiniMaxAI/MiniMax-M2.5` | 2026-02-12 | 226d | `MiniMaxAI/MiniMax-M3` | 2026-06-01 | A |
| `claude-sonnet-4-6` | 2026-02-17 | 221d | `claude-sonnet-5` | 2026-06-30 | A |
| `gpt-5.4` | 2026-03-05 | 205d | `gpt-5.5` | 2026-04-23 | A |
| `MiniMaxAI/MiniMax-M2.7` | 2026-03-18 | 192d | `MiniMaxAI/MiniMax-M3` | 2026-06-01 | A |
| `Qwen/Qwen3.6-Plus` | 2026-04-02 | 177d | `Qwen/Qwen3.7-Plus` | 2026-06-02 | A |
| `zai-org/GLM-5.1` | 2026-04-07 | 172d | `zai-org/GLM-5.3` | 2026-08-14 | A |
| `meta/muse-spark-1.1` | 2026-04-08 | 171d | `meta/muse-spark-1.3` | 2026-09-02 | A |
| `claude-opus-4-7` | 2026-04-16 | 163d | `claude-opus-5-5` | 2026-09-22 | A |
| `Qwen/Qwen3.6-Max-Preview` | 2026-04-20 | 159d | `Qwen/Qwen3.8-Max-0902` | 2026-09-02 | A |
| `moonshotai/Kimi-K2.6` | 2026-04-21 | 158d | `moonshotai/Kimi-K3` | 2026-07-16 | A |
| `xiaomi/mimo-v2.5-pro` | 2026-04-22 | 157d | `xiaomi/mimo-v2.6-pro` | 2026-09-22 | A |
| `deepseek/deepseek-v4-flash` | 2026-04-24 | 155d | `deepseek/deepseek-v4.1-flash` | 2026-09-10 | A |
| `google/gemini-3.1-flash-lite` | 2026-05-07 | 142d | `google/gemini-3.5-flash-lite` | 2026-07-21 | A |
| `google/gemini-3.5-flash` | 2026-05-19 | 130d | `google/gemini-3.8-flash` | 2026-09-02 | A |
| `Qwen/Qwen3.7-Max` | 2026-05-21 | 128d | `Qwen/Qwen3.8-Max-0902` | 2026-09-02 | A |
| `claude-opus-4-8` | 2026-05-28 | 121d | `claude-opus-5-5` | 2026-09-22 | A |
| `zai-org/GLM-5.2` | 2026-06-13 | 105d | `zai-org/GLM-5.3` | 2026-08-14 | A |

### aws-bedrock — 13

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `anthropic.claude-opus-4-1-20250805-v1:0` | 2025-08-05 | 417d | `anthropic.claude-opus-5` | 2026-07-24 | A |
| `deepseek.v3-v1:0` | 2025-08-21 | 401d | `deepseek.v3.2` | 2025-12-01 | A |
| `anthropic.claude-sonnet-4-5-20250929-v1:0` | 2025-09-29 | 362d | `anthropic.claude-sonnet-5` | 2026-06-30 | A |
| `jp.anthropic.claude-sonnet-4-5-20250929-v1:0` | 2025-09-29 | 362d | `jp.anthropic.claude-sonnet-5` | 2026-06-30 | A |
| `anthropic.claude-opus-4-5-20251101-v1:0` | 2025-11-01 | 329d | `anthropic.claude-opus-5` | 2026-07-24 | A |
| `zai.glm-4.7` | 2025-12-22 | 278d | `zai.glm-5` | 2026-02-12 | A |
| `anthropic.claude-opus-4-6-v1` | 2026-02-05 | 233d | `anthropic.claude-opus-5` | 2026-07-24 | A |
| `anthropic.claude-sonnet-4-6` | 2026-02-17 | 221d | `anthropic.claude-sonnet-5` | 2026-06-30 | A |
| `jp.anthropic.claude-sonnet-4-6` | 2026-02-17 | 221d | `jp.anthropic.claude-sonnet-5` | 2026-06-30 | A |
| `anthropic.claude-opus-4-7` | 2026-04-16 | 163d | `anthropic.claude-opus-5` | 2026-07-24 | A |
| `jp.anthropic.claude-opus-4-7` | 2026-04-16 | 163d | `jp.anthropic.claude-opus-5` | 2026-07-24 | A |
| `anthropic.claude-opus-4-8` | 2026-05-28 | 121d | `anthropic.claude-opus-5` | 2026-07-24 | A |
| `jp.anthropic.claude-opus-4-8` | 2026-05-28 | 121d | `jp.anthropic.claude-opus-5` | 2026-07-24 | A |

### opencode-go — 6

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `qwen3.6-plus` | 2026-04-02 | 177d | `qwen3.7-plus` | 2026-06-02 | A |
| `hy3-preview` | 2026-04-20 | 159d | `hy4-preview` | 2026-08-28 | A |
| `kimi-k2.6` | 2026-04-21 | 158d | `kimi-k3` | 2026-07-16 | A |
| `mimo-v2.5-pro` | 2026-04-22 | 157d | `mimo-v2.6-pro` | 2026-09-22 | A |
| `qwen3.7-max` | 2026-05-21 | 128d | `qwen3.8-max` | 2026-08-03 | A |
| `glm-5.2` | 2026-06-13 | 105d | `glm-5.3` | 2026-08-14 | A |

### opencode-go-2 — 6

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `qwen3.6-plus` | 2026-04-02 | 177d | `qwen3.7-plus` | 2026-06-02 | A |
| `hy3-preview` | 2026-04-20 | 159d | `hy4-preview` | 2026-08-28 | A |
| `kimi-k2.6` | 2026-04-21 | 158d | `kimi-k3` | 2026-07-16 | A |
| `mimo-v2.5-pro` | 2026-04-22 | 157d | `mimo-v2.6-pro` | 2026-09-22 | A |
| `qwen3.7-max` | 2026-05-21 | 128d | `qwen3.8-max` | 2026-08-03 | A |
| `glm-5.2` | 2026-06-13 | 105d | `glm-5.3` | 2026-08-14 | A |

### neuralwatt — 5

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `qwen3.6-35b-flex` | 2026-04-17 | 162d | `qwen-3.8-27b-flex` | 2026-08-14 | A |
| `deepseek-v4-flash` | 2026-04-24 | 155d | `deepseek-v4.1-flash` | 2026-09-10 | A |
| `deepseek-v4-flash-flex` | 2026-04-24 | 155d | `deepseek-v4.1-flash-flex` | 2026-09-10 | A |
| `glm-5.2` | 2026-06-17 | 101d | `glm-5.3` | 2026-08-14 | A |
| `glm-5.2-flex` | 2026-06-17 | 101d | `glm-5.3-flex` | 2026-08-14 | A |

### clinepass — 4

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `kimi-k2.6` | 2026-04-21 | 158d | `kimi-k3` | 2026-07-16 | A |
| `qwen3.7-max` | 2026-05-21 | 128d | `qwen3.8-max` | 2026-08-03 | A |
| `glm-5.2` | 2026-06-13 | 105d | `glm-5.3` | 2026-08-14 | A |
| `glm-5.2:free` | 2026-06-13 | 105d | `glm-5.3` | 2026-08-14 | A |

### ollama-cloud — 3

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `minimax-m2.7` | 2026-03-18 | 192d | `minimax-m3` | 2026-05-31 | A |
| `kimi-k2.6` | 2026-04-20 | 159d | `kimi-k3` | 2026-07-16 | A |
| `glm-5.2` | 2026-06-13 | 105d | `glm-5.3` | 2026-08-14 | A |

### grok-build — 1

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `grok-4.3` | 2026-04-17 | 162d | `grok-4.6` | 2026-08-12 | A |

### groq — 1

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `qwen/qwen3.6-27b` | 2026-04-22 | 157d | `qwen/qwen3.8-27b` | 2026-08-14 | A |

### meta-model — 1

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `muse-spark-1.1` | 2026-04-08 | 171d | `muse-spark-1.3` | 2026-09-02 | A |

### minimax — 1

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `MiniMax-M2.5` | 2026-02-12 | 226d | `minimax-m3` | 2026-06-01 | A |

### stepfun — 1

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `step-3.5-flash` | 2026-01-29 | 240d | `step-3.7-flash` | 2026-05-29 | A |

### zai-glm — 1

| lane | released | age | successor | successor released | tier check |
|---|---|---|---|---|---|
| `glm-5.2` | 2026-06-13 | 105d | `glm-5.3` | 2026-08-14 | A |

Tier check: **A** = same lineage, same size/class, same role. **B** = same lineage and
size, lane role generalized (`coder/code/thinking`), equal cost tier ($0↔$0 or within 40%).

### 27 of these raise the replacement price >3x — deliberate, per "Being cheap is not
being current" (each is individually reversible by deleting its overlay row)

| lane | $ | successor | $ |
|---|---|---|---|
| `clinepass/kimi-k2.6` | 0.825 | `kimi-k3` | 3.0 |
| `commandcode/moonshotai/Kimi-K2.6` | 0.95 | `moonshotai/Kimi-K3` | 3.0 |
| `neuralwatt/glm-5.2-flex` | 0.725 | `glm-5.3-flex` | 2.975 |
| `ollama-cloud/kimi-k2.6` | 0.08195 | `kimi-k3` | 0.29801 |
| `opencode-go/glm-5.2` | 0.436364 | `glm-5.3` | 1.745455 |
| `opencode-go/kimi-k2.6` | 0.333913 | `kimi-k3` | 3.49 |
| `opencode-go/mimo-v2.5-pro` | 0.118154 | `mimo-v2.6-pro` | 0.4524 |
| `opencode-go-2/hy3-preview` | 0.1576 | `hy4-preview` | 0.9007 |
| `opencode-go-2/kimi-k2.6` | 1.072 | `kimi-k3` | 3.49 |
| `openrouter/aion-labs/aion-2.0` | 0.8 | `aion-labs/aion-3.5` | 3.0 |
| `openrouter/anthropic/claude-3-haiku` | 0.25 | `anthropic/claude-haiku-4.5` | 1.0 |
| `openrouter/moonshotai/kimi-k2` | 0.57 | `moonshotai/kimi-k3` | 3.0 |
| `openrouter/moonshotai/kimi-k2-0905` | 0.6 | `moonshotai/kimi-k3` | 3.0 |
| `openrouter/moonshotai/kimi-k2.5` | 0.45 | `moonshotai/kimi-k3` | 3.0 |
| `openrouter/moonshotai/kimi-k2.6` | 0.95 | `moonshotai/kimi-k3` | 3.0 |
| `openrouter/openai/gpt-3.5-turbo` | 0.5 | `openai/gpt-5.5` | 5.0 |
| `openrouter/openai/gpt-3.5-turbo-0613` | 1.0 | `openai/gpt-5.5` | 5.0 |
| `openrouter/openai/gpt-3.5-turbo-instruct` | 1.5 | `openai/gpt-5.5` | 5.0 |
| `openrouter/openai/gpt-5` | 1.25 | `openai/gpt-5.5` | 5.0 |
| `openrouter/openai/gpt-5-nano` | 0.05 | `openai/gpt-5.4-nano` | 0.2 |
| `openrouter/openai/gpt-5.1` | 1.25 | `openai/gpt-5.5` | 5.0 |
| `openrouter/qwen/qwen3-32b` | 0.08 | `qwen/qwen3.8-27b` | 0.42 |
| `openrouter/tencent/hy3-preview` | 0.18 | `tencent/hy4-preview` | 0.834 |
| `openrouter/z-ai/glm-4.6` | 0.43 | `z-ai/glm-5.3` | 1.4 |
| `openrouter/z-ai/glm-4.6v` | 0.3 | `z-ai/glm-5v-turbo` | 1.2 |
| `stepfun/step-3.5-flash` | 0.0051 | `step-3.7-flash` | 0.0164 |
| `xkiro/z-ai/glm-4.6v` | 0.0108 | `z-ai/glm-5v-turbo` | 0.043733 |

Also deliberately stamped: 2 `$0` free lanes whose successor is paid
(`clinepass/glm-5.2:free`, `commandcode/xiaomi/mimo-v2.5-pro`) — the free twins of those successors are not carried by the provider.

## Skipped — catalog drift (32 candidates, NEVER stamped)

These are old AND have a newer same-tier sibling, but the digest classifies them as
ID-SHAPE/TAG-SHAPE: the lane is still in the catalog under another spelling, so a
retirement stamp would delete a live lane. They are REMAP candidates.

| lane | catalog drift kind |
|---|---|
| `fireworks-ai/kimi-k2-6` | remap |
| `minimax/minimax-m2.7` | remap |
| `openrouter/anthropic/claude-fable-5:batch` | tag-shape |
| `openrouter/anthropic/claude-opus-4.1:batch` | tag-shape |
| `openrouter/anthropic/claude-opus-4.5:batch` | tag-shape |
| `openrouter/anthropic/claude-opus-4.6:batch` | tag-shape |
| `openrouter/anthropic/claude-opus-4.7:batch` | tag-shape |
| `openrouter/anthropic/claude-opus-4.8:batch` | tag-shape |
| `openrouter/anthropic/claude-sonnet-4.5:batch` | tag-shape |
| `openrouter/anthropic/claude-sonnet-4.6:batch` | tag-shape |
| `openrouter/google/gemini-2.5-flash-lite:batch` | tag-shape |
| `openrouter/google/gemini-2.5-flash:batch` | tag-shape |
| `openrouter/google/gemini-2.5-pro:batch` | tag-shape |
| `openrouter/google/gemini-3-flash-preview:batch` | tag-shape |
| `openrouter/google/gemini-3.1-flash-lite:batch` | tag-shape |
| `openrouter/google/gemini-3.5-flash:batch` | tag-shape |
| `openrouter/openai/gpt-3.5-turbo:batch` | tag-shape |
| `openrouter/openai/gpt-4-turbo:batch` | tag-shape |
| `openrouter/openai/gpt-4.1-mini:batch` | tag-shape |
| `openrouter/openai/gpt-4.1-nano:batch` | tag-shape |
| `openrouter/openai/gpt-4.1:batch` | tag-shape |
| `openrouter/openai/gpt-5-mini:batch` | tag-shape |
| `openrouter/openai/gpt-5-nano:batch` | tag-shape |
| `openrouter/openai/gpt-5-pro:batch` | tag-shape |
| `openrouter/openai/gpt-5.1:batch` | tag-shape |
| `openrouter/openai/gpt-5.2-pro:batch` | tag-shape |
| `openrouter/openai/gpt-5.2:batch` | tag-shape |
| `openrouter/openai/gpt-5.4-pro:batch` | tag-shape |
| `openrouter/openai/gpt-5.4:batch` | tag-shape |
| `openrouter/openai/gpt-5:batch` | tag-shape |
| `openrouter/openai/o3-mini:batch` | tag-shape |
| `openrouter/x-ai/grok-4.3:batch` | tag-shape |

## Retained/skipped residuals — 353 old lanes NOT stamped, by reason

| reason | lanes | meaning |
|---|---|---|
| no newer sibling in the lineage at all | 132 | the policy's explicit "no newer sibling = no stamp" |
| newer sibling in a DIFFERENT tier/size | 97 | a newer model exists but not of this tier — retiring would drop a tier |
| no generation in the id | 90 | the id carries no version (`command-r7b`, `gpt-oss-120b`) so generation order is unprovable |
| newer date but NOT a newer generation | 15 | a re-released older generation (gpt-3.5-turbo-0613 vs gpt-4-turbo) |
| newer sibling exists but is NOT an enabled lane (off in the registry) | 8 | ENABLE CANDIDATE: the successor exists but we disabled/retired it |
| same lineage, role generalized, but a DIFFERENT cost tier | 7 | BANE DECISION: coder/thinking lane whose newer sibling costs a different tier |
| sibling has a different role/modality | 4 | e.g. a vision lane whose only newer sibling is text |

### Enable candidates — a newer same-tier sibling exists but is OFF in our registry (8)

| lane | released | newer sibling now off |
|---|---|---|
| `clinepass/deepseek-v4-flash` | 2026-04-24 | `deepseek-v4.1-flash [disabled]` |
| `clinepass/kimi-k2.7-code` | 2026-06-12 | `kimi-k3:batch [disabled]` |
| `clinepass/mimo-v2.5-pro` | 2026-04-22 | `mimo-v2.6-pro [disabled]` |
| `neuralwatt/qwen3.6-35b` | 2026-04-17 | `qwen-3.8-27b [disabled]` |
| `xkiro/moonshotai/kimi-k2.6` | 2026-04-21 | `moonshotai/kimi-k2.7-code [disabled]` |
| `xkiro/qwen/qwen3-vl-plus:free` | 2025-09-23 | `qwen/qwen3.5-plus [disabled]` |
| `xkiro/qwen/qwen3.5-omni-plus:free` | 2026-03-30 | `qwen/qwen3.6-plus [disabled]` |
| `zai-glm/glm-4.6v-flash` | 2025-12-08 | `glm-4.7-flash [disabled]` |

### Decision queue — role generalized but a different cost tier (7)

| lane | released | nearest newer sibling | why held |
|---|---|---|---|
| `commandcode/moonshotai/Kimi-K2.7-Code` | 2026-06-12 | `moonshotai/Kimi-K3` | successor is a different price tier; policy says "by cost" |
| `fireworks-ai/kimi-k2-7-code` | 2026-06-12 | `kimi-k3` | successor is a different price tier; policy says "by cost" |
| `neuralwatt/kimi-k2.7-code` | 2026-06-12 | `kimi-k3` | successor is a different price tier; policy says "by cost" |
| `ollama-cloud/kimi-k2.7-code` | 2026-06-12 | `kimi-k3` | successor is a different price tier; policy says "by cost" |
| `opencode-go/kimi-k2.7-code` | 2026-06-12 | `kimi-k3` | successor is a different price tier; policy says "by cost" |
| `opencode-go-2/kimi-k2.7-code` | 2026-06-12 | `kimi-k3` | successor is a different price tier; policy says "by cost" |
| `openrouter/moonshotai/kimi-k2.7-code` | 2026-06-12 | `moonshotai/kimi-k3` | successor is a different price tier; policy says "by cost" |

### Unresolved dates — 39 lanes (NO catalog date anywhere → not stamped)

| provider | lane | catalog match |
|---|---|---|
| clinepass | `dots-3-note-preview:free` | None |
| clinepass | `laguna-s-2.1:free` | None |
| clinepass | `laguna-xs-2.1:free` | None |
| clinepass | `lfm-2.5-2.6b:free` | None |
| clinepass | `ling-3.0-flash-fin:free` | None |
| clinepass | `ling-3.0-flash-sante:free` | None |
| clinepass | `ling-3.0-flash-vl:free` | None |
| clinepass | `nex-n2.5-mini:free` | None |
| clinepass | `nex-n2.5-pro:free` | None |
| clinepass | `north-mini-code:free` | None |
| commandcode | `deepseek/deepseek-v4-flash-fast` | None |
| commandcode | `moonshotai/Kimi-K2.5` | None |
| commandcode | `stealth/pixel-canary` | None |
| commandcode | `tencent/hy3-paid` | None |
| commandcode | `zai-org/GLM-5` | None |
| fireworks-ai | `accounts/fireworks/models/mistral-large-3-fp8` | absent |
| fireworks-ai | `accounts/fireworks/routers/deepseek-pro-latest` | absent |
| openrouter | `kwaipilot/kat-coder-pro-v2` | absent |
| openrouter | `nex-agi/nex-n2.5-mini` | absent |
| openrouter | `nex-agi/nex-n2.5-mini:free` | absent |
| openrouter | `nex-agi/nex-n2.5-pro` | absent |
| openrouter | `nex-agi/nex-n2.5-pro:free` | absent |
| openrouter | `openrouter/auto-beta` | absent |
| openrouter | `typesafe/jev-router` | absent |
| xkiro | `cohere/aya-expanse-32b` | None |
| xkiro | `cohere/aya-vision-32b` | None |
| xkiro | `cohere/command-a-translate` | None |
| xkiro | `cohere/command-a-vision` | None |
| xkiro | `cohere/north-small-translate` | None |
| xkiro | `cohere/tiny-aya-earth` | None |
| xkiro | `cohere/tiny-aya-fire` | None |
| xkiro | `cohere/tiny-aya-global` | None |
| xkiro | `cohere/tiny-aya-water` | None |
| xkiro | `minimax/minimax-m2.1-highspeed:free` | None |
| xkiro | `mistralai/devstral-medium` | None |
| xkiro | `mistralai/ministral-8b` | None |
| xkiro | `moonshotai/kimi-k2.5` | None |
| xkiro | `nvidia/nemotron-3-nano-omni` | None |
| xkiro | `sensenova/sensenova-6.7-flash-lite` | None |

These are the honest unknown-date class: mostly `:free` reseller lanes and provider-side
routes. Queue for verification (a live `/models` probe can date nothing; a provider
announcement or a models.dev entry is what would make them stampable).

## Proof

### Digest before

```
LIFECYCLE DIGEST — 2026-09-26
counts: live 1584 | coming_soon 0 | retiring 0 | retired 25
```

### Digest after (`scripts/router_lifecycle.py --all`)

```
LIFECYCLE DIGEST — 2026-09-26
counts: live 1383 | coming_soon 0 | retiring 0 | retired 226

RETIRED (hidden from chains, counted here) — last 20 of the block:
  xkiro/qwen/qwen3-max:free  retired 2026-09-26  -> xkiro/qwen/qwen3.8-max:free
  xkiro/qwen/qwen3-omni-flash:free  retired 2026-09-26  -> xkiro/qwen/qwen3.8-omni-flash:free
  xkiro/qwen/qwen3.5-flash:free  retired 2026-09-26  -> xkiro/qwen/qwen3.7-flash:free
  xkiro/qwen/qwen3.5-omni-flash:free  retired 2026-09-26  -> xkiro/qwen/qwen3.8-omni-flash:free
  xkiro/qwen/qwen3.5-plus:free  retired 2026-09-26  -> xkiro/qwen/qwen3.7-plus:free
  xkiro/qwen/qwen3.6-max-preview:free  retired 2026-09-26  -> xkiro/qwen/qwen3.8-max:free
  xkiro/qwen/qwen3.6-plus:free  retired 2026-09-26  -> xkiro/qwen/qwen3.7-plus:free
  xkiro/qwen/qwen3.7-max:free  retired 2026-09-26  -> xkiro/qwen/qwen3.8-max:free
  xkiro/z-ai/glm-4.5  retired 2026-09-26  -> xkiro/z-ai/glm-5.3
  xkiro/z-ai/glm-4.5-flash  retired 2026-09-26  -> xkiro/z-ai/glm-5.3-flash
  xkiro/z-ai/glm-4.5v  retired 2026-09-26  -> xkiro/z-ai/glm-5v-turbo
  xkiro/z-ai/glm-4.6  retired 2026-09-26  -> xkiro/z-ai/glm-5.3
```

Per-lane verification: **200/200** stamped lanes report `retired` with
`valid_to = 2026-09-26` and a populated `replaced_by` + `lifecycle_source` in the
rebuilt `registry.json`. The retired SET grew by exactly 200 pairs
(24 → 224 distinct); the digest row counts move 25 → 226 (the digest counts rows, two
of which are duplicate spellings, unchanged by this pass).

### Resolver proof — retired lanes are hidden from CHAINS, still counted

| profile | chain length | retired lanes in chain |
|---|---|---|
retired lanes: 224 | live: 1383 | total rows: 1609
| muster | 17 | 0 |
| uhlp | 17 | 0 |
| hermes-dagger | 17 | 0 |
| coding-hermes-scheduler | 68 | 0 |
| duckbrain-sync | 27 | 0 |
| temple-runner | 17 | 0 |
| crier | 17 | 0 |

### Seed / export

```
loaded models       1609 rows
applied 219 lifecycle overlays from /home/kara/task-router/data/lifecycle.jsonl
exported tables to /home/kara/.local/share/task-router/scratch/ns/routing
```

`registry.json` (the file both the resolver and the digest read, gitignored, rebuilt)
was regenerated by `scripts/router_seed.py`. The DuckBrain fleet mirror
(`~/duckbrain/namespaces/routing`) was deliberately NOT written: a bare run exports to
the data-home scratch ns by design (TR-048), and the fleet mirror is the daily maintain
cron's job — writing it by hand would leave the mirror dirty for sibling ticks. The
overlay is committed, so the next maintain run reproduces this state.

### HEAD-DRIFT GUARD (mandatory after an evidence slice)

`router_spawn.py <profile> --format json` before and after, `diff` of the head lines:

```
--- before ---
muster                     xkiro          mistralai/mistral-large-2512 usd_1m=0.0
uhlp                       xkiro          mistralai/mistral-large-2512 usd_1m=0.0
hermes-dagger              xkiro          mistralai/mistral-large-2512 usd_1m=0.0
coding-hermes-scheduler    xkiro          minimax/minimax-m3:free      usd_1m=0.0
duckbrain-sync             xkiro          mistralai/mistral-large-2512 usd_1m=0.0
--- after ---
muster                     xkiro          mistralai/mistral-large-2512 usd_1m=0.0
uhlp                       xkiro          mistralai/mistral-large-2512 usd_1m=0.0
hermes-dagger              xkiro          mistralai/mistral-large-2512 usd_1m=0.0
coding-hermes-scheduler    xkiro          minimax/minimax-m3:free      usd_1m=0.0
duckbrain-sync             xkiro          mistralai/mistral-large-2512 usd_1m=0.0
--- diff ---
IDENTICAL — no head moved
```

No head moved, and that is explainable rather than lucky: every head lane
(`xkiro/mistralai/mistral-large-2512`, `xkiro/minimax/minimax-m3:free`,
`xkiro/z-ai/glm-5.3-flash`) falls in the RETAINED bucket (newest of its lineage). The
3 stamped lanes that point AT `xkiro/z-ai/glm-5.3-flash` only add evidence to that lane.

#### The guard as specified MISSED a real move — the repo's own golden profiles

The five-profile list above does not include the profiles the repo pins in
`tests/test_regression.py` (P0_FORE / P1_CODING / P2_AGENTIC / P4_SECURITY), and the
suite caught a head move there that the five-profile diff reported as clean:
`test_golden_fixed_point_heads` went red on P0_FORE and P2_AGENTIC. Extension of the
guard to the golden profiles, run against three datasets so the CAUSE is not a guess
(`A` = HEAD data + HEAD lifecycle, `B` = worktree data with this pass's stamps stripped,
`C` = worktree data + stamps), hermetic prod-mirror state as the test builds it:

| dataset | P0_FORE | P2_AGENTIC |
|---|---|---|
| A — before this pass (HEAD) | `zai-glm/glm-5.3-flash` $0.082 | `stepfun/step-3.5-flash` $0.108 |
| B — worktree data, lifecycle stamps STRIPPED | `xkiro/cohere/command-a-plus` $0.0 | `xkiro/cohere/command-a-plus` $0.0 |
| C — worktree data + this pass's stamps | `xkiro/cohere/command-a-plus` $0.0 | `xkiro/cohere/command-a-plus` $0.0 |

B and C are identical, so **the lifecycle pass did not move either head**. What moved
them is a `xkiro/cohere/command-a-plus` lane (normalized $0.0, 436k ctx, no retirement
date) that a 2026-09-26 models.dev sync added to the same working tree — it takes the
price tie-break in the hermetic mirror. One of the old heads IS this pass's business:
`stepfun/step-3.5-flash` was retired (Bane's own named case), which is why P2_AGENTIC
could not keep it. Both fixtures were updated deliberately with that reason in the test
comment (the repo's rule: the golden heads are updated on purpose or not at all).

### Test suite

```
........................................................................ [ 82%]
........................................................................ [ 89%]
........................................................................ [ 95%]
...........................................                              [100%]
1050 passed, 1 skipped in 255.69s (0:04:15)
PYTEST EXIT=0
```

## What was found on the way (not part of the ask, worth a row)

1. **THIN OVERLAY ROWS WIPE A LANE — measured, not theorised.** `router_seed.py` applies
   the lifecycle overlay as `UPDATE models SET <every column except provider/model>`
   with `_o.get(c)`; a row carrying only a few keys therefore NULLs price, `perf_*`,
   `context_limit`, `disabled` and every other column of that lane in the derived
   registry. Proven in a scratch copy: a 6-key overlay row zeroed 20 columns of
   `stepfun/step-3.5-flash`. The one thin row currently in `data/lifecycle.jsonl`
   (`ollama-cloud/deepseek-v4-flash:0731`) targets a lane that is NULL-valued anyway, so
   no live data was lost — but every row in this pass is a FULL row copy. The seed should
   either merge key-wise or refuse a row missing required columns.
2. **The policy as written cannot bring the list inside one quarter by itself.** Age alone is explicitly not a reason to drop a model, so out of 585 lanes older than 90 days only the 232 with a newer same-tier sibling were eligible. A lineage-only rule (ignoring tier/role) would additionally retire 131 lanes — the 97 tier/size + 7 role/cost + 15 generation-only + 8 successor-off lanes listed above. Flagging those as a decision rather than silently widening the rule.
3. **2 lanes already carried a metadata-only overlay row** (`neuralwatt/qwen3.6-35b-flex`,
   `opencode-go/hy3-preview`); the new retirement row is appended after it and wins,
   replacing the 2026-09-21 live-verification provenance with the 2026-09-26 retirement
   (the earlier provenance stays in the file history).
4. **A second agent session was writing the same working tree.** The tree already carried
   an uncommitted models.dev sync (46 added lanes — including the $0
   `xkiro/cohere/command-a-plus` that moved the two golden heads — plus ~55 price/
   cache-rate refreshes), a `router_provider_import.py` wording fix that its own test
   (`tests/test_pricing_audit_classes.py`) requires, and two appended board rows
   (TR-164/TR-175, the latter reporting that this repo's guard was blocking commits). The
   commit that carries this pass therefore also carries that in-flight work: the guard
   grades the WHOLE tree and git stages whole files, so the data, the fixtures it drives
   and the script wording its test asserts either land together or not at all. Nothing
   of it was modified by this pass; the isolation table above separates the two changes.

## Follow-ups (filed on the board, not done here)

* Same-class repair for the 8 enable-candidates and the 7 cost-tier decisions.
* The thin-overlay-row hazard (mechanism fix in `router_seed.py`).
* Repeat this pass on a schedule — the registry has no retirement clock of its own, and
   this pass is a snapshot of 2026-09-26 dates.

---
## ADDENDUM — after TR-178 landed (same day, same tree)

The main report was written and committed (`e3912e7`) while a second session was mid-flight
in the same working tree. That session then landed two commits of its own:

* `c49b18a` — "reach the degraded path when nothing is eligible + no longer fabricate $0 on
  plan SKUs", which also pinned `tests/test_health_identity.py` (that test asserted live
  HEAD, so ANY concurrent commit failed this repo's guard — the reason the guard had been
  blocking every commit here, see TR-175).
* `ab0d41f` (TR-178) — "regenerate with the fixed rule — fabricated $0 on non-free plan SKUs
  cleared" (31 price lines in `data/tables/models.jsonl`).

Three consequences, checked rather than assumed:

1. **The retirements survived the regeneration.** TR-178 rewrote `models.jsonl` (which also
   carried this pass's applied stamps after the seed's table mirror); a re-seed then
   re-applied all 219 overlay rows and nothing moved: **200/200 stamped lanes still report
   `retired`, digest still `live 1383 / retiring 0 / retired 226`**. That is this pass's
   durability claim demonstrated against a real concurrent rewrite instead of in theory.
2. **The P0_FORE / P2_AGENTIC heads moved a second time and the fixtures were re-updated.**
   The `$0.0` price on `xkiro/cohere/command-a-plus` was itself the TR-178 defect; with it
   cleared that lane is unpriced, and the settled head is **`xkiro/openai/gpt-6-luna` at
   $0.003867/M** (`price_evidence: provider_import preset=xkiro $200 coding plan`) for both
   profiles. The isolation table earlier in this report therefore describes the INTERMEDIATE
   state; the fixture comment in `tests/test_regression.py` carries the final sequence. Two
   heads moved and neither moved because of a lifecycle stamp: P2_AGENTIC could not keep
   `stepfun/step-3.5-flash` (retired here, Bane's own named case) and both profiles re-sorted
   on the pricing fix.
3. **CI is red independently of this pass, and is filed as TR-177.**
   `tests/test_envelope_step_count.py::test_success_envelope_reports_the_step_count` fails in
   CI (`steps == 0`: the proxy resolves an empty ladder there) while the same tree passes
   1050 tests locally. Proof it is not this pass: the docs-only commit `7992b1f`
   (run 36221756438) fails the identical step, as do `86f1452` and this pass's `e3912e7`
   (run 36223733944) — steps>0, so a REAL failure, not the billing-block class.

---

*Generated by the 2026-09-26 lifecycle pass; every number in this report comes from the
artifacts it wrote (`data/lifecycle.jsonl`, `registry.json`, the digest, the head
snapshots). Scanner used for the analysis: `/tmp/prune_final.py` (kept in /tmp, not
committed — promoting it to `scripts/` is a code change and needs its own suite run).*
