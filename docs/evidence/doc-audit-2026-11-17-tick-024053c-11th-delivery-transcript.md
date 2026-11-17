# task-router docs audit — tick task-router-docs-2026-11-17-02-40-53, 11th delivery

Frozen: 2026-11-17 04:03:02 

- Audited tip (origin/main): 0d7f8eb31eb6b53e7d1863e0d42503a4b78297be
- Probe root: /home/kara/.hermes/cache/scratch/tr-docs-11th/wt (clean detached worktree; `git status --porcelain` empty at open)
- Canonical checkout /home/kara/task-router: 122 behind / 7 ahead, 6 modified + 2 untracked
  paths owned by a foreign writer (data tables, scripts/router_outcomes.py). Never used as
  probe root or write target.
- Deliveries 1-10 of this tick are historical inputs (stand-in reports); rows DOC-32..DOC-41
  published by deliveries 5-10. This delivery re-ran the audit at the tip and filed only what no
  published row owns.

## 1. Count-sentence census (reader corpus)

```
docs/catalog-freshness-2026-09-25.md:44:10 categories
docs/category-data-quality.md:13:59 models
docs/category-data-quality.md:21:24 categories
docs/complexity-model.md:45:21 categories
docs/configuration.md:280:25519 keys
docs/duckbrain-namespace.md:20:24 categories
docs/duckbrain-namespace.md:20:59 models
docs/duckbrain-namespace.md:21:11 levels
docs/lane1-catalog-freshness-2026-09-10.md:11:663 models
docs/lane1-catalog-freshness-2026-09-10.md:92:37 models
docs/lifecycle-prune-20260926.md:651:26 models
docs/model-selection-cross-pollination.md:49:5 categories
docs/muster-scope.md:113:19 operations
docs/muster-scope.md:127:19 operations
docs/muster-scope.md:128:19 operations
docs/muster-scope.md:134:1563 models
docs/muster-scope.md:228:19 operations
docs/muster-scope.md:27:19 paths
docs/muster-spec.md:109:30 commands
docs/muster-spec.md:122:31 operations
docs/muster-spec.md:15:19 operations
docs/muster-spec.md:15:30 operations
docs/muster-spec.md:273:30 operations
docs/muster-spec.md:274:30 operations
docs/muster-spec.md:293:19 operations
docs/muster-spec.md:293:30 operations
docs/muster-spec.md:95:30 operations
docs/observability-spec.md:323:24 categories
docs/ranking-coverage-TR-221.md:89:31 models
docs/specs/SPEC-REDIS-READ-PLANE.md:275:17 categories
docs/specs/SPEC-REDIS-READ-PLANE.md:352:12 tables
docs/specs/SPEC-REDIS-READ-PLANE.md:360:11 profiles
docs/specs/SPEC-REDIS-READ-PLANE.md:361:12 tables
docs/specs/SPEC-REDIS-READ-PLANE.md:414:11 profiles
docs/specs/SPEC-REDIS-READ-PLANE.md:414:17 categories
docs/specs/SPEC-REDIS-READ-PLANE.md:72:12 tables
docs/tr-361-capability-ratings-relative.md:15:060 models

```

Classification: dated snapshots (catalog-freshness-*, lane1-*, category-data-quality,
lifecycle-prune-*, coverage-*) are point-in-time reports, not living reference; muster counts
-> DOC-33; duckbrain-namespace counts -> DOC-40; observability-spec §3 -> DOC-41;
configuration.md:280 '25519 keys' is the string `ed25519 keys` (grep false positive);
model-selection-cross-pollination.md:49 '5 categories' describes ANOTHER project's selector
(reference architecture, not task-router state); SPEC-REDIS-READ-PLANE.md declares itself
implementation authority; tr-361 '060 models' is a thousands-separator artifact (1,060).
NOT owned by any row: complexity-model.md:45 '21 categories' — measured in §2.

## 2. complexity-model.md:44-45 — 'The floor is permissive today (21 categories at -5)'

The spec itself (docs/complexity-model.md:25-26) declares the registry the authority for the
category list: 'The registry is keyed by these; the registry is the authority for the list, not
this document.'

### 2a. Registry category counts at the audited tip
```
HEAD category_levels.jsonl: 24 distinct categories
HEAD model_perf.jsonl:      24 distinct categories
HEAD model_tier.jsonl:      24 distinct categories
```

### 2b. The same three tables at b1c810d2 — the commit that INTRODUCED the sentence (2026-10-03)
```
b1c810d2 category_levels.jsonl: 24 distinct categories
b1c810d2 model_perf.jsonl: 24 distinct categories
b1c810d2 model_tier.jsonl: 24 distinct categories
```

### 2c. Is there a 21-entry floor map in code?
```
$ grep -rn 'default-floor' scripts/ tests/   -> 0 hits
$ grep -rn 'default-floor' scripts/          -> no hits (empty)
$ grep -rln 'default-floor' tests/           -> no hits (empty)
```
No code artifact carries a category->-5 floor map. The JEV scorer derives its matrix from the
registry vocabulary (scripts/router_jev.py:161-171 `cats = rc.registry_categories()` -> 24), and
when BOTH scorers fail the resolver degrades to the PROFILE fallback (TR-322), not a uniform -5
matrix:
```
                _err('WARNING: complexity scoring degraded to the profile — '
                     f"{complexity_meta.get('degrade_reason')}", force=True)
                if (not args.profile_id and not args.adhoc
                        and not (board_profile_meta
                                 and board_profile_meta.get('matched'))
                        and not declared_raw_adhoc):
                    args.profile_id = DEFAULT_FALLBACK_PROFILE
                    complexity_meta['profile_fallback'] = {
                        'profile': DEFAULT_FALLBACK_PROFILE,
                        'reason': 'scoring degraded with no profile available '
                                  f'(no board declaration, no --profile, no '
                                  f'project row) — resolved via the default '
                                  f'profile {DEFAULT_FALLBACK_PROFILE} '
                                  f'(TR-322)'}
                    _err(f"WARNING: no profile to degrade to — resolving via "
                         f"the default profile {DEFAULT_FALLBACK_PROFILE} "
                         f"(TR-322); set ROUTER_SCORER credentials or declare "
```

### 2d. Birth commit of the sentence
```
b1c810d2 spec(complexity): write the design authority the owner asked for on 09-19; reopen TR-139 against live measurement
2026-10-03 16:25:52 -0500 spec(complexity): write the design authority the owner asked for on 09-19; reopen TR-139 against live measurement
```
Conclusion: the count 21 matches no artifact at the tip or at the sentence's birth commit — all
three registry tables carried 24 categories at both points, and no floor map with 21 entries
exists anywhere in scripts/ or tests/ at the audited tip. The sentence's mechanism shape (a
uniform -5 floor) also differs from shipped behavior (profile fallback).

## 3. CLI surface census

### 3a. `router --help` command list (28 commands)
```
usage: router [-h] COMMAND [args...]

task-router CLI — deterministic model routing for the coding-hermes fleet.
Each subcommand runs the matching scripts/router_<name>.py tool with
data-home env overrides applied. `router <cmd> --help` passes through to
the underlying tool's own argparse.

COMMAND
  chain-run   
  circuit     circuit-breaker state for (provider, model) pairs
  clinepass   Cline Pass plan lane diagnostics
  context-call
  decide      just-in-time execution decision: closed v1 request -> canonical expiring route receipt (ACP-ROUTER-001; --verify checks a receipt)
  diff        chain snapshot diff between two dates (head moves, price deltas)
  estimate    cost estimate for a project's chain at given token volumes
  gaps        registry coverage gap report
  ingress     
  learn       DuckBrain-backed learning loop (dump/lesson/doctrine)
  ledger      spawn-ledger start/end/status (TR-007)
  lifecycle   
  maintain    registry repair/export/reprice maintenance
  metrics     usage metrics: top providers/models/pairs, per-profile, since-window
  modelsdev   models.dev catalog sync + price refresh
  outcomes    
  plan-sweep  disable plan-outside flat-subscription lanes
  pricing     price table diagnostics
  pricing-audit
  probe       provider health probe (manual calibration run)
  probefix    resolve 404/400 model ids from probe logs
  quota       plan-window quota gates (429 exhaustion): set/clear/status; plus accounting (TR-209 L2 view), pacing (TR-210 L3 gate), burn (TR-211 L3 burn-surplus finder)
  seed        rebuild registry.json from data/tables/*.jsonl
  server      OpenAPI API server + MCP bridge (read-only | edit with API key)
  spawn       resolve a task/profile to a model chain (fail-open JSON/text)
  status      one-command overview: registry, gates, circuit, gaps (json|text)
  validate    registry/schema/state/profile integrity check (--json, exit 1 on issues)
  web         local web UI: settings editor + live resolve preview (:9093)

Data home: $TASK_ROUTER_HOME > $XDG_DATA_HOME/task-router > ~/.local/share/task-router (see task_router.paths).
```

Blank descriptions: chain-run, context-call, ingress, lifecycle, outcomes, pricing-audit (6)
-> DOC-28, byte-true. README CLI-reference table: 32 rows; duplicated commands chain-run,
lifecycle, outcomes, pricing-audit, quota (5) -> DOC-12, byte-true; absent command:
context-call -> DOC-13, byte-true. clinepass/decide/plan-sweep/probefix all present (:638-665).
No new gap.

## 4. docs/usage/context-call.md vs `router context-call --help`

### 4a. Live help probe (T3)
```
usage: router context-call [-h] [--surfaces SURFACES]
                           [--db-namespaces DB_NAMESPACES] [--json]
                           [--measure]
                           [workdir]

ONE CONTEXT CALL: code graph + DuckBrain + specs + boards

positional arguments:
  workdir               Project workdir to analyze (default: current
                        directory)

options:
  -h, --help            show this help message and exit
  --surfaces SURFACES   Comma-separated surfaces to query
                        (code,db,specs,board). Default: all.
  --db-namespaces DB_NAMESPACES
                        Comma-separated DuckBrain namespaces to query.
                        Default: task-router,routing,coding-hermes.
  --json                Output JSON instead of human-readable report.
  --measure             Include per-surface latency in output (default: always
                        included).
rc=0
```

### 4b. Delta against docs/usage/context-call.md
Page documents: workdir, --surfaces, --json, --db-namespaces, HILO_BIN env, the four-surface
table, read-only claims. Help exposes one MORE option the page never names: --measure
(scripts/router_context_call.py:815 defines it; help: 'Include per-surface latency in output
(default: always included)'). Env names on the page match source reads exactly
(scripts/router_context_call.py:84 HILO_BIN, :171 DUCKBRAIN_URL, :178 DUCKBRAIN_API_KEY);
DUCKBRAIN_URL's corpus absence is DOC-37's owned finding. AC coverage: DOC-13/14 target the
README front door, not this page's flag list — --measure is owned by no row.

## 5. Corpus censuses (re-run at the tip)
```
$ doc_link_census.py:         2  docs
  SAMPLE docs/evidence/doc-audit-2026-11-17-tick-024053c-10th-delivery-transcript.md -> docs/observability-spec.md  [no renamed sibling (never captured)]
  SAMPLE docs/reviews/tr388-quorum/seat1-glm.md -> r  [no renamed sibling (never captured)]
$ doc_anchor_census.py --all: md files scanned: 192
  dead internal anchors: 0
```
Both unresolved links sit in archival/evidence trees (docs/evidence/*10th-delivery*,
docs/reviews/tr388-quorum/*) — outside the reader-facing corpus, same classification as the
10th delivery. 0 dead anchors.

## 6. Published-state readback (reconcile duty)
```
DOC-41 marker_fields 1 priority P3 status pending
```
DOC-41 (10th delivery) carries exactly one marker, priority P3 with the P4 title label — the
mapped state TR-348 requires. Nothing to repair.

