# CI-class reconciliation — INT-CI-3 and INT-CI-6 (2026-10-21)

Two P1 board rows name a red CI run on `main`. Both were re-verified against the
external surface (GitHub Actions) in this tick, by a method that does not depend on
the rows' own notes.

- **INT-CI-3** — "CI red on main: run 39138425455 test suite failed on docs-only
  commit 2f0d6c8" (filed 2026-10-18).
- **INT-CI-6** — "CI red on main: run 39417840143 failed after registry onboarding
  commit d643ef44" (filed 2026-10-20).

Repository: `coding-hermes/task-router` (derived from `git remote -v`, not the
on-disk folder name). Method: `gh run list --json databaseId,headSha,conclusion`
for the run ledger, `gh api repos/<repo>/actions/runs/<id>/jobs --jq .total_count`
for the job census (a run that never started reports **0 jobs** and its log route
returns HTTP 404), and `git merge-base --is-ancestor` for commit ancestry.

---

## Class A — INT-CI-3: the Full-test-suite hang

**Class definition.** The `Full test suite (pure JSON — no duckdb needed)` step hung
to the job timeout because `tests/test_symlink_path_resolution.py` read
`router_seed.py`'s path constants by **runpy-executing the module** — and that
module's whole body *is* the seed (import-time duckdb `executemany` over ~1959 model
rows plus `apply_overlay` over ~3944 `model_perf` rows). Diagnosed under INT-CI-5
from run 39195671258 (all four red runs died at ~23m45s; the last green took 48m53s).

**Fix commit.** `2d1b017` — `test(INT-CI-5): the symlink-contract probe must not
execute router_seed's seed body` (declaration-only AST slice + symlink-bound
`__file__` + write guard; `tests/test_symlink_path_resolution.py` +460/-58).

`git merge-base --is-ancestor 2d1b017 origin/main` → **ancestor = YES** and
`git diff --stat origin/main...feat/complexity-contract` is empty, so the fix is
part of the published tree, not a branch.

**Green run at the fix commit.** `gh run view 39378454973` → head `2d1b017a`,
conclusion `success`, job `test: success` — the same job that had died at step 7
after ~23m in six prior runs.

**Recurrence check (the falsifiable half).** Every completed run after `2d1b017`,
classified by its **failing step**:

| run | head | created (UTC) | failing job step |
|---|---|---|---|
| 39378676311 | 852b1be0 | 2026-10-20 06:08 | TR-251 evidence replay |
| 39382221804 | 485bd7bd | 2026-10-20 06:45 | TR-251 evidence replay |
| 39412055845 | 19dfb84a | 2026-10-20 11:01 | *(no job — 0 jobs, log 404)* |
| 39412323078 | 6cdecf4c | 2026-10-20 11:04 | *(no job — 0 jobs, log 404)* |
| 39413947028 | 1fb8a1fa | 2026-10-20 11:17 | *(no job — 0 jobs, log 404)* |
| 39417840143 | d643ef44 | 2026-10-20 11:50 | *(no job — 0 jobs, log 404)* |
| 39510427292 | 6c700124 | 2026-10-20 22:45 | TR-251 evidence replay |
| 39531615321 | bb189bef | 2026-10-21 02:11 | TR-251 evidence replay |
| 39540037938 | 837d9fec | 2026-10-21 03:42 | Full test suite (pure JSON) — **doc parity**, fixed by `e8acd7a` (INT-CI-7) |

**Zero recurrences of the hang class.** Only one run after the fix failed at the
`Full test suite` step, and it is a different failure (a missing `ROUTER_*` doc key,
`tests/test_doc_parity.py::test_all_router_env_keys_documented`), already owned and
closed as INT-CI-7 by commit `e8acd7a`. The four `TR-251 evidence replay` reds are
the board gate refusing every evidence form (owned by TR-343, fix `14b763c4`); the
four zero-job reds are Class B below.

**Local focused re-verification at HEAD** (board venv, this tick):
`python3 -m pytest -q tests/test_symlink_path_resolution.py` → **20 passed in
3.37s** (slowest call 0.27s). The pathological path is gone; the file no longer
executes the seed body.

**Current CI state.** `ed4c0f3` (HEAD at tick start) → run **39549830156 success**.
The three most recent runs before it are all `success` with 1 job each.

---

## Class B — INT-CI-6: the zero-job window

**The run the row names.** `gh api repos/coding-hermes/task-router/actions/runs/39417840143`
→ `status: completed`, `conclusion: failure`, `head_sha: d643ef44`, `run_attempt: 1`;
`.../jobs` → `total_count = 0`; `.../logs` → **HTTP 404**. The run object exists, but
no job ever existed, so there is no failing step to read — the failure is *before*
the first job.

**Attribution (already landed, before this row was closed).** Commit `36dc9da0`
("ci: unbreak workflow startup — remove step-level permissions key") names this run
and its three siblings and gives the actionlint proof:

- Cause: `19dfb84a` added `permissions: actions: read` **at step level**
  (`.github/workflows/ci.yml:183`), which is invalid GitHub Actions schema;
  `permissions` is valid only at workflow or job level. GitHub refused to start
  every run after it.
- Proof: `actionlint` on the pre-fix tree → `ci.yml:183:9 unexpected key
  "permissions" for step` (rc=1); on the fixed tree → rc=0.
- The window and its shape: runs **39412055845 / 39412323078 / 39413947028 /
  39417840143** — all `failure`, all `0` jobs.

**Class closure.** The fix run **39427428184** (`36dc9da0`, 2026-10-20T13:05:12Z)
→ `success`. A job census over the 12 most recent runs (this tick) shows **every run
after the fix has exactly 1 job** — the workflow starts again; no zero-job run
recurs:

```
2026-10-21T05:29:17Z success  ed4c0f34 jobs=1
2026-10-21T04:55:19Z success  abc7f1d9 jobs=1
2026-10-21T04:37:27Z success  e8acd7a2 jobs=1
2026-10-21T03:42:22Z failure  837d9fec jobs=1
2026-10-21T02:16:59Z success  955863c5 jobs=1
2026-10-21T02:11:23Z failure  bb189bef jobs=1
2026-10-21T00:30:53Z success  14b763c4 jobs=1
2026-10-20T23:31:47Z success  780c2785 jobs=1
2026-10-20T22:53:50Z success  21126fa8 jobs=1
2026-10-20T22:45:44Z failure  6c700124 jobs=1
2026-10-20T21:07:59Z success  b7b363c0 jobs=1
2026-10-20T17:23:53Z success  cba1de77 jobs=1
```

---

## What this record does NOT claim

- **No source change was made by this tick.** Both rows are verification-only
  closes: the implementation commits (`2d1b017` for Class A, `36dc9da0` for
  Class B) already landed on `origin/main` before this tick started.
- **No local full-suite run is claimed.** The full suite is what CI runs
  (`.github/workflows/ci.yml` → `scripts/gitreins-guard-tests.sh`, same `tests/`
  tree); the exact-head CI run is cited instead of a local replay, because it is the
  same suite executed on a machine that does not carry this box's live state.
- **Green elsewhere is not inherited.** Each run id above is cited against its own
  head SHA; no run's result is transferred to a commit it did not test.
- **Tier 2 (the evaluator verdict) is recorded on the board row separately** from
  these CI observations, and is not implied by them.

## Independent verification (GitReins Tier 2)

The GitReins task `INT-CI-3` was created and started before this record was written
and completed against the pushed closeout commit. Verdict: **passed = true**, Tier 2
COMPLETE, graded commit `e398db2b0608` (history dir
`.gitreins/history/2026-10-21/71ceb259/`, verdict printed as `dd3a9c3b` — the id and
the directory name differ; the directory is authoritative).

The evaluator re-derived the evidence independently (ancestry, the per-run failing
step for all nine reds, the job census, run 39554779492 at `e398db2` and run
39378454973 at `2d1b017`, the focused test, and both gates) and raised one factual
correction, applied above: the prose said **five** `TR-251 evidence replay` reds while
the table lists **four** (39378676311, 39382221804, 39510427292, 39531615321). The
totals reconcile either way — nine red runs: 4 evidence-replay + 4 zero-job +
1 doc-parity. The correction is recorded here and in the board's `task_verified` event.

## Tick

`task-router-foreman-2026-10-21-06-04-00`. Evidence gathered with `gh` run/job APIs
and local pytest only; no board row was edited before its evidence was read back.
CI at the final head (run 39554779492 on `e398db2`, 18/18 steps) is **success**,
including the `TR-251 evidence replay` step the repair above unblocks.
