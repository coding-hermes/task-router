# Task-router dogfood diagnostics — 2026-11-06

Scheduler tick `task-router-dogfood-2026-11-06-12-08-29`. Target: `/home/kara/task-router`
(project row `task-router`, repo `https://github.com/coding-hermes/task-router`).
Skill: `coding-hermes-dogfood` (skill-driven executor; the old `dogfooding.ts` driver is retired).

This file records HOW the run was done, what broke, and the right way to repeat it. It is written
for the next agent or user who asks "does this project actually work?" — read it with
`2026-11-06-integration.md`.

## Identity of what was probed

| Surface | Identity |
|---|---|
| canonical checkout | `/home/kara/task-router`, HEAD `586dcf6b16bde9a3b722a65a805bf4e090819a52`, clean, `HEAD...origin/main` = `0 0` |
| source-fidelity copy | `git archive origin/main` → `~/.hermes/cache/scratch/tr-df-20261106-1208` (no `.git`) |
| git-metadata copy | `git clone --no-hardlinks --branch main` → `~/.hermes/cache/scratch/tr-clone-20261106`, HEAD verified = `586dcf6b`, clean before use |
| evidence dir | `~/.hermes/cache/scratch/tr-df-20261106-1208-ev/` (numbered logs 01–54) |
| board | `/home/kara/task-router/.coding-hermes/board/tasks.jsonl` (457 → 458 rows) |
| install leg | bunker-mvp, agent `301926f4`, evidence `/tmp/bunker-qa-evidence-20261106T122522Z-3139047.jsonl` |

Two copies on purpose: the archive is the honest source-fidelity surface, and the clone is the one
that can run the Makefile (which shells out to git) and that stamps a real commit into `/health`.
`/health` on the archive reports `commit: unknown` — that is the archive transport, NOT a product
defect, and it is not reported as one.

## The right way to run this dogfood

```bash
SCR=~/.hermes/cache/scratch/tr-df-20261106-1208
mkdir -p $SCR && git --git-dir=/home/kara/task-router/.git archive origin/main | tar -xf - -C $SCR
cd $SCR && python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/pip install duckdb
.venv/bin/router spawn P1_CODING --format json     # pre-seed: resolves from data/tables, fallback_used=true
.venv/bin/router validate                          # pre-seed: exit 1, "registry.exists: missing ... run scripts/router_seed.py once"  (documented)
.venv/bin/router seed --allow-unsatisfiable        # flagless seed refuses (see below) and STILL writes
.venv/bin/router validate --json                   # seeded: {"valid": true}, 14/14 checks
```

Reality check before trusting any of it: `TASK_ROUTER_HOME` is sticky in a shell. If you export it
to an empty dir after seeding, `spawn`/`estimate` silently fall back to `data/tables`
(`source: data/tables`, `fallback_used: true`) and `validate` exits 1 with a missing-registry issue.
That is the TR-345 class of confusion and it cost this run one mis-read before the env was unset.

## What broke, in order

1. **`router seed` refuses but writes anyway** (the tick's filed finding, `TR-1000976681059248`).
   Order in `scripts/router_seed.py`: `_dump_registry()` at `:1946`, the committed
   `data/tables/*.jsonl` sync at `:1948-1957`, and only then the TR-383 satisfiability check at
   `:1991-2031` which prints `Seeding REFUSED` and `sys.exit(1)` at `:2031`.
   Measured on the clean clone with no env vars: `make seed` (Makefile:59-60, no flags) → rc 2 in
   50.4 s; `git status --porcelain` then shows 2 modified tracked files —
   `data/tables/category_levels.jsonl` (20 lines, float repr only) and
   `data/tables/model_tier.jsonl` (22 lines, **11 semantic tier changes**, e.g. MiniMax-M2.7 `mock`
   tier −3 → −2) — while `make validate` exits 0 with 14/14 checks on the registry the refusal wrote.
   So the README's `make seed && make validate` claim (README.md:54) is false at this HEAD in the
   direction nobody would guess: the *second* half is the green one.
2. **`math` has no routable lane** (pre-existing, `TR-1000976681059246`, reproduced here).
   `SATISFIABILITY FAIL: 6 of 264 rating(s) ... math: levels 0..5` — the only holder is
   `ollama-cloud/qwen3.5:397b`, disabled and unpriced. Flagless seed rc 1, `--allow-unsatisfiable`
   rc 0. Same verdict at `12f8981` (row's evidence) and at today's `586dcf6`.
3. **`scripts/router_mirror_account.py --commit` is dead** (pre-existing `QA-TASKROUTER-13`,
   reproduced live). The module imports `argparse, json, pathlib, shutil, sys, time` and no `os`,
   then calls `os.path.join` at `:71`; `--commit` raises
   `NameError: name 'os' is not defined` (rc 1) *before* writing, so `models.jsonl` stays intact —
   a second paid seat still cannot be mirrored with the shipped tool. `--dry-run` works.
4. **QA harness cells, not product cells.** `QA-TASKROUTER-8`'s premise re-checked: `pyproject.toml`
   carries `[tool.ruff]` + `[tool.ruff.lint] select = ["E4","E7","E9","F","I"]` and the board venv
   ships ruff 0.15.22, so a lint cell reporting N/A is a detector gap, not a missing config.

## What did NOT break (checks that could have failed and didn't)

- Fresh install: venv 2.63 s, `pip install -e .` 5.72 s, `pip install duckdb` 7.48 s (Python 3.14.9).
- Pre-seed contract exactly as documented: `spawn` rc 0 with `source: data/tables`,
  `fallback_used: true`; `validate` rc 1 naming `registry.exists` and the fix command.
- Seeded contract: `validate --json` → `{"valid": true}`, 14/14; `spawn P1_CODING` →
  `source: registry.json`, `fallback_used: false`, 11 hops, head `opencode-go/deepseek-flash`
  at `0.029538` plan-effective.
- Fail-open: `spawn definitely-not-a-project --format json` → rc 0 + JSON `error`, no head.
- `router decide` (ACP-ROUTER-001) end-to-end, incl. the operator key bootstrap
  (`--init-receipt-key`, verified only in an isolated `TASK_ROUTER_HOME` so host state was untouched):
  receipt `eligible: true`, 42 candidates, `valid_until` = +900 s; `--verify` → `ok: true`,
  `signature_ok: true`, `issued: true`; a receipt with `head.model` edited → rc 1 with
  `DIGEST_MISMATCH` + `RECEIPT_SIGNATURE_INVALID`. The tamper gate is real.
- Read-only server refuses mutations — `POST /api/v1/execution-decision` → 403, `POST /ledger/start`
  → 403, both naming `--mode read-only`.
- `POST /api/v1/outcomes` answers 200 in read-only mode **by design** (`docs/integration.md:195`,
  `READ_ONLY_POST_PATHS` in `scripts/router_server.py:1374`), and validates its body. Not filed:
  documented, tested, and the exposure question is already owned by `TR-378`/`TR-375`.
- `router status --json` does not exist — it is `--format json` (`--json` is `circuit`'s flag).
  My own invocation error, not a docs defect.

## Performance (measured, not felt)

Host loadavg during the run: 36.8 / 32.5 / 25.5 on 16 cores — every absolute below is a loaded-host
number and is not comparable to earlier ticks measured in quieter windows.

| Operation | Mean ± σ | Runs | Command |
|---|---|---|---|
| spawn P1_CODING | 317.2 ms ± 22.0 | 15 | `router spawn P1_CODING --format json --quiet` |
| status | 324.3 ms ± 29.2 | 8 | `router status --format json` |
| estimate | 690.0 ms ± 164.4 | 8 | `router estimate P1_CODING --tokens-in 100000 --tokens-out 100000` |
| decide | 627.3 ms ± 77.4 | 6 | `router decide --request … --format json` |
| seed | 27.886 s ± 1.037 | 3 | `router seed --allow-unsatisfiable` |
| HTTP /resolve | 451 ms | 1 | `GET /resolve?project=P1_CODING` |
| HTTP /api/ui/chain | 482 ms | 1 | `GET /api/ui/chain?project=P0_FORE` |
| HTTP /health | 90 ms | 1 | `GET /health` |

No PERF row filed. Every interactive operation is subsecond — nobody notices 0.3 s. `seed` is the
only felt wait (~28 s) and it is a one-shot admin/quick-start step whose input scale has not moved
(`models` 2023 → 2081 rows, `model_tier` 3910 → 3896 between 2026-10-26 and today), so the earlier
11.85 s figure is a different-window measurement, not a regression this tick can claim. The
attribution arm (`cProfile` of `scripts/router_seed.py`) was **BLOCKED by the harness and did not
run**; that boundary is why no seed PERF row exists. If a later tick wants it, the honest starting
point is `python -m cProfile -o /tmp/seed.prof scripts/router_seed.py --allow-unsatisfiable`
followed by `pstats` sorted on `tottime` — and note that the satisfiability check re-reads every
table (`router_satisfiability.load_tables`) after duckdb already did.

## Install leg (ephemeral bunker)

`bunker-mvp`: `/health` → 404 (a live bunkerd answers 404 there; 000 would mean down),
`bunker-qa.sh status` → `bunkerd=active docker=yes act=no uidmap=yes`, client `bunker 0.2.0`.
Launched per `bunker-qa.sh launch /home/kara/task-router` → agent `301926f4`, evidence
`/tmp/bunker-qa-evidence-20261106T122522Z-3139047.jsonl`. Collection is bounded (`collect`) and
recorded in the integration report; teardown state is in that report too.

## Left behind

- Board row `TR-1000976681059248` (P1) + its `task_created` event; verified byte-identical 457-line
  prefix, exactly one occurrence, no duplicate ids. `boardctl validate` was already FAIL before the
  write (422 errors in legacy `events.jsonl` id ordering) and is 422 after — this run added zero.
- `skills/task-router-usage/SKILL.md`: the fresh-clone recipe now uses
  `router seed --allow-unsatisfiable` and warns that a refusal still writes.
- This file, `docs/dogfood/2026-11-06-integration.md`, and the `.coding-hermes/dogfood-log.md` entry.
- No source change, no scheduler mutation, no repo visibility/permission change, no credentials minted.
