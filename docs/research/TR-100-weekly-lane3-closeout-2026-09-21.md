# TR-100 closeout — weekly research lane 3/3 (rankings + misc), week of 2026-09-21

Verification-only lane. **Zero data changes** — no seed input was edited; this document IS the
deliverable of record (worker report verified by the foreman before landing). Worktree
wt/TR-100 finished clean at base ff0d772 with nothing to commit, by design.

## 1. Chain-head sanity vs the 2026-09-20T14:0xZ baseline

Spawn runs re-executed 2026-09-21 (data/tables + live gates in ~/.hermes/model-router):

| project | baseline (09-20) | now (09-21) | verdict |
|---|---|---|---|
| coding-hermes-scheduler | 44 hops, head hop 3 = stepfun/step-3.5-flash $0.108 (hops 1,2,4 excluded: xkiro m3:free DOWN(?), xkiro gpt-5.6-luna circuit OPEN til 14:23:42Z, xkiro glm-5.3-flash DOWN(?)) | 51 hops, head hop 1 = xkiro minimax/minimax-m3:free $0.00; stepfun still exactly hop 3 | The baseline head exclusion CLEARED (m3:free probe now OK, 1535 ms). Stepfun lost the top slot only because an upstream exclusion resolved — price order unchanged. All 09-20 circuit entries (xkiro luna etc.) expired; circuit-state.json now holds NO entries for xkiro/stepfun/kimi. |
| hivemind-work | 8 hops, head hop 1 = stepfun/step-3.5-flash $0.108, hop 5 zai-glm/glm-5.3-flash $0.082, kimi-for-coding DOWN | 9 hops, head UNCHANGED (stepfun $0.108 hop 1), zai-glm still hop 5 | kimi-for-coding still DOWN but reason changed: health probe now HTTP 403 (auth/permission), not generic DOWN. |

Price-correctness proof: every emitted chain entry recomputed against the script's own
`_legacy_sort_key` (PAYG-last bucket + plan_tier + effective price): **0 sort inversions in
both chains**. The stepfun head is current, not stale: plan_terms row "Step Plan Flash Pro
$29/mo = 8,000M credits/mo", usage_multiplier 39.4, `normalized:flat-sub(39.4x lane)`,
official dossier re-verified 2026-09-16 — a genuine tier-0 flat-sub lane; census of eligible
lanes finds 0 sorting ahead of it.

## 2. The xkiro "DOWN (?)" open question — VERDICT: health-probe artifact, not an unlogged exclusion

The string is generated at scripts/router_spawn.py:1746 as `model DOWN ({hm.get("ts", "?")})`
— "(?)" literally means the model-level health entry has `status: DOWN` with a MISSING `ts`
field. health-state.json confirms: xkiro/z-ai/glm-5.3-flash DOWN with `error: "HTTP 429"`, no
`ts`. It is a real rate-limit, not an outage: health.jsonl shows the lane flapping OK↔DOWN all
week (last DOWN transition 2026-09-20T01:00Z; OK as recently as 09-19T10:00Z); xkiro as a
provider is OK (9/11 models up; 483 ms on the failing lane).

Missing-ts is fleet-wide: 56 model-level entries across clinepass, opencode-go(-2),
kimi-for-coding, neuralwatt, openrouter, commandcode(-2), xkiro share the no-ts shape — only
probe v3's model-level entries lack it (provider-level entries all carry ts).

**Follow-ups filed for a future lane** (not done here): (a) probe v3 should stamp `ts` on
model-level entries so DOWN(?) becomes a real timestamp; (b) the baseline's minimax DOWN(?)
had already recovered by baseline time per the same flapping pattern.

## 3. or-spot evidence >30 days: NONE due this week

Census over models.jsonl: 0 rows over the 30-day line. The 3 rows named in the row
(opencode-go-2 deepseek-v4-flash / glm-5.2 / qwen3.8-max, or-spot-2026-08-27) are age 25d on
2026-09-21; they cross the line 2026-09-26 — reprice fold-in lands NEXT week if untouched.
(Spot-check not run, models.jsonl not touched, per wave fences.)

## 4. temporary_discounts.jsonl expiry verdict: nothing expired from lane 3's view

All 22 rows `valid_to: null` at read time; no expiry since TR-037 dropped 3 rows 2026-09-11.
(Lane 2 concurrently stamped ONE row expired with live evidence — clinepass
deepseek-v4-flash-0731:free, valid_to 2026-09-20 — landing via TR-099's commit. That is
lane 2's file.)

## 5. fallback_lanes.jsonl verification: PASS, no fix needed

All 7 rows (orders 1–7, grown 3→7 on 2026-09-19) verified on three axes (foreman re-verified
the spot-checks independently):
- key_env: all 5 distinct env vars (XKIRO_API_KEY ×2, OPENCODE_GO_API_KEY,
  COMMANDCODE_API_KEY, DEEPSEEK_FOREMAN_API_KEY ×2, DEEPSEEK_DUCKBRAIN_SYNC_API_KEY) resolve
  SET in ~/.hermes/.env.
- Registry pairs: all 7 (provider, model) pairs have live, non-disabled registry rows.
- Order honors plan-lanes-ahead-of-PAYG: xkiro plan lanes (1–2) → opencode-go/commandcode
  plan-pool lanes (3–4) → PAYG deepseek lanes (5–7; vision-exp and duckbrain-sync correctly
  tagged P5_VISION_E2E / P8_SYNC).
- Liveness: commandcode deepseek-v4-flash probe OK (3165 ms); opencode-go glm-5.3-flash DOWN
  (HTTP 400) and xkiro glm-5.3-flash DOWN (429) — those two head lanes flap; that is the
  health gate's job, not a file defect. Order intact.

## 6. Quality-estimate staleness (explicit, per row): NO new quality-estimate source in three weeks

quality_estimates.jsonl unchanged at 226 static priors (last commit 9bbc908, 2026-09-16); only
dated notes remain hy3 (2026-08-29) + the TR-039 neutral fill (2026-09-15). Perf freshness
lives in benchmarks.jsonl instead: 648 rows, freshest dated evidence 2026-09-19 (new since the
baseline: 2 rows 09-17 — glm-5.3-flash + gpt-5.6-sol sentiment; 5 rows 09-19 — glm-5.3-flashx
code_gen/sentiment + qwen3.8-max code_gen/sentiment/terminal, the latter two explicitly
vendor-scale/INERT by design). None moves model_tier: verified on a scratch seed run —
qwen3.8-max keeps its PROFILE_TAGS-derived code_gen tier 4; glm-5.3-flashx (clinepass/zai-glm,
null price, no perf overlay) derives no measured tier. TR-054 law holds: no binary pass/fail
battery scores entered perf_*.

## 7. Integrity + tests

Scratch-copy router_seed.py round-trip byte-identical (diff -rq exit 0 across data/tables).
Affected tests pytest -q tests/test_regression.py tests/test_chain_run.py = 39 passed (board
venv). No ns export, no s3daily, no spot-check, no board writes from the worker.

no-change verified 2026-09-21 — sources: data/tables/{model_perf,benchmarks,
quality_estimates,model_tier,models,fallback_lanes,temporary_discounts,plan_terms}.jsonl (git
last-touch 9bbc908 09-16 / 9eba6a6 09-19 / 985f728 09-19), live router_spawn.py runs ×2,
health-state.json + health.jsonl + circuit-state.json (probes through 2026-09-21T09:00Z),
scratch router_seed.py round-trip, 39/39 affected tests.

Worker: glm-5.3-flash@zai-glm, session 20260921_041818_d7e657, worktree wt/TR-100 (zero
commits — verification-only). Foreman spot-verified: fallback key_envs SET, or-spot-08-27
rows = 3, discounts valid_to-null count = 22 (pre-stamp), before landing this closeout.
