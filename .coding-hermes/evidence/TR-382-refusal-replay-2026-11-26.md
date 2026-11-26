# TR-382 — Refusal Replay: 2026-11-03 unservable-rating refusals vs the current registry

Date of analysis: 2026-11-26
Worktree: /home/kara/worktrees/task-router-TR-382 (branch wt/TR-382, base origin/main @78a6b14c)
Registry state: data/tables as of this worktree (seed run today, SATISFIABILITY OK over 918 emit-domain ratings), registry.json 10,486 KB.

## Headline counts

| metric | count |
|---|---|
| failed ticks on 2026-11-03 with `error LIKE '%no eligible model%'` | **173** |
| of which `failure_reason = "unservable-rating"` | **173** (100%; the row's "~153" estimate ran low — every one of the 173 is unservable-rating, no other requirements_unmet variant appears) |
| replayed against the CURRENT registry (router's own `router_satisfiability.diagnose_request`) | **173** (0 silent drops) |
| **now resolve to a lane** | **173 / 173 (100%)** |
| still unservable | **0** |
| also fully servable when replayed against the FULL pre-clamp inferred matrix | 173 / 173 |

## Method

1. Source rows: `sqlite3 ~/.hermes/coding-hermes/scheduler.db "SELECT id || '||SPLIT||' || replace(replace(error, char(10), ' '), char(13), ' ') FROM ticks WHERE date(spawned_at)='2026-11-03' AND status='failed' AND error LIKE '%no eligible model%'"` → 173 rows (the `ticks` PK is `id`, not `tick_id`).
2. Each error embeds the router's 503 payload as JSON after the `gateway POST: HTTP 503: ` prefix. 18 payloads parse as full JSON; the other 155 are truncated mid-JSON (the gateway caps the stored error text), so the demanded `(category, level)` pairs were extracted by regex over the `_router.requirements` fragment: the first `"levels": {…}` block (the clamped demand the resolver actually asked for) and the `"no lane holds <cat>=<level>"` clauses.
   - Cross-validation: on the 18 fully-parseable payloads, regex-extracted `levels` and `matrix_inferred` agree with the JSON-parsed values **18/18** — the fallback is faithful on this corpus.
3. Replay used the router's OWN satisfiability logic, not a hand-rolled check: `scripts/router_satisfiability.py::load_tables` (tables from this worktree's `data/tables/`), `capability_index`, and `diagnose_request(reqs, tables, index)` per tick, where `reqs` = the tick's clamped levels. A tick "resolves" iff `diagnose_request` reports `missing == []`.

## Historical failure dimensions (what the refusals died on, deduped per tick)

| demanded pair | ticks naming it (of 173) |
|---|---|
| long_horizon=2 | 129 |
| tool_use=3 | 126 |
| tool_use=2 | 46 |
| spec_docs=2 | 39 |
| mechanical=1 | 39 |
| security=1 | 21 |
| spec_docs=1 | 20 |
| long_horizon=3 | 19 |
| mechanical=2 | 3 |
| test=2 | 2 |
| spec_docs=3 | 2 |
| long_horizon=4 | 1 |
| security=3 | 1 |
| long_horizon=1 | 1 |

(Sums exceed 173 because a single tick can name several dropped dimensions.)

## Current-registry servability of every demanded category

| category | best routable tier today | routable holders |
|---|---|---|
| agent_tick | 5 | 297 |
| code_gen | 5 | 346 |
| creative | 5 | 198 |
| debug | 5 | 250 |
| delegation | 5 | 256 |
| guard | 5 | 195 |
| long_doc | 5 | 262 |
| long_horizon | 5 | 341 |
| mechanical | 5 | 193 |
| reasoning | 5 | 225 |
| review | 5 | 167 |
| security | 5 | 24 |
| spec_docs | 5 | 199 |
| terminal | 5 | 101 |
| test | 5 | 320 |
| tool_use | 5 | 240 |
| ui_frontend | 5 | 189 |

Every dimension the 173 ticks died on now has ≥24 routable holders, best tier 5, so every demanded level in the historical set (max demanded: long_horizon=4, tool_use=3, security=3, spec_docs=3, mechanical=2, test=2) is covered.

## Residual (honest remainder)

No refusal remains unresolved, so there is no "genuinely no capable lane" residue from THIS corpus. The one residual capability gap in the registry is orthogonal to these refusals and is declared, not silent: `math` levels 0..5 are DECLARED UNMEASURED (only live lane glm-5.2 sits 8e-05 below the lowest frozen band; the sole math-tier holder ollama-cloud/qwen3.5:397b is disabled + unpriced). Levels -5..-1 serve by the blank default tier -1. None of the 173 ticks demanded math.

## Note on the second defect named in the row

The row also measured "only 105 of 2060 models (5%) carry ANY perf_* rating". That is a different axis (rating COVERAGE) from the vocabulary hole (column EXISTENCE) closed by this row's EXTEND decision; the replay above proves the serving path for the previously-unservable dimensions, not catalogue-wide rating density.
