# Dogfood run 34 — 2026-10-16 — TR-251 mechanical-completeness surface (first drive)

Target: task-router HEAD `c16714d` (delta under test: `a0c7fc` + `c16714d`, both your
own 2026-10-16 commits). Angle picked because runs 1-33 covered CLI resolve, MCP, web, seed,
profiles, versioning, proxy ladder, drift, intake/ingress — none touched the TR-251
boundary-gate surface that landed today.

## What the surface promises

A lane closing a board row MUST bind evidence. Three mechanisms drive it:

1. `scripts/board_commit_boundary_gate.py` — refuses a diff (base..head, staged, or
   file-mode) that flips a row to `complete`/`done` without (a) `evidence_run_id`
   or `detail.evidence[].run_id`, (b) `evidence_none` / `witness=none:<reason>`, or
   (c) evidence satisfying the TR-249 gate's measured-number/artifact forms.
2. `scripts/evidence_replay.py` — the REPLAY pair: for each completion in the diff,
   resolve its run through the evidence-run store (a gitreins verdict dir
   `.gitreins/history/<date>/<id>/verdict.json` + command.json, a GitHub Actions run
   id, or a stated-reason null), then RE-EXECUTE the recorded command and compare
   exit + stdout digest. A stored digest alone never passes.
3. `scripts/outcome_probes.py` — the outcome-probe library: a
   `outcome-probes/<family>.json` per active project, checked/recorded via
   `check`/`check-all`/`record`, with authorship fields so dispatches can be audited
   "was the probe authored, by whom, when".

Plus pre-push wiring in `.githooks/pre-push` (second gate pass over the same push
lines, escape hatch `ALLOW_UNBOUND_COMPLETION=1` printed loudly) and CI steps in
`.github/workflows/ci.yml` under "TR-251: mechanical completeness at the CI boundary".

## Real-use legs run tonight (scratch dirs, no repo/board mutation)

### pre-push hook, real repo wiring

`git config core.hooksPath` -> `.githooks` (installed by scripts/install-hooks.sh on
this checkout; a FRESH clone does NOT have this set — the installer must be run
per-clone, that's how the design wants it). Fed fake push lines
`refs/heads/fake refs/fake refs/heads/main refs/heads/main` through
`bash .githooks/pre-push`:
- Without any escape hatch: exit 1, prints
  `pre-push: refused (1 of 1 push(es) to main had red CI or unbound completions)`
  because main's newest completed CI run today (38906393029) is failure ("Coding
  Hermes supervisor: queue task-router dependency review"). This is REAL behavior the
  repo owner can verify right now: `gh run list --repo coding-hermes/task-router
  --limit 3`.
- With `ALLOW_RED_PUSH=1`: exit 0, prints
  `OVERRIDDEN by ALLOW_RED_PUSH=1 — proceeding anyway. This should be rare and is logged.`
  Loud-override contract works. Note: this second call also runs the title-251
  boundary-gate sandwich, and with 0 flips it prints OK twice (one per leg) — that is
  the design reading okay, just noisy in output that flushes to the pusher.

### The flyer + replay ladder (head-of-queue probe, contact test)

Used file-mode (hand-rolled `tasks.jsonl` A through H as a scratch repo's board),
crafted the same three flip rows the skill's programming says a real cookie-cutter
closure goes through (no run id, a 9-digit CI id, a stated-reason null):

| row | flip shape | gate | replay |
|---|---|---|---|
| A | plain completion, no evidence | REFUSED `missing field: evidence` | FAIL `carries no evidence run at all` |
| B | `evidence_run_id` bound to a real verdict run (command.json `printf probe-ok`) | OK | replayed OK, exit + digest agree |
| C | `detail.evidence[].run_id` (SG-126 envelope) | OK | replayed OK |
| D | `witness=none:nothing measurable by design` | OK (stated null) | FAIL `stated-reason null — not replayable` |
| E | bound to run-dogfood-b whose command prints `probe-TAMPERED` but digest = sha256("probe-ok") | OK (run id present is enough at gate) | FAIL `replay stdout sha256 … != recorded … — the command no longer reproduces its record` |
| F | `evidence_run_id="000000001234"` (pure shape, 9 digits) | OK | **RESOLVED as kind=ci** |
| G | `evidence_run_id="none:the tool cannot measure this"` | OK (stated null via run id prefix path) | FAIL `stated-reason null — not replayable` |
| H | `evidence_run_id="run-nowhere"` (no store entry) | OK (run id present is enough at gate) | FAIL `does not resolve to any store` |

The acceptance proof `docs/evidence/tr251-mechanical-completeness-2026-10-16.json`
(criterion 5) showed exactly this matrix for A/B/C/D/E/G (and B's anti-tamper twin).
Tonight added F and H, which the committed proof did NOT probe. F is the headline
finding — see Finding 339-01 below.

Also tonight: `evidence_replay.py` has NO `--old-file/--new-file` mode (the base gate
does) — first invocation with the same flag-shape I'd used on the gate exits 2
`unrecognized arguments`. A lane wanting to fence one row through the flyer's exact
file-mode path cannot; HEAD/worktree mode needs a temp git repo to exist. (Found
because a real consumer tries the symmetric flag first — that's a good sign for the
API it *does* share, `--base/--head/--repo`.)

### The real store, populated by the REAL gitreins lane (the adopted leg)

`.gitreins/history/2026-10-16/fe0601db/verdict.json` is the only run written today
(the gitreins judge for TR-251 itself, at commit d0db679). It has task_id, commit,
passed=false, `items[]` — but NO `command.json`. Verdicts in this repo are
per-task-directory-with-sha-embedded, so the store-decision doc's promise of a
replayable store isn't yet true for real verdicts: 223 runs across
`.gitreins/history/2026-10-1[4-6]/`, of which 0 carry command.json. Replay-fail on
those rows is the honest error and the error message is actionable
(`a stored digest alone never passes; record the command next to the verdict`) — but
no real verdict in the repo has ever had a command recorded. Finding 339-02 (store
writers) is the mechanism that closes this; until it lands, the CI replay step is
effectively blank for verdict-form evidence (see 339-03).

### The verdict-vs-proof gap

`fe0601db/verdict.json` says `passed: false` at commit d0db679. The two commits that
landed the criteria (a0c7fc, c16714d) landed AFTER it, at commit 73a7367-and-later.
Nothing re-runs the judge at the fix commit — no new verdict was appended, and the
board still shows TR-251 with `status: pending` and no evidence_run_id (a correct
non-flip). The completion story currently rests on the hand-written
`docs/evidence/tr251-…json` + evidence_replay.py itself, not the store. Finding
339-04 is the follow-up the row's own author should close: bind an evidence run (for
example a fresh gitreins run at HEAD, or a completed CI run id) or the row stays
pending by its own law.

### Outcome-probe library, the real check

`outcome_probes.py check-all` = **RC 1 on the shipped product**. All 122
`outcome-probes/*.json` placeholders from `ensure` (file exists, statement still
`TODO(author): state the outcome this project's next dispatch must show`). Coverage
leg says `122/122 active projects carry a probe file`; every file is the
author-action-needed template seeded in a0c7fc. That is the same gap the accepted
proof's criterion 4 footnote called "(library complete; per-project statement
authoring is the coordinator's ongoing dispatch-time duty by design)" — but no
boundary runs check-all, so the 122 TODOs only surface if someone hands-runs
`check-all`. Finding 339-05 (wiring check-all at a boundary).

Fresh-install leg: the TR-251 tools are present in the fresh clone (script files +
launch help rc=0 in a venv on bunker 44c65b6f, agent destroyed cleanly after).
`outcome_probes.py check-all` RC=1 on the fresh clone too (same 122 placeholders) —
consistent, no install drift. The repo's own smoke-tool leg (pytest on the fresh
clone, `outcome_probes.py check-all` as a pass/fail smoke) is not part of the README
anywhere today.

## Integration notes for someone wiring this next

- Two different evidence plumbing share one contract: the flyer's `--old-file /
  --new-file`, `--base / --head`, `--repo` and the replay's `--base / --head /
  --repo`. Keep them aligned mentally: gate = judgment-only; replay = judgment +
  RE-EXECUTION. If you enrich defense from `board_complete_gate.evidence_state`,
  read `scripts/board_complete_gate.py` lines 103-140 — the same
  measured-number/artifact-path/witness forms that mechanism 1 reuses at the
  boardctl write gate.
- For a verdict-form evidence run to actually replay tonight, the
  `.gitreins/history/<date>/<run>/` dir must carry BOTH `verdict.json` AND
  `command.json` (recorded command, `expect_exit`, optional `stdout_digest`). No tool
  in this repo yet writes command.json — awaiting the store-writer mechanism.
- To prove your own flyer flip later: `python3 -m venv .venv && .venv/bin/pip
  install -e . duckdb && .venv/bin/router seed && .venv/bin/router spawn my-project
  --format json` (fresh clone, 4.5 MB registry, ~14s warm on the bunker).

## What I did NOT change

- No source, board, doctrine or hook file was touched. All legs ran against scratch
  (`/tmp/tr251-sc*`, temp git repos, bunker agent home) plus read-only walks of the
  real repo. `git status` after cleanup: only `M .gitreins/tasks.yaml` — the pre-existing
  dirty state this lane arrived on.
- No repo visibility/permissions/clone-settings modification anywhere.
- No credentials minted or rotated. Bunker `cp` + `ssh -i` used the agent's own key.

## Verdict

🟡 **PROMISING-BUT-ROUGH** on the TR-251 surface specifically (the repo's overall
row state carries SHIPPABLE from runs 31/32/33 — this run is a verdict about one
freshly-landed mechanism, not a downgrade of the project). One end-to-end honest
refusal/refusal-to-accept/acceptance/replay chain worked on story-legs A through E
(the flyer + replay matrix landing exactly the catch TR-251 exists for, including
REPLAY-CATCH of a tampered digest, exit+digest passthrough and one file-mode gap
between the two scripts). The 339-01 CI-id shape hole in resolve_run, 339-02
zero-command.json store reality, 339-03 CI unskippable-doomed step and 339-05
boundary-unchecked probe library are what stand between "tests green" and "the gate
delivers what its name says on real pushes".

Rows filed: TR-339 (umbrella), with 339-01..05 as its sub-findings; install leg /
boundary-fit / perf live here, not on the board (no code AND no board mutation from
this lane; the row carries direction, not edits).

ch:trace row=TR-339 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_tr251_boundary_and_replay.py evidence=docs/dogfood/2026-10-16-run34-tr251-boundary-gates.md
