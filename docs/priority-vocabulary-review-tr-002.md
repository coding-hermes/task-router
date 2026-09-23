# Priority vocabulary — REVIEW-TR-002 closeout (2026-09-23)

Row: `REVIEW-TR-002` — "[P2] Two priority vocabularies on one board (28 rows numeric vs
90 P-prefixed) and the validator never checks priority". Filed from fleet project
review #17 (`~/project-reviews/review-17-task-router-2026-09-22.html`, finding R17-03),
which measured this board at **118 rows**: `P0` 10, `P1` 37, `P2` 38, `P3` 5 against
**28 bare numbers** (`3` x16, `2` x9, `1` x3).

## Verdict: both halves were already closed — the durable gap is the FLEET

Measured on the branch base (`5bbbb93`) **before any change was made**:

| Claim in the row | Live measurement | Result |
|---|---|---|
| 28 numeric priorities on this board | histogram `P0` 10, `P1` 48, `P2` 54, `P3` 23 — zero off-vocabulary | **stale premise** |
| "the validator never checks priority" | `boardctl validate` warns on a stray priority (BT-048) | **stale premise** |

Both were fixed after the review date and before the row was picked up:

* **The 28 rows** were migrated by `b0e504c` — *"board: close items 86-90 — priority
  vocabulary, freeform results, review rows"* (2026-09-22 19:37, item 88). Every migrated
  row kept its old value in `notes` as `"TR-088 priority migration: was N"`, which is what
  makes the mapping checkable after the fact instead of only assertable.
* **The validator check** is `BT-048` in `coding-hermes-boardctl`
  (`53a79cc` — *"validate warns on out-of-vocabulary priority values found on disk"*),
  with its own `validate_priority_test.go`.

So this row is not a fix; it is a **PIN plus a census**. Loading the row's own wording
("28 rows numeric (1/2/3)") as an instruction would have produced a commit that "migrated"
rows which were already canonical.

### The mapping: identity, not the row's guess

The row's description guessed `1→P2, 2→P1, 3→P0`. The mapping actually applied — and the
one `board.NormalizePriority` implements — is the **identity** map:

```
1 → P1      2 → P2      3 → P3
```

That is re-derived from the board itself in
`tests/test_board_priority_vocabulary.py::test_migration_notes_agree_with_the_priority_that_landed`
(28/28 notes agree), not taken on trust from either document. The row's guess would have
also silently *inverted* the priority order (`P0` is highest, so `1→P2` demotes every row
it touched).

## What actually landed

| File | What it does |
|---|---|
| `tests/test_board_priority_vocabulary.py` | Pins this board to one vocabulary (AC1) and re-derives the migration mapping from the board's own `notes`. Includes a RED control that injects a bare `3` and shows the assertion fail, plus the absent-vs-off-vocabulary distinction. |
| `scripts/board_priority_census.py` | Fleet-wide census (AC3) — the artefact the row's AC3 actually needed, because a check on one board cannot answer the fleet question. |
| `tests/test_board_priority_census.py` | Tests for the census, including the live repo's own board. |

### The census: this board is the clean one

Scanning `~` on 2026-09-23 (deduped by realpath then content hash):

```
boards scanned:  111  (262 duplicate copies skipped)  [primary=72 worktree=25 archive=14]
task rows:     19566
value classes:   canonical=18688, off-vocabulary=766, corrupt-fragment=12, absent=100
verdict:         35 PRIMARY boards carry an off-vocabulary priority; this board is NOT one
```

`task-router` — the only board review #17 examined, and the one the row is about — is the
only primary board in its fleet cohort with a single vocabulary. The two largest offenders
by row count are `gitreins` (98 rows: `high` x26, `low` x48, `medium` x22, `1` x2 — a
whole prose scheme one board over) and `hermes-canopy` (52 rows: `1` x14, `2` x6, `3` x3,
`Critical` x5, `High` x11, `Medium` x6, `Low` x1, `P4` x6 — three vocabularies on one board).

Across all scanned rows the split is **canonical 18688 (95.5%) · off-vocabulary 766 ·
corrupt-fragment 12 · absent 100** — 778 offending rows, 4.0% of the corpus.

**A second, worse class the row did not know about — 12 corrupted rows.** On `consensus`,
`asce`, `warpfs`, `dexdat-memory`, `inference-estimator` and `escalation-doctrine` the
priority value is a JSON fragment, e.g. ``P0","source":"dogfood-dagger``. A column splice
swallowed the following key into the priority field, which means the row is not merely
mis-spelled — **every other key in that row is suspect too**. It is reported as
`corrupt-fragment` rather than `off-vocabulary` precisely so a cleanup pass cannot "fix" the
priority and leave a broken row behind. `wojons-mythos` carries `PP3`; `P4` appears 18 times
across 14 boards, i.e. the vocabulary in use is wider than `{P0,P1,P2,P3}` even where it is
not numeric.

### AC2 is NOT met as literally written

AC2: *"The boardctl validator flags an out-of-vocabulary priority value the way it flags a
bad status."* Measured side by side on scratch boards with the deployed `boardctl`:

```
bad status   "banana"  ->  [error] ... status "banana" not in vocabulary ...   RESULT: FAIL   exit 1
bad priority "1"       ->  [warn]  ... priority "1" is not in vocabulary ...   RESULT: OK     exit 0
bad priority "high"    ->  [warn]  ...                                         RESULT: OK     exit 0
bad priority "P4"      ->  [warn]  ...                                         RESULT: OK     exit 0
```

A bad status is an **error** (non-zero exit, blocks a gate); a bad priority is a **warning**
(zero exit, blocks nothing). The flag exists, but it is not "the way it flags a status" — and
`boardctl validate --strict-keys` / `--fail-on` can promote key drift and dangling deps but
**not** the priority class, so there is no existing way to make a stray priority gate a
commit. The census found 766 such rows across 35 primary boards; a warn-only flag is
consistent with why they are still there.

Making it gate-grade is a one-line change in the sibling repo (`coding-hermes-boardctl`: add
`priority` to the `--fail-on` allow-list, mirroring `CountDanglingDepWarns`), and it is
**not** made here — that repo is not this worktree's repo, and a census plus a pin over this
board is the part that belongs to this row. Named residual, not silently assumed.

### Standing doctrine: the census is the ranking input

Until every board is normalised, priority sorting stays scheme-dependent. `daily_review.py`
already sorts by the raw string (`sorted(planned, key=lambda r: str(r.get('priority')))`),
under which `"3"` and `"P2"` are unrelated values — exactly the failure the row described, but
fleet-wide rather than on this board. `board_priority_census.py` is the tool that measures it;
run it after a project's normalisation pass to confirm the class shrank.

## Reproduce

```bash
/home/kara/.hermes/venvs/board/bin/python3 -m pytest -q \
    tests/test_board_priority_vocabulary.py tests/test_board_priority_census.py   # 25 passed
/home/kara/.hermes/venvs/board/bin/python3 scripts/board_priority_census.py       # exit 1
/home/kara/.hermes/venvs/board/bin/python3 scripts/board_priority_census.py --json
```

## Residuals (explicit, not fixed here)

1. **AC2 literal semantics** — priority is warn-only in `boardctl`; no `--fail-on priority`
   class exists. Belongs to `coding-hermes-boardctl`.
2. **766 off-vocabulary rows across 35 primary boards** (4.0% of all scanned rows) — a fleet
   sweep, per-project, not this row. The census prints the list; `gitreins` (98 rows, a whole
   prose scheme) and `hermes-canopy` (52 rows, three schemes) are the two worth doing first.
3. **12 corrupt-fragment rows** on 6 boards — needs row reconstruction, not re-spelling;
   the splices also mean a neighbour key was lost, so a repair must recover the key that the
   value swallowed (its name is visible in the fragment on all 12).
4. **99 rows with no priority key at all** — a different class (missing field), left alone
   deliberately; flagging it would be a new error class on legacy rows.
5. **`helios` board has 2342 unparseable lines** — that board's rows are only partially
   counted here; its priority census is a floor, not a total. Its own row
   (`DF-BOARDCTL-9` class) covers the corruption.
