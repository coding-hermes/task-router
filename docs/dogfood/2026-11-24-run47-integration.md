# Dogfood run 47 — 2026-11-24 — integration report (tick task-router-dogfood-2026-11-24-19-47-57)

Tick: `task-router-dogfood-2026-11-24-19-47-57`, delivery 1 (gateway dispatch
2026-11-24 14:48:52 host-local). Measured at HEAD `3231e966` == `origin/main` at
fire time. Executor: hand-run of skill `coding-hermes-dogfood`, workdir
`/home/kara/.hermes/stand-in/dogfood/task-router` → product root
`/home/kara/task-router`.

## What a real user did

Fresh consumer clone (`git clone --no-hardlinks`) into scratch + `uv venv` +
editable install, then the README quick start verbatim against a clean
`TASK_ROUTER_HOME`:

| Milestone | Result | Time |
|---|---|---|
| clone (origin tip 3231e966) | 593-row board, clean tree | — |
| `uv venv` + editable install | rc 0 | 1.17 s |
| `pip install duckdb` | rc 0 | 0.59 s |
| `router --help` | usage passthrough, rc 0 | ~0.1 s |
| `router validate` (pre-seed) | rc 1, names `registry.exists` in `TASK_ROUTER_HOME` — matches README's documented contract | ~1.5 s |
| `router seed` | **rc 0, no refusal.** 10 MB registry into the data home; SATISFIABILITY now passes (run 42's refusal + silent-write defect is fixed at this tip; the clone stayed clean except `data/state/seed_cache/manifest.json`) | 79.95 s |
| `router validate` (post-seed) | **14/14 valid** | ~1 s |
| `router spawn P1_CODING --format json` | rc 0, 20 hops from `registry.json` (`fallback_used=false`), head `xkiro/openai/gpt-6-luna` at plan-effective $0.003867/M; the new eligibility gate reports live counts (526 eligible, 18 `ineligible:unpriced`, 131 `ineligible:no-evidence`) | 0.30–0.39 s fresh-tree / **5.4–6.95 s** once the outcomes store synced |
| `router status` | JSON overview, rc 0 | 0.66 s |
| `router estimate P1_CODING` | rc 0 | **6.18 s** (same store cost) |
| `router spawn no-such-project-xyz` | rc 0 + JSON `error: project ... not in registry` — fail-open contract holds | 0.35 s |

The outcomes-store import is part of the product now (TR-116's
`sync_if_due()` at spawn entry, `router_store_sync.py`): the first spawn on a
fleet box imports the live 534,783,661-byte store into the clone
(`data/state/outcomes.jsonl`, gitignored) and every subsequent spawn pays for
it — see PERF-2.

## Verdict

**PROMISING-BUT-ROUGH**: the documented workflow works end-to-end
(install → seed → validate → spawn → status → estimate → fail-open errors),
seed's 2026-11-09 refusal class is fixed, and the new eligibility gate names
excluded lanes instead of hiding them. What a user feels: every CLI resolve
re-parses a ~232 MB–535 MB outcomes store (~5–7 s), and running any `router`
verb from `~/.hermes` (where fleet crons live) dispatches the WRONG tree.

## Findings (board rows)

- **TR-1000976681059308 (P1)** — `router` run under `~/.hermes` grades the
  Hermes mirror tree, not the product (mechanism: `cli.py` walk-up accepts any
  tree owning `scripts/router_spawn.py`; mirror is missing
  `lifecycle_gate.py`, so `router validate` there dies with an opaque import
  error).
- **PERF-2 (P2)** — every CLI `spawn`/`estimate` re-parses
  `data/state/outcomes.jsonl` per invocation: 5.4–6.95 s warm at 461,863 rows
  vs 0.29–0.39 s with `ROUTING_OUTCOMES_FILE` empty; cProfile: 464,766
  `json.loads` = 4.76 s; `router_outcomes.py:849` ← `router_spawn.py:2516`
  (quota headroom) + `:1851` (outcome stats).
- **TR-1000976681059309 (P3)** — first-run bootstrap `quota-state.json` (30
  providers, "all providers OPEN") omits `deepseek-duckbrain-sync`; hop 2 is
  gate-out by omission on every fresh install ("policy-gate-missing-row").
- **QA-TASKROUTER-27 (P3)** — SKIPPED-install-bunker: `bunker-qa.sh launch`
  refused at preflight (disk admission 96% > 94% cap, rc 2; `bunker metrics`
  read 91.9%); no agent created, nothing to collect.

## What this run left behind

- Evidence: `.coding-hermes/evidence/dogfood-20261124t1947/` (timings, both
  spawn captures, unknown-project capture, cProfile pstats, the wrong-tree A/B
  transcript and probe, store-sync stamp, outcomes rowcount).
- `docs/dogfood/2026-11-24-run47-diagnostics.md` — the how/why trail.
- `skills/task-router-usage/SKILL.md` — a "Dogfood run 47" section (new
  pitfalls for the next agent).

**Not verified this fire:** the ephemeral-bunker install leg (SKIPPED,
QA-TASKROUTER-27); the :9092/:9093 live server surface (prior fires covered
it; this fire stayed CLI-side and read-only on fleet state).
