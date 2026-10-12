# TR-222 — Onboarding gaps + config lane drift: closeout (2026-10-12)

Worker: coding-hermes worktree `wt/TR-222` @ `/home/kara/worktrees/task-router-TR-222`.
Board row: TR-222 (P1, registry/ranking/config/data-quality). Source finding:
the 2026-09-27 vendor roster sweep (`data/rosters/all-vendors-2026-09-27.json`
+ `~/model_bench/roster_backfill_report.json`).

Method note (what changed vs the row's premise): the roster's vendor-model
names are NOT carrier ids. Onboarding went lane-by-lane through live evidence
instead of id-matching, which moved most of the row out of "onboard" and into
"verified-present / not-actionable / dead-id-replaced". Every verdict below
carries its probe.

## 1. Per-model outcome table

| roster model | verdict | registry lane (row id = provider/model) | price ($/M norm) | tier coverage | evidence |
|---|---|---|---|---|---|
| meituan/LongCat-2.5-Preview | VERIFIED-PRESENT + lane family completed | `opencode-go/longcat-2.5-preview-free` + `opencode-go-2/longcat-2.5-preview-free` (imported 2026-09-27; the premise check missed them by spelling: `longcat-2.5-preview-free` vs `LongCat-2.5-Preview`) | 0.0 (window-cost-pending, TR-179 two-wallet rule) | 0 rows (was: the actual gap) | live catalogs: xkiro `meituan/longcat-2.5-preview:free` + opencode-go-2 `longcat-2.5-preview-free` (2026-10-12 keyless listings) |
| meituan/LongCat-2.5-Preview (xkiro SKU) | ONBOARDED this tick | `xkiro/meituan/longcat-2.5-preview:free` | 0.0 + dated window-cost-pending reason | inherits `longcat-2.0` family prior via the NEW alias chain — 18 tier rows land on the :free lane itself (measured post-seed) | xkiro live catalog + plan_terms $200/30x free-lane rule |
| MiniMax-M3.1-Flash-Preview | NOT-ACTIONABLE (no carrier) | none — reason recorded here + below | — | — | native minimax API listing 2026-10-12 = M3/M2.x only (no M3.1); commandcode 88-id listing: no M3.1; xkiro 129-id listing: no M3.1; OR 459-id listing: no M3.1. No key on this deployment reaches the model |
| kimi-k2.8-preview | DEAD-ID → replaced by its live serving form | `xkiro/moonshotai/kimi-k2.8-preview-agent` (NEW row) ; old `moonshotai/kimi-k2.8-preview` rows left (price stays for the fold-in) + 2 typed `not_served` gap rows | 0.0833 ($200/30 plan math on the $1/$4 catalog sticker) | inherits k2 family tags via PROFILE_MODELS entry (seed-derived; 0 rows post-seed) | direct chat probe on the bare id: HTTP 404 `"Model \"moonshotai/kimi-k2.8-preview\" does not exist."` (2026-10-12, XKIRO key); `-agent` id: HTTP 200 reasoning answer; xkiro catalog lists ONLY `-agent` |
| qwen3.8-flash-next | NOT-ACTIONABLE (no carrier) | none — reason recorded | — | — | absent from all 5 live catalog pulls (cc 88, xk 129, og2 38, OR 459, cp 459); open-weights HF release, no serving lane on any keyed provider here |
| qwen3.7-max-2026-06-08 | VERIFIED-PRESENT (same weights) | the undated slug IS the 2026-06-08 release: 8 live lanes incl. `opencode-go/qwen3.7-max` ($2.259, plan rate table) + `clinepass/qwen3.7-max` ($1.667) | 1.475–2.7 | seed-derived via qwen3.7-max profile (GPQA 0.924 / TB 0.697 bench) | vendor changelog in the roster itself: `qwen3.7-max` released 2026-05-19, `qwen3.7-max-2026-06-08` = its 06-08 refresh, same API slug; the dated id appears in NO catalog (it is a changelog name, not a serving id) |
| qwen3.5-plus-2026-04-20 | VERIFIED-PRESENT + refresh | `openrouter/qwen/qwen3.5-plus-20260420` live; the stale vt=2026-09-26 clinepass row needs no new row (vt= is a lifecycle stamp, re-auditable) | 0.3 | alias `qwen3.5-plus-02-15` family | OR live catalog lists the exact id (2026-10-12) |
| Qwen-AgentWorld-35B-A3B | NOT-ACTIONABLE (no carrier) | none — reason recorded | — | — | absent from all 5 live catalog pulls; research/agent-world weights with no serving lane on any keyed provider here |
| gpt-5.6-cyber | NOT-ACTIONABLE (no carrier) | none — reason recorded | — | — | absent from all 5 live catalog pulls; no openai-codex entitlement reachable from this deployment serves it |
| gpt-5.5-instant | NOT-ACTIONABLE (no carrier) | none — reason recorded | — | — | absent from all 5 live catalog pulls (gpt-5.5 IS served and carried; `-instant` is not, on any keyed lane) |
| claude-mythos-5 / claude-mythos-5-1 | NOT-ACTIONABLE (no carrier) | none — reason recorded | — | — | OR catalog (459 ids) carries NO mythos id at all (fable-5/5-1 + opus/sonnet/haiku only); MYTHOS_OPENROUTER_KEY exists in ~/.hermes/.env but its lane is the local chimera panel, not an OR catalog id — no catalog id to onboard |
| command-a-plus-05-2026 | NOT-ACTIONABLE (no credentials — row-listed) | none | — | — | roster row: Cohere; no Cohere key on this deployment |
| north-mini-code-1-0 | NOT-ACTIONABLE (no credentials — row-listed) | none | — | — | roster row: Cohere/Snowflake-class; no key on this deployment |
| Spark-X2.5 | NOT-ACTIONABLE (no credentials — row-listed) | none | — | — | roster row; no key on this deployment |
| ERNIE 5.1 | NOT-ACTIONABLE (no credentials — row-listed) | none | — | — | roster row: Baidu; no key on this deployment |
| xiaomi/mimo-v2.6-pro-ultra (phantom) | DOES NOT EXIST — confirmed, nothing to fix | — | — | — | registry already carries 15 `xiaomi/mimo-v2.6-*` rows incl. `mimo-v2.6-pro-ultraspeed` on commandcode/-2/openrouter/xkiro/-2; grep proves `mimo-v2.6-pro-ultra` appears ONLY inside the roster's own gaps note, never in config.yaml nor in any registry row. The config's xiaomi entries were `xiaomi/mimo-v2.5` + `xiaomi/mimo-v2.5-pro` — the real ultraspeed id is now ADDED to the chimera map (see §2) |
| google/gemini-3-pro-preview (config map id, found during the AC-6 census) | config-drift companion: phantom map id; real successor lane onboarded | `openrouter/google/gemini-3-pro-image` (row existed already at $2.0 — census shows the map id resolves to it as the family's live lane; the preview slug itself never shipped) | 2.0 | existing | OR live catalog: no `gemini-3-pro-preview`, but `gemini-3-pro-image` + `gemini-3.1-pro-preview` live |

## 2. Config drift — before/after (lines quoted from ~/.hermes/config.yaml)

Backup: `~/.hermes/config.yaml.bak-TR-222-2026-10-12` (byte copy, taken before
the edit). Edit was SURGICAL (line-level; a yaml.safe_dump rewrite was tried
first and rejected: it re-indents 903 diff-lines and drops all 21 comment
lines). Final diff = 4 insertions + 1 line replacement, comments intact.

```diff
--- config.yaml (before)
+++ config.yaml (after)
@@ chimera custom_providers models map @@
       openrouter/x-ai/grok-4.5: {{}}
+      openrouter/x-ai/grok-4.6: {{}}
+      openrouter/x-ai/grok-4.7: {{}}
       zai-coding-plan/glm-5.2: {{}}
...
       xiaomi/mimo-v2.5-pro: {{}}
+      xiaomi/mimo-v2.6-pro-ultraspeed: {{}}
       qwen/qwen3-coder-next: {{}}
...
       router9/openrouter/x-ai/grok-4.6: {{}}
-      router9/xai/grok-4: {{}}
+      router9/openrouter/x-ai/grok-4.7: {{}}
       cliproxy/deepseek,deepseek-v4-flash: {{}}
```

- `grok-4.7` in the xai provider list was ALREADY present (line 121 —
  grok-4.5, 4.3, 4.20-0309-*, 4.6, multi-agent, build-0.1, **grok-4.7**);
  premise check confirmed. Verified selectable: `xai/grok-4.7` is a live
  registry lane (commandcode/xkiro/opencode-go spellings) and the native
  `x-ai/grok-4.7` id answered **HTTP 200 "pong"** on XAI_API_KEY (2026-10-12).
- `router9/xai/grok-4` (retired slug, silent redirect → bills as grok-4.3):
  REMOVED. Replaced by the equivalent live route `router9/openrouter/x-ai/grok-4.7`.
- OpenRouter map coverage: `x-ai/grok-4.6` AND `x-ai/grok-4.7` now declared
  (both ids verified in the live OR catalog pull, 459 ids, 2026-10-12).
- xiaomi: `mimo-v2.6-pro-ultra` appears NOWHERE in the file (grep-verified);
  `xiaomi/mimo-v2.6-pro-ultraspeed` added next to the v2.5 entries.
- `cliproxy/deepseek,deepseek-v4-flash` (the last unresolved chimera-map id in
  the AC-6 census): the map entry predates this tick and its id form
  (`provider,model` composite) is a cliproxy-carrier addressing shape — the
  underlying deepseek-v4-flash id resolves in the registry on 5+ lanes; left
  as-is (removing a live map entry used by the panel is outside this row).

## 3. DeepSeek flag state (registry rows vs upstream reality)

Upstream truth (roster gap note + live probes 2026-10-12):
- `deepseek-v4-flash` + `deepseek-v4-flash-vision-exp`: RETIRED 2026-09-10
  (news260910); ids still resolve → redirect to V4.1-Flash.
- `deepseek-v4-pro`: NOT retired (2026-09-14 redirect announced then REVERSED).
  Live-verified 2026-10-12: native api.deepseek.com chat **HTTP 200** on
  deepseek-v4-pro; ollama-cloud listing carries `deepseek-v4-pro:0813`.

| lane | before | after |
|---|---|---|
| `deepseek/deepseek-v4-flash` | vt=2026-09-10 retired-verified | vt=2026-09-10 + re-verification note (redirect semantics) |
| `deepseek/deepseek-v4-flash-vision-exp` | vt=2026-09-10 retired-verified | vt=2026-09-10 + re-verification note |
| `commandcode{{,-2}}/deepseek/deepseek-v4-flash` | vt=2026-09-26 (replaced_by v4.1-flash) | **vt=2026-09-10** (true retirement date; replaced_by kept) |
| `neuralwatt/deepseek-v4-flash{{,-flex}}` | vt=2026-09-26 | **vt=2026-09-10** |
| `opencode-go{{,-2}}/deepseek-v4-flash` | vt=2026-09-27 (rank-supersession) | **vt=2026-09-10** |
| `opencode-go{{,-2}}/deepseek-v4-flash-vision-exp` | vt=2026-09-27 | **vt=2026-09-10** (replaced_by deepseek-flash kept) |
| `deepseek-foreman/deepseek-v4-flash-vision-exp` | vt=2026-09-27 | **vt=2026-09-10** (its `fallback_lanes.jsonl` P5_VISION_E2E entry now resolves 0 lanes — the resolver's `row_is_retired` check skips the retired id; the vision-exp fallback slot is empty until a live vision lane replaces it) |
| `deepseek-foreman/deepseek-v4-pro` | live, no retirement | live, no retirement (correct; untouched) |
| `deepseek/deepseek-v4-pro` | live | live + v4-pro-NOT-retired verification note |
| `neuralwatt/deepseek-v4-pro` | vt=2026-09-10 "retired-verified" | **vt KEPT** (lane still delisted: live 404 "Model 'deepseek-v4-pro' not found" on neuralwatt 2026-10-12) + explicit note that this is a LANE delisting, not an upstream retirement |
| `ollama-cloud/deepseek-v4-pro` | disabled (billing) | stays disabled + note: billing, not retirement; `:0813` re-verified in the live listing |
| `clinepass`, `crof`, `xkiro{{,-2}}` v4-flash/v4-pro rows | disabled / PAYG-outside-plan | untouched (provider-side flags, not upstream retirement claims) |

## 4. Tier coverage (rule 5)

Mechanism, per the seed: PERF rows (per MODEL) → category_levels ladder
(regenerated) → model_tier. The two new lanes inherit family evidence through
`model_aliases.jsonl` (sanctioned variant→base inheritance; **not** new
quantile entries, so no TR-044 pool shift):
- `moonshotai/kimi-k2.8-preview-agent` → `moonshotai/kimi-k2.8-preview` →
  k2 family tags via the `PROFILE_MODELS['kimi k2']` xkiro pair (seed-derived;
  0 tier rows post-seed).
- `meituan/longcat-2.5-preview:free` → `longcat-2.5-preview` → `longcat-2.0`
  (18 tier rows on the base). Deliberately NOT added to
  PROFILE_MODELS (no survey exists for 2.5; inheriting the base through the
  alias keeps the quantile pools and golden heads untouched).

After the seed, `ROUTER-MISS` census (same command as rule 5, with
ROUTER_MISS_VERBOSE=1 — the env flag the spawn telemetry requires):

```
before: 2095 MISS lines (pre-change tree)  ->  after: 2100 (decomposition in §6)
````

## 5. Verification

- pytest (board venv): **2150 passed, 1 skipped** in 700.73s (full suite,
  includes the TR-168 sequence pin re-pinned for the 3 new lanes)
- router_rank_audit.py: run on the seeded tree — see §6 for the UNRANKED
  delta on the touched providers (the tool has no config section; the
  config-declared-id census was run separately, see §2/§7: 292 ids checked,
  the only map ids without a registry lane are pre-existing non-routed SKUs
  (groq whisper/prompt-guard, nvidia code LLMs, stepfun audio/image, synthetic
  syn:* config-lane ids — each documented in-repo as not-routed) plus the
  phantom gemini preview slug covered in §1).
- data_null_census.py: `live_unexplained_count: 0` with the new rows in
  (both new lanes' zero-prices carry dated window-cost-pending stories;
  every other new NULL is structural).

## 6. MISS census (ROUTER-MISS lines, verbose telemetry)

`ROUTER_MISS_VERBOSE=1 router_spawn.py coding-hermes-scheduler --format json`,
stderr line census — **before 2095 → after 2100** (/tmp/miss-before.txt vs
/tmp/miss-final.txt). Per touched provider (before → after): commandcode
95→95, commandcode-2 93→93, opencode-go 47→47, opencode-go-2 35→35, xkiro
83→87, xkiro-2 83→85.

The +5 decomposes exactly:
- **+6 lines = the NEW lanes themselves** (`xkiro/-2 moonshotai/
  kimi-k2.8-preview-agent` +4: debug tier=None, test tier=-1; `xkiro
  meituan/longcat-2.5-preview:free` +2: refactor tier=None, test tier=-1).
  A lane that did not exist cannot MISS; these are new live lanes now being
  honestly telemetered on the two categories their families have no
  estimate/bench for. Their other categories TIER and ROUTE (k2.8-agent:
  code_gen=0 refactor=+2 tool_use=0 multilingual; longcat: 16 of 18
  categories, agent_tick/delegation=+5).
- **−1 line = stepfun/step-3.7-flash** (test>=0): the test L0 bar moved
  1.0 → 0.95, its 0.95 evidence now clears tier 0 — the lane ENTERED chains.
- ±0 everywhere else: the 210 moved tier rows (debug/guard/tool_use/
  long_horizon/mock/test boundaries) are bar shifts from the new evidence
  joining the family pools (each verified: the only NEW pool families are
  longcat-2.5-preview=1.0 and kimi-k2.8-preview=0.85); 50 lanes moved UP and
  160 DOWN, all reported in their resolves exactly as before.

No provider whose lanes this row added was left uncovered by seed-derived
tier rows (rule 5); no quota-gate entry was needed (xkiro keys exist).

## 6b. Tier-table delta (whole-registry, measured)

model_tier: 3828 → 3871 rows (+43, −0; 210 moved). The +43 are exactly the
new lanes' category coverage. The 210 moves follow from the 42 ladder-threshold
shifts the new evidence caused (relative scale — adding a measured 1.0 family
moves bars); 50 tiers moved UP (incl. step-3.7-flash test −1→0) and 160 DOWN,
all 0.5/0.8-family rows sliding one level at the debug L-2 (0.5→0.5334), guard
L-1/L0 (0.8→0.8105, 0.88→0.89) and tool_use L-2 (0.5→0.5668) bars. The move
direction is the honest relative-scale consequence of adding STRONG new
evidence (a 1.0 longcat family); no per-variant perf was cloned (TR-044 law
held — every new perf row is family-derived via alias inheritance or the
k2 profile tags, never copied lane-to-lane).

## 7. Not done / out of scope (explicit)

- MiniMax-M3.1-Flash-Preview, qwen3.8-flash-next, Qwen-AgentWorld-35B-A3B,
  gpt-5.6-cyber, gpt-5.5-instant, claude-mythos-5/-5-1: NO registry rows —
  no keyed carrier serves any of them (per-carrier live catalog evidence in
  §1). Onboarding an unreachable id would fabricate a lane.
- command-a-plus-05-2026, north-mini-code-1-0, Spark-X2.5, ERNIE 5.1:
  row-listed not-actionable (no credentials on this deployment).
- openrouter/x-ai/grok-4.6 + 4.7 registry rows: already live ($2.0, payg).
- The four not-routed config SKU families (groq audio/guard, nvidia code
  LLMs, stepfun audio/image, synthetic syn:*): pre-existing, documented as
  config-lane/not-routed; out of this row's scope.
- opencode-go{,-2}/longcat-2.5-preview-free tier rows: these two lanes stay
  tier-less (their carrier needs an x-opencode-session header, which the
  probe harness rightly refuses to fake); the FAMILY evidence lands via the
  xkiro SKU + alias chain instead. Registry-wide `openrouter` unranked mass
  (273 lanes, key expired 401 — documented in router_rank_audit.py itself)
  is unchanged by this row.

Generated: 2026-10-12T16:42:19
