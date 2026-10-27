# TR-348 — docs-audit writer priority contract: evidence (2026-10-26)

Row: `TR-348` — "[P1] CI red on main: board priority vocabulary regression — 8 DOC
rows carry off-vocabulary P4, blocking pushes". This file is the AC3 evidence
artifact: the writer contract, the measured current state, and the exact runs.

## Source reality (verified before any edit)

* The row **writer** for task-router docs-audit findings is the fleet docs-audit
  lane. Its doctrine lives in the filing skill
  (`~/.hermes/skills/coding-hermes-docs/SKILL.md`: "severity: P4 for a
  documentation gap …; P5 for polish. NEVER P1-P3") — **outside this repo**, not
  editable here. Rows land through `boardctl` (each DOC row carries a
  `task_created` event in `events.jsonl`; actor `foreman`). The detection
  helpers under `~/.hermes/stand-in/docs/task-router/` (audit.py,
  audit_surface.py, docrows.py) do not set priorities.
* The 8 offending rows (DOC-5, DOC-6, DOC-7, DOC-13, DOC-14, DOC-15, DOC-16,
  DOC-17) were already normalized on main by `c45ee4f` (2026-10-25,
  "board: normalize 8 DOC rows to canonical priority P3"): field `P4 -> P3`,
  titles untouched, so the original "P4 —" wording stays legible (AC1 + AC2
  landed before this worktree branched).
* `boardctl` normalizes case and bare digits on write (BT-048, sibling repo)
  and passes `P4`/`P5` through untouched — the exact hole the emission fell
  through.

## What landed (AC3)

1. `scripts/board_priority_normalize.py` — the canonicalization ADAPTER, the
   form a writer can adopt repo-side:
   - docs severities map `P4 -> P3` and `P5 -> P3` (P3 = lowest canonical
     tier: a doc row stays visible, never outranks functional work — the
     repository doctrine for documentation findings);
   - the ONLY mutations are the `priority` value and a `notes` provenance
     marker (`TR-348 priority canonicalization: docs severity P4 -> board P3
     (vocabulary P0-P3)`), mirroring the "TR-088 priority migration: was N"
     convention so the original label stays machine-checkable;
   - titles are never touched (the "P4 —" wording is AC2's legibility
     evidence); canonical values and absent keys are never touched; any other
     off-vocabulary value is NEVER guessed — the boundary refuses it;
   - `--apply` rewrites ONLY target lines (line-wise, `json.dumps` per row),
     asserts the line count is unchanged, and re-verifies the whole file
     against the census classifier or fails loudly; dry run exits 3, no-op
     `--apply` exits 2.
2. `scripts/board_commit_boundary_gate.py` — TR-348 arm on the TR-251
   commit-boundary gate (already wired at BOTH `.githooks/pre-push` and CI's
   "TR-251 boundary gate" step): a diff whose NEW last-state carries an
   off-vocabulary priority (census classes `off-vocabulary` or
   `corrupt-fragment`) that the OLD side did not already carry for that id is
   REFUSED, naming the row and the mapping. A value already on the old side is
   history, not a new emission — re-appends pass, and the c45ee4f
   normalization shape (old P4 -> new P3) remains landable.
3. `tests/test_tr348_writer_priority_contract.py` — 27 deterministic tests on
   tmp_path boards (the real board is only read): the filed DOC-15 shape
   canonicalizes with the title byte-identical; the gate refuses the exact
   incident shape and passes the shipped normalization diff; P5/digits/prose/
   corrupt-fragment classes; CLI end-to-end on both scripts; the 8 shipped
   rows re-pinned for P3 + "P4 —" title legibility.

## Measured on this tree (2026-10-26)

```
board histogram (375 rows): <absent> 1, P0 40, P1 173, P2 116, P3 45
value classes: canonical=374, absent=1 — off-vocabulary=0
focused: pytest -q tests/test_board_priority_vocabulary.py \
              tests/test_board_priority_census.py \
              tests/test_tr348_writer_priority_contract.py   # 52 passed
red-proof: gate --old-file (empty) --new-file {id:RED-1,priority:P4}
           -> rc=1, refusal names RED-1, P4, and the P4/P5 -> P3 mapping
live-board dry run: board_priority_normalize.py -> "nothing to canonicalize"
```

## Residuals

1. The filing-side doctrine (P4/P5) itself lives in the docs-audit skill and
   is not editable from this repo; the adapter + boundary make an emission
   unable to land rather than unable to be attempted. If the skill's doctrine
   is later amended to file at P3 directly, this adapter's map already
   accepts canonical values unchanged.
2. `boardctl` still treats priority as warn-only (`--fail-on` has no priority
   class) — sibling-repo work, named in
   `docs/priority-vocabulary-review-tr-002.md` and unchanged here.
