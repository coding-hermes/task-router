# ADR TR-393: The Decline Contract — Structured Reasons for Every Non-Complete Row

**Status:** accepted
**Date:** 2026-11-07
**Owner:** task-router foreman
**Supersedes:** none (extends commit ecbc472, which introduced the shape on 8 rows)
**Superseded by:** (none)

## Context

Bane, 2026-11-06: "tell me what is on the board now that it says it can not
work on — it has to give a reason why it cannot complete the tasks". The board
already had the *shape* for an answer: commit ecbc472 put a top-level `decline`
object on 8 rows (`TR-266`, `TR-1000242751831738`, `QA-TASKROUTER-7..12`) and
grew an `OTHER_REPO` code. What it did not have was anything that made the
answer machine-checkable: no validator, no enforcement in the board-write path,
no backfill of rows that still lacked a decline, and no report command. A row
could sit in `blocked` with no reason at all, and only a human reading every
row would notice.

## Decision

**A row in `blocked`, `failed` or `review` MUST carry a `decline` object; the
contract is validated, enforced at commit time, backfilled, and answerable in
one command.**

The shape (top-level key on the task row; `code`/`reason`/`evidence`/`needs`
required and non-empty, `routed_to`/`by` accepted optional extras):

```json
{"code": "DEPENDENCY_UNLANDED", "reason": "...", "evidence": "path@sha:line",
 "needs": "...", "routed_to": "...", "by": "..."}
```

A `pending` row may have no decline — it has not been tried yet. Rows in
`complete` / `done` / `closed` are out of scope.

### The closed vocabulary, and why each code exists

| Code | Exists because |
| --- | --- |
| `PREMISE_FALSE` | Rows get re-measured and the defect turns out not to hold (e.g. TR-117's census claim). The board must be able to say "no change owed" without pretending the row is done. |
| `OWNER_DECISION` | Some actions are explicitly reserved to Bane (service restarts, public-surface changes). A blocked-on-permission row must say whose decision it is. |
| `MISSING_EVIDENCE` | The row's own acceptance criteria demand evidence that does not exist yet (a load test, a run id). Naming it prevents "blocked" from silently meaning "nobody checked". |
| `DEPENDENCY_UNLANDED` | Work is gated on another row, merge or service (the recurring shape: acceptance met, close blocked by the TR-251 evidence-replay gate). |
| `CI_RED` | A red CI run is the named blocker. |
| `ENVIRONMENT` | The box/host is the blocker, not the code. |
| `SUPERSEDED` | Duplicate/fold rows (TR-225→TR-232, TR-226→TR-233) cannot fold to `complete` (evidence-replay refuses a duplicate's run) and `superseded` is off the status vocabulary — so the fold lives in `decline.code` with `routed_to` pointing at the successor. |
| `UNCHECKABLE` | No available mechanism can verify the row either way; that is a real disposition, distinct from "not checked yet". |
| `OTHER_REPO` | The work lives in a repository this board does not track (the gateway, the harness scripts); `routed_to` names where. |

Vocabulary is a closed set in `scripts/decline_contract.py`; adding a code is a
one-line change to that tuple plus this file.

### Where it is enforced

- **`scripts/decline_contract.py`** — stdlib-only validator.
  `--validate [--board PATH]` scans a board and exits 1 naming every violating
  row id and offending field/code; exit 2 is reserved for internal errors
  (unreadable board, bad JSON line — a corrupt row must never silently escape
  the check), mirroring `scripts/model_data_commit_gate.py`'s exit contract.
- **`.githooks/pre-commit`** (`core.hooksPath=.githooks`; `.git/hooks` is dead
  in this repo) — after the existing model-data leg, when the staged diff
  touches `.coding-hermes/board/tasks.jsonl`, the validator runs in `--staged`
  mode: it reads the **staged blob** (`git show :…`), never the working tree,
  so an unstaged fix cannot hide a staged violation. A missing/broken script
  warns instead of wedging commits; an internal error (exit 2) warns without
  blocking — the same check runs in CI (`tests/test_decline_contract.py`).
  The validator's internal `git diff --cached --name-only` fast-path keeps an
  unrelated commit's added latency at one Python spawn.
- **Escape hatch:** `ALLOW_DECLINE_GAP=1 git commit …` prints a loud warning
  naming EVERY violation and exits 0. Never silent — same doctrine as
  `ALLOW_UNUSABLE_DATA`.

### The backfill

The 8 rows that were blocked/review without a decline now carry one, grounded
in each row's own text (per-line rewrite; nothing else on the board moved, no
status changed): `TR-117` and `TR-261`/`TR-380` `DEPENDENCY_UNLANDED` (close
gated by TR-251's evidence run), `TR-225`/`TR-226` `SUPERSEDED` (content
duplicates of complete TR-232/TR-233, fold refused by evidence-replay),
`TR-294` `OWNER_DECISION` (service restart authorization), `TR-296`
`DEPENDENCY_UNLANDED` (verified work unmerged on wt/TR-296),
`TR-1000657714383498` `MISSING_EVIDENCE` (its own fleet-shaped load test does
not exist yet). The pre-change board failed `--validate` with exactly those 8
rows (the red proof); after the backfill it passes.

### Answering the owner's question

```
python3 scripts/decline_contract.py --report
```

one line per non-complete row: `<id>  <status>  <code or ->  <needs or reason,
truncated>`. `-` is legitimate only for `pending` rows; every
blocked/failed/review row names its code.

### Tests

`tests/test_decline_contract.py` (board venv): missing decline and unknown
code fail naming the row/bad code, the real board validates clean, `--report`
prints pending + blocked correctly, corrupt boards are internal errors, and
`--staged` is proven against a throwaway git repo: it judges the staged blob
(not the working tree), fast-paths when the board is untouched, and the escape
hatch names violations while exiting 0.
