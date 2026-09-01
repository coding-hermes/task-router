# Registry Maintenance (TR-005)

How the model-routing registry stays fresh: one script reprices models from
live OpenRouter data, rebuilds the derived tables, exports to both namespaces,
writes the daily chains snapshot, and commits.

## 1. The maintenance loop

One command runs the whole cadence:

```bash
cd task-router && ~/.hermes/venvs/board/bin/python3 scripts/router_maintain.py all
```

Add `--dry-run` to preview: it prints exactly what would change (price diffs,
exports, files, git commands) and writes nothing.

Step order: **reprice → seed → export → snapshot → commit**.

- **seed failure ABORTS** the run (exit non-zero) — the derived tables are the
  registry's backbone; exporting stale-but-consistent tables is fine, exporting
  broken ones is not.
- **reprice is fail-open**: any spot-check error (missing `OPENROUTER_API_KEY`,
  network down, a bug in plan collection/apply) prints a WARNING and continues.
  Reprice never blocks seed/export/snapshot/commit.
- `ROUTING_DB=<path>` env var overrides the database path for the whole loop
  (reprice, seed, export, snapshot) — used for testing against a scratch copy.

## 2. Pricing formulas

Project pricing rules, as implemented in `scripts/router_maintain.py`.

### deepseek

```
normalized_price = OpenRouter in-price of the EXACT matching OR id (deepseek/deepseek-*)
price_evidence   = 'or-spot-<date>'
```

Exact leaf match first: `deepseek-v4-pro` must price from `deepseek/deepseek-v4-pro`,
never from longer leaves like `deepseek/deepseek-v4-pro-0813` or
`deepseek/deepseek-v4-flash-vision-exp`. Longest-prefix is only a fallback when no
exact leaf exists.

### opencode-go

```
normalized_price = $12/5h ÷ req-per-5h ÷ 31,250 tok/req
budget unknown  → blended estimate: 0.96 × OR-in + 0.04 × OR-out of the matching id
```

Note: the `kimi-k2.7-code`, `kimi-k3`, and `mimo-v2.5` rows currently have NO
matching family in `SPOT_FAMILIES` (`deepseek`, `glm`, `qwen`, `gpt-5.6`) →
skipped unchanged. `glm-5.2` and `qwen3.8-*` ARE repriced from OpenRouter.

### zai-glm — STATIC, never repriced from OpenRouter

Official GLM Coding Plan points/M × $0.03 (the 3×-quota ratio; source:
model-intelligence reference 2026-08-27). The rows carry these values with
evidence `official formula`:

| model               | points/M | peak $/M | offpeak $/M |
|---------------------|----------|----------|-------------|
| glm-5.3-flash       | 2.3      | 0.069    | 0.0345      |
| glm-5.3             | 6.9      | 0.207    | 0.103       |
| glm-5-turbo         | 5.7      | 0.171    | —           |
| glm-4.7             | 4.6      | 0.138    | —           |

Off-peak rows are exactly half the peak value. NEVER overwritten from OpenRouter:
OR glm prices are USD per 1M tokens, not zai credit points — there is no live
credit source on OR.

### estimate rows on OR-backed providers

Rows whose `price_evidence` contains `estimate` are repriced to the OR in-price
of the matching id.

### Never repriced (sub-plan / non-OR providers)

`clinepass`, `ollama-cloud`, `kimi-for-coding`, `neuralwatt`, `minimax`,
`stepfun`, `synthetic`, `groq`, `grok-build`, `crof`, `openai-codex`,
`zai-glm`.

### Spot-check source

The optional spot-check helper configured by `ROUTING_SPOT_CHECK` queries public
pricing sources using a locally supplied `OPENROUTER_API_KEY`. Intraday prices
can move materially, so the daily reprice is the freshness mechanism.

## 3. Namespace export + commit

- **Base tables** (`archetypes`, `benchmarks`, `models`, `projects`,
  `providers`) are written to the routing ns `tables/` and copied to the
  task-router ns `tables/`.
- **6 derived tables** (`category_levels`, `level_defs`, `model_perf`,
  `model_tier`, `task_profile_requirements`, `task_profiles`) are mirrored
  routing → task-router (they were previously a manual copy step).
- **Snapshot**: `chains/<date>.md` lands in the task-router ns `chains/` plus a
  dated copy in `docs/chains-<date>.md`.
- `commit` does **git add + commit in BOTH namespace repos**. No push from the
  script — the operator/foreman pushes the s3daily remotes.

## 4. Weekly snapshot operator one-liner

For weeks where running the full loop is unnecessary but the chains record must
exist:

```bash
cd task-router
~/.hermes/venvs/board/bin/python3 scripts/router_maintain.py snapshot
```

Commit or publish any optional namespace mirror through its own documented
repository workflow. The router maintenance command does not push this source
repository.

Decision (AC2): a documented operator command, not a cron, keeps snapshot
creation explicit and avoids cron-context publish complexity.

## 5. Quota policy review

State file: the deployment-configured `quota-state.json`.

GATED entries as of 2026-08-27:

| provider   | reason                                                                                     |
|------------|--------------------------------------------------------------------------------------------|
| grok-build | "pool size UNPUBLISHED (R-09) — premium-only until measured by pilot ledger"                |
| crof       | "wrapping canary pending (R-10/A10) — inflation 328x measured, mechanism open"              |

Both gates have `window_exhausted false` and `concurrency_free false`.

Policy: GATED pairs are excluded at resolve time by the breaker/resolver — the
resolver hops past them and reports the reason.

Review triggers:

- **grok-build** — re-evaluate when pilot-ledger data measures the pool size.
- **crof** — re-evaluate when the wrapping canary (A10) closes.

Review verdict 2026-08-27: both reasons current; no state change; next review
on either trigger.

## 6. Fail-open doctrine

`router_spawn.py` and `router_maintain.py` never block the scheduler or the
maintenance loop on spot-check errors: any failure → warning + continue (for
`router_spawn.py`: `{"error": ...}` + exit 0).
