
# Documentation audit transcript — tick task-router-docs-2026-11-17-02-40-53 (9th delivery)

Recorded 2026-11-17T08:15:31Z from a clean detached worktree at the published tip.

Audited revision: 3f675db59a2a1b2c8c8e9cb476bedd38b99d2b14

Probe root: /home/kara/.hermes/cache/scratch/tr-docs-9th/wt (git status --porcelain empty before the probes; this file is the only addition).

Canonical checkout /home/kara/task-router is behind the published tip and dirty; it was never used as a probe root or a write target.

## 1. Served contract surface (build_openapi() executed from this tree)

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ../openapi_probe.py .
openapi version: 3.1.0
paths: 36
operations: 36
methods used: ['get', 'post']
operations without operationId: 0 []
servers: [{"url": "http://127.0.0.1:9092"}]
PATHS / /api/models /api/provider-rollups /api/ui/board /api/ui/chain /api/ui/flow /api/ui/ledger /api/ui/nohops /api/ui/quota /api/ui/registry /api/ui/registry/edit /api/ui/series /api/ui/unservable /api/v1/execution-decision /api/v1/execution-decision/verify /api/v1/outcomes /chains /circuit/record /circuit/status /gaps /health /ledger/end /ledger/start /listings/model /listings/profile /listings/provider /model_status /openapi.json /pricing /profiles /providers /proxy/stats /resolve /status /ui /v1/capabilities
```

## 2. /api/ui/* contract-vs-prose parity

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ../ui_parity.py .
GET /api/ui/* in contract: 9
  GET /api/ui/board  op=getUiBoard
  GET /api/ui/chain  op=getUiChain
  GET /api/ui/flow  op=getUiFlow
  GET /api/ui/ledger  op=getUiLedger
  GET /api/ui/nohops  op=getUiNohops
  GET /api/ui/quota  op=getUiQuota
  GET /api/ui/registry  op=getUiRegistry
  GET /api/ui/series  op=getUiSeries
  GET /api/ui/unservable  op=getUiUnservable
POST /api/ui/* in contract: 1 -> ['/api/ui/registry/edit']

documented headings:
  ## GET /api/ui/registry
  ## GET /api/ui/chain
  ## GET /api/ui/flow
  ## GET /api/ui/series
  ## GET /api/ui/board
  ## GET /api/ui/ledger
  ## GET /api/ui/quota
  ## POST /api/ui/registry/edit
```

## 3. docs/ui/data-command-center.md — the two claims as written

```
$ sed -n '1,14p' docs/ui/data-command-center.md
# Data Command Center — UI data plane routes

The task-router server ships a small UI and the JSON APIs behind it, served by
the same process that resolves tasks (`scripts/router_server.py --mode
read-only`, default port 9092). The route table in the source (TR-150..TR-156
descriptions, ~lines 106–146) is the contract; this page documents it in prose.

- `GET /ui` serves the whole page as one self-contained document — no CDN, no
  build step, read-only.
- Seven `GET /api/ui/*` endpoints feed its panels.
- One mutation, `POST /api/ui/registry/edit`, exists for edit mode.

Every search/scan endpoint follows one honesty rule (TR-151): **a response
always says how much of the store it actually read** — `rows_scanned`,
```

```
$ grep -nE '^#{2,3} ' docs/ui/data-command-center.md
27:## GET /ui
32:## GET /api/ui/registry
57:## GET /api/ui/chain
81:## GET /api/ui/flow
104:## GET /api/ui/series
128:## GET /api/ui/board
156:## GET /api/ui/ledger
185:## GET /api/ui/quota
226:## POST /api/ui/registry/edit
```

## 4. The two undocumented endpoints ARE named in README (so the dedicated page is the stale one)

```
$ grep -n 'api/ui/nohops\|api/ui/unservable' README.md
254:    disclosed), `/api/ui/nohops` (TR-195 no-hops rate + hourly buckets +
260:    `ROUTER_NOHOPS_BURST_MIN`), `/api/ui/unservable` (TR-344 the
```

## 5. docs/duckbrain-namespace.md — the data inventory block as written

```
$ sed -n '14,24p' docs/duckbrain-namespace.md
4. Gate at resolve time: quota-state (GATED), health-state (DOWN/SLOW), circuit-state (OPEN) → skip
5. Head = first OPEN pair. PAYG (deepseek) is a legitimate fallback hop — subs first, PAYG where price ranks it.

## Data (tables/, JSONL canonical)
- `level_defs.jsonl` — the -5..+5 scale → percentiles of each category distribution
- `tier_ladder.jsonl` — **the frozen absolute bands** (per category + effort, 11 `min_perf` thresholds). This is an INPUT: `model_tier` is derived from these constants, so evidence landing for one model cannot re-tier another. Recalibrate deliberately with `scripts/router_tier_calibrate.py` (dry run by default, `--write` to move it); without this file the seed falls back to live pool quantiles and says so loudly.
- `model_perf.jsonl` — (provider, model, category, perf): 24 categories × 59 models
- `category_levels.jsonl` — per-category thresholds (11 levels each): the frozen ladder UNION the pool-quantile fallback for pairs the ladder does not cover
- `model_tier.jsonl` — every model's signed level per category
- `task_profiles.jsonl` + `task_profile_requirements.jsonl` — P0_FORE / P5_VISION_E2E / P7_MOCK / P9_REVIEW + P1_CODING / P2_AGENTIC / P3_DOCS / P4_SECURITY (TR-003)

```

## 6. Table facts at this revision

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ../table_counts.py .
model_perf.jsonl rows=6898 categories=24 provider/model pairs=695
category_levels.jsonl rows=918 distinct categories=24
models.jsonl rows=2173
providers.jsonl rows=30
data/tables/*.jsonl files=34
```

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ../table_facts.py .
task_profiles.jsonl rows=11
profiles: P0_FORE P1_CODING P1_WORKER P2_AGENTIC P3_DOCS P4_SECURITY P5_VISION_E2E P6_DEFAULT P7_MOCK P8_SYNC P9_REVIEW
task_profile_requirements.jsonl rows=72 profile-key sample=['category', 'level', 'task_id']
distinct profiles in requirements=1: None
category_levels columns sample=['category', 'label', 'level', 'min_perf', 'reasoning_effort']
levels per category: agent_tick=11, code_gen=11, creative=11, debug=11, delegation=11, e2e_vision=11 (min=5, max=11)
tier_ladder.jsonl rows=264 cols=['category', 'label', 'level', 'min_perf', 'reasoning_effort']
level_defs.jsonl rows=11 cols=['label', 'level', 'qcol']
level_defs distinct categories=1
```

```
$ head -3 data/tables/level_defs.jsonl
{"level": -5, "label": "-----", "qcol": "q01"}
{"level": -4, "label": "----", "qcol": "q05"}
{"level": -3, "label": "---", "qcol": "q10"}
```

```
$ jq -r '[.category,.reasoning_effort]|@tsv' data/tables/category_levels.jsonl | sort | uniq -c | awk '$1<11'
      5 math	default
```

```
$ jq -r '[.category,.reasoning_effort]|@tsv' data/tables/category_levels.jsonl | sort -u | wc -l
84
```

## 7. Ownership search on the published board (no open row owns either mechanism)

```
$ git -C /home/kara/task-router show origin/main:.coding-hermes/board/tasks.jsonl | jq -r 'select(type=="object") | select(((.title|tostring)+" "+(.acceptance_criteria|tostring)+" "+(.reasoning|tostring)+" "+(.summary|tostring)) | test("data-command-center|api/ui|command center|model_perf|level_defs|category_levels|duckbrain-namespace";"i")) | "\(.id)\t\(.status)\t\(.title[0:110])"'
TR-002	complete	Category data quality — guard/mock saturation + multilingual neutral
TR-008	complete	Remove phantom opencode-go/ox-alpha-free from routing registry — every chain heads with an unavailable model
TR-150	complete	Task Router Data Command Center — v1 web UI served by the router itself
TR-151	complete	UI API: ledger search + row browser (the raw data)
TR-152	complete	UI API: traffic + cost time series (how much we are proxying)
TR-153	complete	UI API: per-request flow drill-down (debug one call end to end)
TR-154	complete	UI API: chain explorer + live gate state (why this lane and not that one)
TR-181	complete	Tier coverage: make the ratings the classifier ACTUALLY produces servable (91.6% -> 72.6% unsatisfiable on a f
TR-188	complete	One end-to-end battery: a real request through the whole stack, with assertions on the row
TR-204	complete	task-router-sync writes a false last-success marker on a failed run, and its >100kb table snapshots fail silen
TR-226	blocked	clinepass lanes are unmeasurable: the prober sends bare ids, the API requires modelType/model
TR-233	complete	clinepass lanes are unmeasurable: the prober sends bare ids, the API requires modelType/model
TR-294	pending	DEPLOY AND PROVE: the classifier work and the measured ordering are on branch feat/complexity-contract and ine
TR-296	review	THE CAPABILITY TERM IS MISSING FOR THE MODELS THE COST RANKING NAMES: no task-level benchmark row for MiMo v2.
TR-195	complete	A no-hops-rate baseline and threshold (the incident had no alarm)
TR-324	complete	DOGFOOD TR-212 quota panel: /api/ui/quota 404s on the router web door (:9093) — the panel only exists on the A
INT-CI-2	complete	CI red on main: run 39094596521 test timeout in test_symlink_path_resolution.py
DOC-15	pending	P4 — Document API readiness, model-browser, rollup, outcome, listing, and serving-mode routes
TR-353	complete	Classifier vocabulary omits ui_frontend: rankable in category_levels but rejected as unknown, degrading every 
TR-369	complete	Capability contract: STORE + PUBLISH + ACCEPT the same vocabulary (three measured gaps)
TR-370	complete	Perf store parity: seed must read all 24 category rows, not UNPIVOT 10 legacy columns
TR-371	pending	Ship the no-hops alert to Alertmanager instead of an unreachable CLI verdict
TR-382	pending	Capability vocabulary hole: 14 dimensions profiles demand have no column in models.jsonl, so the classifier's 
TR-384	pending	[P2] math has no routable lane: the only math-tier lane is disabled and unpriced, so ratings 0..5 cannot be se
TR-388	pending	Redis read plane for the routing registry: JSONL stays the source of truth, version-stamped so a stale cache i
TR-1000976681059246	pending	router_seed.py refuses flaglessly: the math capability has no measured lane (blocked holder disabled+unpriced)
INT-CI-20	pending	CI red on main: TR-363 (d8e2e17) sets P4_SECURITY guard/review to -1 and leaves five dependent expectations un
QA-TASKROUTER-9	pending	ui-probe cell reports N/A 'no web UI detected' while scripts/router_server.py serves an HTML UI at /ui (HTTP 2
TR-1000599737158536	blocked	Commit-time usability guard: .git/hooks/pre-commit never references the gate, so incomplete model data commits
TR-389	complete	Commit-time usability guard: .git/hooks/pre-commit never references the gate, so incomplete model data commits
TR-396	pending	[P3] 4 latent model_perf case-variant collision groups remain (qwen3.7-flash, qwen3.8-27b, qwen3.8-omni-flash,
TR-398	complete	Effort axis in capability storage: model_perf rows keyed (model, category, reasoning_effort)
TR-399	pending	Ingest change: probes and benchmark pulls record the effort level they ran at
TR-1000976681059261	pending	[P2] TR-1000976681059257's premise does not reproduce at 326d36ec: a clean archive seeded with --allow-unsatis
DOC-23	pending	P4 — No documented refresh path for data/tables/benchmarks.jsonl (scripts/router_market_ingest.py) or cost_eff
DOC-24	pending	P4 — docs/ has no reader entry point: 13 reader-facing pages (incl. docs/failure-contract.md, docs/muster-spec
DOC-26	pending	P4 — ROUTING_TIER_LADDER_IGNORE (the seed silently ignores the frozen tier ladder) is named in zero docs
DOC-30	pending	P4 — README CLI reference and docs/quota-gate.md omit `router quota accounting|pacing|burn`
DOC-33	pending	P4 — docs/muster-spec.md carries three stale operation counts (19 / 30 / 31) for a contract that serves 36, an
```

## 8. Surfaces measured clean this sweep (no finding)

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ~/.hermes/skills/coding-hermes/coding-hermes-docs/scripts/doc_link_census.py . --samples 5
root: /home/kara/.hermes/cache/scratch/tr-docs-9th/wt
markdown files scanned: 190 (excluded: .git, .gitreins, node_modules, .venv, venv, dist, build, target, .coding-hermes/board/evidence)
dead relative links: 1
distinct dead targets: 1
  resolve by suffix swap [('.md', '.spec.md')]: 0   <- mechanical generator class
  resolve neither way: 1   <- pages never captured; needs a manifest, not a rewrite
by top-level path:
        1  docs
SAMPLE docs/reviews/tr388-quorum/seat1-glm.md -> r  [no renamed sibling (never captured)]
```

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ~/.hermes/skills/coding-hermes/coding-hermes-docs/scripts/doc_anchor_census.py . --all
md files scanned: 190
dead internal anchors: 0
```

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ../doc_flag_parity.py .
verbs from `router --help`: 26
VERBS chain-run circuit clinepass decide diff estimate gaps ingress learn ledger lifecycle maintain metrics modelsdev outcomes plan-sweep pricing probe probefix quota seed server spawn status validate web
documented (verb,flag) pairs: 38

DOCUMENTED FLAGS ABSENT FROM THE VERB SURFACE: 2
  metrics --prune  <- docs/decisions/review-tr-003-metrics-ledger-retention.md:114
  metrics --retain  <- docs/decisions/review-tr-003-metrics-ledger-retention.md:114
```

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ../doc_examples_battery.py .
rc=0	status (README:65)	{
rc=1	validate (README:117)	[FAIL] registry.exists: missing: /home/kara/.hermes/cache/scratch/tr-docs-9th/state-probe/registry.json — run scripts/router_seed.py once to generate it
rc=0	gaps (README:118 area)	== registry data-quality gaps (755 of 755 active models) ==
rc=0	pricing (README:119 area)	public-price fill: 480 rows stamped from models.dev sticker
rc=0	spawn --list-profiles (README:114)	{
rc=0	spawn P1_CODING --format json (README:62)	{
rc=0	spawn --profile P0_FORE --format json (README:93)	{
rc=0	spawn --profile-req ... (README:115)	{
rc=0	estimate my-project --tokens-in/out (README:120)	{
rc=0	estimate --project (README:118)	{
rc=0	diff 2026-08-31 2026-09-01 (README:121)	{
rc=0	circuit status --json (README:116)	{
rc=0	metrics top (README CLI table)	== metrics (708 hops)
rc=0	ledger status (README CLI table)	WARNING: ledger NOT WIRED — no trace rows found; the
rc=0	decide --help	usage: router_decision.py [-h] [--request PATH] [--verify PATH]
rc=0	quota status	plan-window quota gates — /home/kara/.hermes/stand-in/docs/task-router/probe-024053c-5th/cli-state/quota-state.json
```

```
$ cd /home/kara/.hermes/cache/scratch/tr-docs-9th/wt && python3 ../default_parity.py .
documented env rows: 127
rows with a single literal default in code (comparable): 21
default-cell mismatches: 4
  docs/configuration.md:49  ROUTER_CLASSIFIER_MODEL  doc='deepseek-v4.1-flash'  code='glm-5.3-flash'  src=['scripts/router_classify.py']
  docs/configuration.md:50  ROUTER_CLASSIFIER_KEY_ENV  doc='API_SERVER_KEY'  code=''  src=['scripts/router_classify.py']
  docs/configuration.md:142  ROUTER_DEGRADED_WALL_S  doc='120 seconds'  code='120'  src=['scripts/router_server.py']
  docs/configuration.md:143  ROUTER_PROXY_IDLE_TIMEOUT_S  doc='1800'  code=''  src=['scripts/router_server.py']
```
