# task-router TR-251 — the mechanical-completeness surface

## What it is
Run by 3 files (all stdlib-only):
- `scripts/board_commit_boundary_gate.py` — boundary gate: refuses a diff where
  a row flips to complete without evidence. Modes: default=staged; real usage wants
  `--base A --head B` (CI) or `--old-file F --new-file F2` (single-file probes).
- `scripts/evidence_replay.py` — the re-run leg of the same idea. Requires a real run id
  that resolves to `.gitreins/history/<date>/<id>/verdict.json` AND
  `command.json` (recorded argv, expect_exit, optional stdout_digest).
  It's `--base/--head/--repo` only — no single-file mode.
- `scripts/outcome_probes.py` —ensure/check/check-all/record over
  `outcome-probes/<family>.json` authorship-shaped template files, one per active project.

## The correct flow (when you want to prove a lane's closure for real)
1. Get the verdict files for the row's completion (a real run). The best source
   today is a gitreins verdict, but it must ALSO have `command.json` beside it or
   replay refuses. Until the store-writer mechanism lands, capture the command by hand.
2. Point the model at the truth: `evidence_replay.py --base <prev-main> --head HEAD`.
   It compiles one summary line per flipped row, walks evidence, and grades by
   actually re-running what the record says.
3. A row lacking `evidence_run_id` + `evidence_none`/`witness=none:<reason>` +
   artifact/number evidence CANNOT close under your own law — don't fight the
   gate, bind the run or state the null.

## Pitfalls
- resolve_run accepts ANY 8-12 digit id as "CI run" — pure RegExp on the id, no gh. A
  9-digit name crosses "resolved" without meaning anything. Treat CI-sourced
  evidence runs as binding only if they came from a `gh run view` call or the store
  proves otherwise.
- The base gate's file mode (`--old-file/--new-file`) has no twin on
  evidence_replay.py; a symmetric consumer gets 2 usage errors on replay.
- .githooks/pre-push is bash — call it with `bash` (a `python3` invocation gives a
  sea of SyntaxError noise, not verdicts).
- `router seed` needs duckdb (PEP 668 bare Debian blocks `pip install` without a
  venv). The clean path is `python3 -m venv .venv && .venv/bin/pip install -e .
  duckdb && .venv/bin/router seed`.
- outcome_probes ensure creates files check-all REFUSES until the author statement
  is really written (this is the designed state, not a bug) — but nothing calls
  check-all at a boundary so a whole library of placeholders can persist silently.
- `scripts/board_complete_gate.evidence_state` is the shared evidence-lens both
  gates reuse — enrich that if you enrich evidence forms.

## Smoke one-liner
board gate myself on the current tree:
```
python3 scripts/board_commit_boundary_gate.py          # uses staged/HEAD
```
Assuming no flips are staged it exits 0; a craftable flip must look like the
TR-011 shape (4 criteria + guard PASS, no run id) ONLY when it's a real flip.
