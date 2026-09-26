# Tier coverage — the ratings the classifier actually produces (TR-181, 2026-09-26)

The pass the brief asked for: **raise per-category tier coverage so that the rating matrices the
classifier really emits can be served by the primary chain.** Everything below is re-derived from
the ledger and the registry in this tree; every number is reproducible with the commands at the
bottom.

---

## 1. The measured problem, re-derived

`scripts/router_tier_coverage.py` (new, committed with this pass) joins the two tables that were
never joined before: the proxy ledger's `required_categories` (what the classifier ASKED for) and
the registry's `tables.model_tier` (what any lane can actually clear). A rating is
**UNSATISFIABLE** when no lane clears its whole conjunction of category levels — the eligibility
stage finds nothing and the request dead-ends.

Metric semantics mirror `router_spawn._build_chain` exactly (imported, not re-implemented):

* eligible lane = not archived, not retired, `available_from` passed, not `disabled`, **priced**;
* tier per category through `_alias_tiers` (TR-043 alias inheritance), missing tier = **-1** (the
  registry's blank default);
* two verdicts are reported — **eligible** (could a live, priced lane have served it? the dead-end
  number) and **registry** (does *any* lane know a model for this shape of work? the coverage
  number).

**Frozen sample.** The ledger is appended by a LIVE proxy, so the sample is cut at one timestamp:
`--ts-max 1790405356.2132182` (the newest proxied row when the Before run was taken).

| sample | definition | rows |
|---|---|---|
| A — `rated-nohops` | proxied rows with `required_categories` non-null **and** `hops_attempted == 0` | **95** |
| B — `rated` | every proxied row carrying a rating, served or not | **157** |

### Before → after (same sample, same metric)

| sample | UNSATISFIABLE (eligible) | UNSATISFIABLE (registry) | rows fixed | regressed |
|---|---|---|---|---|
| **A (95)** | **87/95 = 91.6 %** → **69/95 = 72.6 %** | 87 → 68 (91.6 % → 71.6 %) | 19 | 1 |
| **B (157)** | **89/157 = 56.7 %** → **69/157 = 43.9 %** | 89 → 68 (56.7 % → 43.3 %) | 21 | 1 |

The brief quotes *89 of 95*; this tree measures **87 of 95**. Four definition variants — {all
registry lanes vs live+priced} × {alias inheritance on/off} — **all** return 87, so the 2-row gap
is snapshot drift (the brief predates the TR-178 `$0`-price regeneration at 01:35 in this tree),
not a metric disagreement. Both numbers are stated rather than one being quietly adopted.

The single regressed row is `{agent_tick 2, reasoning 3, review 4, spec_docs 1, terminal 2,
tool_use 2}` — explained in §4 (a review threshold move, not lost evidence).

## 2. Per-category coverage, before → after

`tiered` = lanes carrying a `model_tier` row for that category (from the frozen metric run).
`clearing` = feasible lanes that clear the **hardest level this sample asks** in that category.

| category | asked | tiered (before → after) | clearing before → after | hardest asked |
|---|---|---|---|---|
| tool_use | 91 | 193 → 194 | 122 → 122 | 4 |
| agent_tick | 89 | 126 → **198** | 17 → **25** | 4 |
| terminal | 85 | 91 → 91 | 23 → 23 | 3 |
| guard | 77 | 93 → **127** | 10 → 10 | 4 |
| long_horizon | 65 | 85 → **105** | **3 → 122** | 4 |
| mechanical | 55 | 16 → 16 | 19 → 19 | 2 |
| review | 52 | 94 → 94 | 22 → **7** | 4 |
| delegation | 46 | 126 → **195** | 71 → 71 | 4 |
| reasoning | 43 | 146 → 148 | 45 → 45 | 3 |
| spec_docs | 22 | 60 → 60 | 4 → 4 | 3 |
| schema | 14 | 126 → **196** | 96 → **102** | 1 |
| debug | 6 | 126 → **196** | 75 → 73 | 3 |
| code_gen | 3 | 214 → 217 | 138 → **141** | 1 |
| e2e_vision | 2 | 90 → 91 | 52 → 53 | 1 |
| test | 2 | 289 → 289 | 61 → 61 | 3 |
| long_doc | 1 | 124 → 126 | 658 → 655 | −1 |

Underlying `model_perf` rows per category (2455 → 2970 rows):

```
agent_tick  126 -> 199    debug       126 -> 196    delegation  126 -> 195
schema      126 -> 196    guard        94 -> 128    mock         94 -> 127
multilingual  0 -> 120    long_horizon 85 -> 105    math          1 ->  14
code_gen    214 -> 218    reasoning   146 -> 149    terminal     92 ->  92
spec_docs    62 ->  62    mechanical   18 ->  18    review       94 ->  94
```
Eligibility impact per category is a *percentile* question, not a count question — see §4.

## 3. What was derived, and from what

Three derivations, all on the **sanctioned pipeline** (`scripts/router_seed.py` → `registry.json` +
`data/tables/*.jsonl` + the ns mirror). No table was hand-edited; no tier was invented. Each
insert carries its provenance in `source` / `source_ref`.

### D1 — a benchmark row's OWN category (`apply_benchmark_categories`, 127 rows)

The registry already mapped benchmark **sources** to categories (`BENCH_OVERLAY`). Every row whose
category is *itself* a registry category but whose source matches no pattern sat **inert**: the 61
`battery-T1-TOOL` rows labelled `agent-tick`/`delegation`, the `battery-T2-CODE`/`battery-T5-DEBUG`
rows labelled `debug`/`schema`, CoWorkBench (`agent_tick`), DeepSWE/NL2Repo (`long_horizon`), HLE
(`reasoning`), AutomationBench, SWE-bench Multilingual, AIME'26 (`math`), the vendor scorecards
(`zai-official-2026-08-26`, `qwen-official-2026-08-26`). Same evidence-trap class as the GPQA /
MCP-Atlas / AA-index fixes recorded above it in the seed.
Guard rails: the label must be a registry category after `-`→`_` normalization; the model must be
served by a live lane; the pass **only inserts** (it never rewrites a row that exists); several
values for one (model, category) resolve to `max(rel)` with explicit window tie-breaks.
Result: `source='bench'`, `source_ref='bench:<source>'`, 118 new (model, category) rows plus 7
rows that now carry their **own** measurement instead of an inherited family value (§4).

### D2 — documented estimates with no row to land on (`apply_quality_estimates` gap-fill, 60 rows)

`data/tables/quality_estimates.jsonl` is model-keyed and documented; the seed could only *replace
degenerate* values. A model with **no row at all** for `guard` / `mock` / `multilingual` silently
lost its documented value. Measured: **`multilingual` had ZERO perf rows in the entire registry**,
so *any* rating asking `multilingual` was unsatisfiable by construction; `guard` 14/42 and `mock`
14/42 values were inert. 60 rows now land (`source='estimate'`, `source_ref='QUALITY_ESTIMATES'`),
same values, same provenance, live lanes only.

### D3 — alias inheritance: transitivity + ordering (`apply_aliases`)

The resolver walks the whole alias chain (`_alias_chain`, TR-043) but the seed applied exactly
**one hop**, so a variant whose base is itself a variant inherited nothing (12 chains, e.g.
`~z-ai/glm-latest → accounts/fireworks/routers/glm-latest → glm-5.3`). Ordering inside the pass
also mattered: a variant visited before its base had rows inherited nothing (8 pairs). The pass now
walks the chain and repeats to a fixed point, with a **spelling-exact** skip test — the registry
deliberately carries several casings of one lane as separate ids, and a case-folded skip test let
one spelling suppress the other's inheritance (found and fixed while measuring: `Qwen/Qwen3.8-Flash`
lost its `reasoning` row in the first cut). Provenance names the model the value came from
(`family` / `alias:<base>`). Rows: 1451 → **1780** inherited; **0 rows lost**.

### Net movement of the derived tables

```
model_perf: 2455 -> 2970   added 515   REMOVED 0   changed 10
model_tier: 2443 -> 2940   added 497   removed 0
```
The 10 changed rows are auditable and deliberate: 7 are a model's **own** measured evidence
replacing a value inherited from its family (e.g. `z-ai/glm-5.3-flash` `review` 0.71 → 0.8865 from
`zai-official-2026-08-26`, `qwen3.6-35b-fast` `debug` 0.0 → 0.75 from `battery-T5-DEBUG`), 3 are
provenance-only (same value, now attributed to the base it actually came from). Nothing was
deleted, which the diff was written to prove.

## 4. What this pass REFUSED to derive (and why) — the honest blanks

| refused | size | why |
|---|---|---|
| **`mechanical`** | 0 rows added (16 lanes tiered, unchanged) | The only mechanical evidence in the repo is the profile-tag survey (`concise-output`, `fast-mechanical`, `filtering`) → 18 rows, 16 of them at a single value 0.72. No benchmark, no battery, no alias and no estimate in any data file measures mechanical work — `quality_estimates.jsonl` has no `mechanical` key. Inventing values would hand chain eligibility to lanes with no evidence (this fleet has already been bitten by a fake-cheap lane taking a chain head). **It is now the #1 blocker: 42 of the 69 remaining unsatisfiable rows.** |
| benchmark rows that declare themselves inert | 4 rows | The source text IS the decision: "held INERT deliberately", "inert by design - no overlay pattern in the source string", "source token withheld by design". A pass that overrides a documented decision is not a coverage pass, it is a rewrite (for the Z.AI FlashX row it would re-land the parent Flash stack's number as FlashX's own). |
| `xkiro-live-battery*` rows | 5 rows | "the values are already carried via the perf_* columns" / "no overlay pattern by design". |
| `battery-T4-INSTR-floor` rows | 94 rows | pass/fail floor test, 40+ models at 1.0 — the exact degeneracy the TR-002 quality estimates exist to replace. |
| benchmark rows under names no lane serves | 98 rows / 14 names (`cline-pass/*`, `deepseek-ai/*`, `qwen3.5-397b`, `syn:large:text`) | Their values **conflict** with the canonical names' measured ones (`cline-pass/glm-5.3` agent-tick 0.75 vs `clinepass` `glm-5.3` 0.85) — landing them would require GUESSING a model identity and would import an older battery over newer measurements. They stay inert, visibly. |
| alias rows whose base is not a lane | 25 rows (`anthropic.claude-*`, `amazon.nova-*`, `qwen.qwen3-*`) | Base model absent from this registry → the variant cannot tier; the "base has no perf rows, skipped" message is kept rather than silenced. |

## 5. Threshold side effects (a percentile scale moves when evidence lands)

Levels are percentiles of measured capability, so **adding honest evidence moves the bars**. Both
directions were measured; the two that matter:

* **`long_horizon` level 4: 0.885 → 0.850** → lanes clearing it went **3 → 122**. Cause: the newly
  landed long-horizon evidence (DeepSWE / NL2Repo, rel 0.53–0.74) sits *below* the old bar, which
  moves the q95 index down into the dominant 0.85 cohort. Honest, but it weakens the semantic of
  "+4" for this category: it now means "the dominant cohort value" rather than "above it". Flagged
  for the owner as a **scale question** (out of scope here; not hand-tuned — thresholds are data).
* **`review` level 4: 0.850 → 0.855** → lanes clearing it went **22 → 7**, and this is the *only*
  regressed row. Cause: one landed vendor row (`z-ai/glm-5.3-flash` review 0.8865, previously
  inherited as 0.71) lifted q95 above the kimi-k3 family's 0.85, demoting 18 kimi-k3 lanes 4 → 3
  while promoting 3 glm-5.3-flash lanes 0 → 4.
* Also: `agent_tick` 4 0.897 → 0.880 (17 → 25 lanes), `guard` 4 1.000 → 0.968, `schema`/`debug`/
  `code_gen` level-5 slightly up, `e2e_vision` 2 0.550 → 0.670 / 4 0.806 → 0.870 (harder).

**Net of both mechanisms the number falls:** 19 rows fixed vs 1 regressed on sample A.

## 6. Guards

* `tests/test_regression.py` — **34 passed**, including `test_golden_fixed_point_heads`
  (`P0_FORE` / `P1_CODING` / `P2_AGENTIC` / `P4_SECURITY`). **No golden head moved, so no fixture
  note was needed** — the dated-note escape hatch the brief allows was not used.
* Full suite + the repo's own commit guard (`./scripts/gitreins-guard-tests.sh`) — see the commit.

## 7. Exact commands

The before/after state snapshots, the analysis scanners (table diff, threshold diff, near-miss,
inert-evidence audit, board appender/verifier) and the frozen sample JSONs are kept OUTSIDE the repo
at `/home/kara/tiercoverage-before/` (`registry-before.json`, `tables-before/`, `evidence/`) — the
repo's precedent is that analysis scanners are not committed, only the instrument is.

```bash
# 0. freeze the BEFORE state (the live registry the scheduler was serving)
cp registry.json /home/kara/tiercoverage-before/registry-before.json
cp -r data/tables /home/kara/tiercoverage-before/tables-before

# 1. BEFORE, on the frozen sample (cutoff = newest proxied row: 1790405356.2132182)
ROUTING_DATA_DIR=/home/kara/tiercoverage-before/tables-before \
~/.hermes/venvs/board/bin/python3 scripts/router_tier_coverage.py \
  --registry /home/kara/tiercoverage-before/registry-before.json \
  --ledger data/state/outcomes.jsonl --ts-max 1790405356.2132182
#   rated-nohops — 95 rows : UNSAT 87/95 = 91.6 %   (registry-wide 87)

# 2. the change: three derivations in scripts/router_seed.py (D1/D2/D3)
#    + the new measurement tool scripts/router_tier_coverage.py
#    proven first in an isolated worktree: ~/proj-wt-tiertop (branch wt/TR-181-tiercoverage)

# 3. regenerate through the sanctioned pipeline
~/.hermes/venvs/board/bin/python3 scripts/router_seed.py
#   overlay: 317 inserted, 181 neutral-updated
#   benchmark-category rows carried into model_perf (TR-181): 127
#   quality estimate rows updated (TR-002): 102   (42 replaced + 60 gap-filled)
#   category estimate rows inserted (TR-039): 202
#   aliases: 219 variants, 1780 inherited perfs
#   model_perf rows: 2970   model_tier rows: 2940

# 4. AFTER, same sample, same metric
~/.hermes/venvs/board/bin/python3 scripts/router_tier_coverage.py \
  --registry registry.json --ledger data/state/outcomes.jsonl \
  --ts-max 1790405356.2132182
#   rated-nohops — 95 rows : UNSAT 69/95 = 72.6 %   (registry-wide 68)

# 5. the second sample (every rated proxied row)
~/.hermes/venvs/board/bin/python3 scripts/router_tier_coverage.py --sample rated \
  --ledger data/state/outcomes.jsonl --ts-max 1790405356.2132182
#   157 rows : UNSAT 89/157 = 56.7 %  ->  69/157 = 43.9 %

# 6. the guards
~/.hermes/venvs/board/bin/python3 -m pytest -q tests/
./scripts/gitreins-guard-tests.sh
```

## 8. Residual, plainly

* **69 of 95** rated no-hop rows are still unsatisfiable. The blocker census moved from
  `long_horizon 37 / mechanical 36 / terminal 6 / guard 5` to
  **`mechanical 42 / terminal 12 / guard 11 / review 2 / spec_docs 2`**.
* The single biggest blocker (`mechanical`) has **no honest evidence source** in this repo — that is
  a data-acquisition task (a battery that measures mechanical/format work), not a derivation. Filed
  as the next row rather than papered over.
* `terminal` (91 lanes, 23 clearing level 3), `guard` (10 clearing level 4), `review` (7),
  `spec_docs` (4 clearing level 3) remain thin in the same sense: evidence exists for a minority of
  lanes only.
* The `long_horizon` +4 bar devaluation (§5) is the one result I would want a second opinion on.
