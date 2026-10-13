# model-registry-data-quality run — 2026-10-13

## What changed in the registry this run
- 5 evidence rows appended (1 benchmarks/sentiment, 2 model_notes, 1 probe_fixes,
  2 provider_rules). Plus the pre-run script's own mutations: +1 lane
  (clinepass/muse-wonder, plan-sweep-disabled), 3 capability field refreshes,
  58-row providers.jsonl sync restamp.
- Research rotation pick: **gpt-5.6-luna** (only tracked model with no October
  row; also the fleet's volume default at the time of the run).
- HEADS BEFORE/AFTER: flipped on 4 of 5 profiles (gpt-6-luna 0.116 ->
  qwen/qwen3.8-max:free $0). CAUSE ISOLATED: NOT the slice — zero tier/price
  rows changed, category_levels diff EMPTY. The 10:00:41Z pre-run probe wave
  stamped 3 xkiro :free lanes model-DOWN; the price-ordered chain handed the
  head to the surviving $0 lane. Structural argument + spawn exclusions
  (DOWN timestamps predate the run's snapshot window) per the HEAD-FLIP law.

## Facts learned (persisted)
- xkiro :free lanes share ONE daily free-token pool per ACCOUNT (both seats
  429 "reached today's free-model token quota", id VALID, resets UTC
  midnight). provider_rules xkiro/free-daily-token-pool + explains_probe so
  the scanner classifies it.
- New head does NOT currently serve (quota-blocked fleet-wide today); chain
  falls to xkiro openai/gpt-6-luna 0.116 (that lane carries live traffic).
- gpt-5.6-luna long-context cliff (vellum 41.3 vs terra 89.6 / sol 91.5)
  disclosed notes-only (conflicts with the 09-27 live-probe 1.0 row;
  apply-overlay is upward-only, note is the audit trail). Sentiment
  0.84 -> 0.80 on the GPT-6 Sol/Luna launch (2026-10-12).
- lyria (audio-out) lanes: chat-probe 400 INVALID_ARGUMENT = surface gate
  class, rule row added (explains_probe lyria), NOT lane death.
- clinepass/muse-wonder: catalog-listed, ALL THREE chat id forms 404
  (300-token probes); plan sweep already disabled it pre-run (not in
  included_models, no discount). Probe evidence recorded; no price (never
  guess).

## Gates this run
- data_null_census --fail-on-unexplained: rc=0 PASS.
- Full suite: launched in background (box load 40-63, wave conditions);
  result checked in-session before commit. NOTE: lastfailed cache showed 44
  entries from 04:31 (pre-slice) — treated as stale cache from an aborted
  earlier run, not a verdict.

## Next gaps (top 3)
1. xkiro free-lane pool: re-probe after 00:00 UTC to confirm reset semantics
   and re-baseline the hourly probe's DOWN stamps for :free lanes.
2. openrouter credential-expired class (TR-223 landed acceptance, 274 lanes
   named) — OR spot-check repricing still blocked by expired keys.
3. muse-code/meta-model catalog provenance still absent (models.jsonl rows
   carry no catalog backing; needs a meta-model source decision).
