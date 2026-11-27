# Dogfood run 48 — 2026-11-27 — CLI routing, cost estimate, and fresh install

Tick: `task-router-dogfood-2026-11-27-20-50-20`. Tested commit: `91888bc89ce3fd7e8d4fef4e3989c95c37aade1d` (`origin/main`). Executor: hand-run `coding-hermes-dogfood`; no product code changed.

## Promise

Task-router claims that a user can install a stdlib-first `router` CLI, seed and validate a registry, resolve a profile into a gated price-ordered chain, estimate its cost, and execute a fallback chain while preserving fail-open behavior.

## Real use

A fresh archive consumer was created outside the repository, installed into a new Python virtual environment, and exercised with isolated `TASK_ROUTER_HOME` and `ROUTER_STATE_DIR` paths.

| Operation | Result | Measured time |
|---|---|---:|
| `pip install -e .` | rc 0; `router --help` worked | install completed |
| `router seed` without DuckDB | rc 1, no output; expected dependency boundary | 0.09 s |
| `pip install duckdb` | rc 0 | 4.63 s |
| `router seed` with DuckDB | rc 0; wrote 10.5 MB registry | 55.55 s |
| `router validate --json` after seed | rc 0; `valid=true`, no issues | <1 s |
| `router spawn --profile P1_CODING --format json` | rc 0; 21 hops; head `xkiro/openai/gpt-6-luna`; `fallback_used=false` after seed | 0.53–0.64 s warm |
| `router estimate P1_CODING --tokens-in 1000 --tokens-out 500 --format text` | rc 0; 21 priced hops; chain total `$0.0352` | 1.03 s JSON path |
| `router chain-run --profile P1_CODING --dry-run --max-hops 3 --format json` | rc 0; three planned attempts; `success=false`, `final=null` | 0.59 s |

The estimate JSON carried `payg_hops=2`, `subscription_hops=18`, and `unknown_billing_hops=1`. The text summary displayed only `payg 2 · subscription 18` while claiming all 21 hops were priced; the unknown-billing hop is not surfaced in the human summary. This is filed as a usability/trust finding, not a cost-calculation defect.

The fresh-install CLI path worked after installing its documented third-party seed dependency. The isolated seed and validate path was clean. The CLI still emitted the expected `spawn ledger NOT WIRED` warning, so concurrency gating remains explicitly unverified/inactive as documented.

## Verdict

PROMISING-BUT-ROUGH. The core install → seed → validate → resolve → estimate workflow completed and returned useful routing data. The main new friction is that the human cost summary hides one of the 21 hops' billing classifications. `chain-run` still has existing open findings for exhaustion exit status, deterministic-error retries, and per-hop ledger cost; this run did not create duplicates.

## Findings

- `TR-1000976681059-1` — `router estimate --format text` omits the `unknown_billing_hops=1` classification from its summary even though the JSON reports it; the exact observed output and fix direction are in the board row.

## Fresh-machine install leg

`bunker-qa.sh launch` did run on `bunker-las-03`, creating agent `9e2371cf`; this is not a SKIPPED-install-bunker case. At the last collection, evidence had 10 rows: target binding OK, launch OK, toolchain bootstrap OK, fresh-install OK (`task-router-0.3.0`, `duckdb-2.0.1`, `pyyaml-6.0.4`, installed `router`), and lint N/A. The battery remained RUNNING, so tests and later cells are unverified and the agent was left intact. Evidence: `.coding-hermes/evidence/dogfood-20261127-run48/bunker-qa-evidence.jsonl`.

## Left behind

- This integration report.
- `docs/dogfood/2026-11-27-run48-diagnostics.md`.
- `.coding-hermes/evidence/dogfood-20261127-run48/` with command outputs and install evidence.
- A run-48 section in `skills/task-router-usage/SKILL.md`.
- The `.coding-hermes/dogfood-log.md` run record.

No code, visibility, permissions, credentials, existing row status, or scheduler state was changed.
