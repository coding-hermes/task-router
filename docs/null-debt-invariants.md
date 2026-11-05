# Null-debt invariants (INV1-INV4)

`scripts/null_debt_invariants.py` is the null-debt PREVENTION gate. The census
(`scripts/data_null_census.py`, TR-197) classifies nulls after the fact; the
invariants fail the SHAPES that create debt before the census has to count it.
The script is stdlib-only, reads `ROUTING_DATA_DIR` (default
`<repo>/data/tables`), and stamps resolve from `null_reasons.jsonl` beside that
directory.

Exit codes: `0` all enforced invariants hold, `1` violation, `2` internal
error (bad args, git failure in diff mode).

## INV1 — the wipe shape (TR-201)

No LIVE lane is >=50% null across its table's measured (non-structural)
columns. This is the TR-198 wipe shape: a lane whose previously-populated
values were replaced with nulls. Structural columns (TR-197 designed absence:
`perf_*`, `thinking`, ...) are excluded from the measurement; per-table
headroom lives in `ALLOWED` with a reason.

## INV2 — the dead column (TR-201)

No column is 100% null across its carrying rows unless the (table, column)
pair is in `NULL_OK` WITH a reason string. Columns nobody writes are
absent-key sparse, not wiped; a carried-but-always-null key is the debt signal.

## INV3 — anonymous lifecycle dates (TR-201)

No row in a `lifecycle_gate.TABLE_KEYS` table carries a non-empty
`valid_to`/`available_from` without a non-empty `lifecycle_source`. The
predicate is lifecycle_gate's (TR-199 R4 "no anonymous dates"); the gate
delegates and does not re-implement it.

## INV4 — capability data must exist or the reason must be stated (TR-362)

No models.jsonl lane may be routable — live per `data_null_census.is_live`:
not `disabled`, not `archive`, no `valid_to` — while carrying NO `perf_*`
value for ANY of the ten perf categories
(`perf_agent_tick`, `perf_long_doc`, `perf_debug`, `perf_schema`,
`perf_e2e_vision`, `perf_review`, `perf_delegation`, `perf_guard`,
`perf_mock`, `perf_reasoning`), unless the reason is STATED in one of two
escapes:

1. **Stamp escape** — every one of the ten perf fields is stamped for that
   `(provider, model)` in `data/null_reasons.jsonl` with a `reason` from the
   AC2 vocabulary (`data_null_census.REASONS`):
   `disabled`, `archived`, `retired-by-date`, `not-published-by-provider`,
   `unpriced-pending-sticker`, `no-sample-yet`, `unknown-by-design`.
   A PARTIAL stamp set is a violation naming the unstamped fields. The gate's
   job is that the reason is STATED, not inferred — `no-sample-yet` and
   `unknown-by-design` are legitimate members. The vision exemption
   (a lane missing only `perf_e2e_vision` accepts an `unknown-by-design`
   stamp) falls out of the stamp rule; there is no code special-case.
2. **Disabled escape** — the lane carries `disabled: true` with a non-empty
   `disabled_reason`.

Root cause this closes: models.jsonl held ~2000 rows with ~1955 `perf_*`
nulls and the census exited 0 because "no sample yet" is exempt. INV4 makes
the STATE of each new lane a decision someone wrote down.

### The two INV4 modes (same predicate)

- **Whole-tree (default, no flags) — REPORT-ONLY, never fails.** Prints
  `REPORT-ONLY: N routable perf-less lane(s)` and includes them under the
  `inv4` key in `--json`. It never affects the exit code: the committed table
  legitimately holds hundreds of pre-existing perf-less lanes (768 at gate
  introduction), and the whole-tree run grades `scripts/gitreins-guard-tests.sh`
  and CI (`.github/workflows/ci.yml`) — making it fatal would red every gate
  without a single new violation. INV1-INV3 keep their exact pre-TR-362
  behavior; `--json` gains only an additive `inv4` key and the `--publish`
  state doc keeps its six-key shape.
- **Diff mode — the commit gate.**
  `python3 scripts/null_debt_invariants.py --inv4-diff --base <ref> --head <ref>`
  compares `data/tables/models.jsonl` between the two refs row-by-row by
  `(provider, model)` and fails (exit 1) when the range ADDED or CHANGED a
  violating lane, naming provider, model and the missing perf fields.
  Unchanged lanes are not this push's debt. If the table does not exist on
  either side, or no routable lane was added/changed, it exits 0. A missing
  or bad ref, or any other git failure, is an internal error (exit 2).

## Where the commit gate is wired: pre-push, NOT pre-commit

This repo has NO pre-commit hook. `git config core.hooksPath` is `.githooks`
and that directory contains ONLY `pre-push` — so the repo's commit gate for
landed-on-main work IS the pre-push hook.

`.githooks/pre-push` gates only pushes that update `refs/heads/main` (override
with `CI_GATE_BRANCH`). In the boundary-gate loop, after
`scripts/board_commit_boundary_gate.py`, it runs:

```
"$PY" scripts/null_debt_invariants.py --inv4-diff --base "$base" --head "$local_sha"
```

with the same `base` the boundary gate computes (`remote_sha`, or `local_sha`
when the push creates the branch and the remote sha is all-zeros). A refusal
increments `blocked` exactly like the other gates, and the final refusal
message names all three gate families.

### Escape hatches (no gate bypasses silently)

- `ALLOW_NULL_DEBT=1` — bypasses the TR-362 INV4 diff gate for that push with
  a loud stderr warning naming the bypass.
- `ALLOW_RED_PUSH=1` / `CI_GATE_BYPASS=1` — the CI gate's own pre-existing
  hatch.
- `ALLOW_UNBOUND_COMPLETION=1` — the TR-251 boundary gate's hatch. Note its
  `continue` predates INV4 and short-circuits the whole loop iteration, so a
  push that uses it skips the boundary gate AND the INV4 diff gate for that
  push; the warning still fires.

## Whole-tree runs elsewhere (report-only there too)

- `scripts/gitreins-guard-tests.sh` runs `null_debt_invariants.py`
  (whole-tree) after pytest whenever the guard grades the tree — INV4 rides
  along as a count, INV1-3 as hard failures.
- `.github/workflows/ci.yml` runs it whole-tree as the "Null-debt invariants"
  step — INV4's report appears in the CI log; INV1-3 fail the build.

## Testing

`tests/test_tr362_inv4_gate.py` pins the red/green evidence: perf-less added
lane fails naming provider/model/field; the same lane with any perf value,
disabled with a reason, fully stamped, or vision-only with an
`unknown-by-design` stamp passes; a partial stamp set fails naming the
unstamped fields; unchanged lanes and missing tables exit 0; whole-tree mode
counts but never fails. Diff-mode tests run against throwaway two-commit git
repos under `tmp_path` (git config pinned to `/dev/null`), never the real
repo history. INV1-3 regression lives in `tests/test_null_debt_invariants.py`.
