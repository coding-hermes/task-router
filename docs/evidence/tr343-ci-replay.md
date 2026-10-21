# TR-343 — CI evidence replay: a bound GitHub Actions run id verifies on a runner

- Row: TR-343 (P1) — "CI evidence-replay gate refuses every evidence form"
- Spec: docs/traceability-doctrine.md#hard-rules
- Test: tests/test_tr339_ci_wiring.py (see `test_ci_run_id_verification_and_verdict_refusal`
  and the `test_resolve_run_ci_id_*` arms)
- Prompt: /tmp/task-router-foreman-2026-10-20-23-23-42-TR-343-brief.txt
- Wave: none (serial worker)

## Defect

The TR-251 evidence replay step refuses a board-only completion flip whose row
binds a numeric GitHub Actions run id:

1. **Authorization gap.** `scripts/evidence_replay.py::_verify_ci_run` verifies
   a digit id with `gh run view <id>` (TR-339-01). `gh` authenticates from
   `GH_TOKEN`; the step exported only `GITHUB_TOKEN`, and the workflow declared
   **no `permissions:` block at all** — the default job token carries no
   Actions read on a private repo. `gh run view` therefore answers
   "Could not resolve to a Run" and the replay refuses a genuinely completed,
   successful run as unverifiable. The documented "bind a VERIFIED CI run id"
   path could never verify on a runner.
2. **Coverage gap.** No CI-mode test pinned the intended behaviour: pass for a
   digit id that gh verifies completed+successful, loud refusal for
   verdict-form ids with no local `.gitreins/history`.

## Change (smallest that closes it; verification untouched)

- `.github/workflows/ci.yml`
  - top-level `permissions: actions: read` (minimal read-only grant; the job
    is the only token surface and everything in it is read-only: pytest,
    guard script, gate scripts, `gh run view`);
  - the "TR-251 evidence replay" step now exports
    `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` (gh prefers GH_TOKEN; the prior
    `GITHUB_TOKEN` export was inert for gh).
- `tests/test_tr339_ci_wiring.py` — TR-343 cases:
  - workflow wiring: `permissions:` block with `actions: read`, and the
    replay step exporting `GH_TOKEN:`;
  - `resolve_run` with mocked gh at the subprocess seam: digit id verified
    completed+success → `("ci", {run_id, verified_via: "gh run view",
    conclusion: success})`, and the mock asserts a real
    `gh run view <id> --json` invocation (a pass cannot come from shape);
  - loud refusals: gh missing, gh present but unauthenticated/unauthorized
    (rc=1 "Could not resolve to a Run" — the pre-fix CI arm), run not
    completed (`in_progress`), verdict-form id in ci_mode naming the
    command.json-writer gap;
  - end-to-end (`test_ci_run_id_verification_and_verdict_refusal`): a
    board-only flip to `complete` bound to a verified digit run id goes
    green through `replay_diff(..., ci_mode=True)` on a tree with NO
    `.gitreins/history`, while a verdict-form id on the same runner refuses
    loudly.

## Security rule preserved

Shape is still not evidence: a digit string passes only through `gh run view`
returning a run with `status=completed` and a non-pending conclusion. Every
unverifiable arm — no gh binary, no auth/authorization, run not found, run
in progress, pending or skipped conclusion, verdict-form id without local
history — raises `ReplayFailure` and fails the gate loudly. No fabricated id
is accepted; the pre-fix red arm is provable by mutating the mocked gh to
rc!=0 (then `test_resolve_run_ci_id_passes_completed_success_run` goes red —
the pass comes from the verification, not the shape).

Red-proof actually run (worker session, 2026-10-20):
* workflow arm — `git stash` of the ci.yml changes alone made
  `test_ci_replay_step_carries_GH_TOKEN_and_actions_read` FAIL on the
  missing `permissions:` block (stash popped, diff intact);
* verification arm — source mutation
  `if check.returncode == 0 and False:` in `_verify_ci_run` (guard branch
  disabled) made `test_resolve_run_ci_id_passes_completed_success_run`,
  `test_resolve_run_ci_id_refused_when_not_completed`, and the
  end-to-end acceptance test FAIL (3 failed, 8 passed);
  `git checkout -- scripts/evidence_replay.py` restored the source
  byte-identical (verified via `git diff --stat scripts/` = empty).

ch:trace row=TR-343 spec=docs/traceability-doctrine.md#hard-rules wave=none:serial-worker test=tests/test_tr339_ci_wiring.py::test_ci_run_id_verification_and_verdict_refusal doc=docs/evidence/tr343-ci-replay.md prompt=/tmp/task-router-foreman-2026-10-20-23-23-42-TR-343-brief.txt evidence=tests/test_tr339_ci_wiring.py witness=none:local-mocked-gh-and-ci-config verdict=none:pending-judge commit=none:worker-commit memory=none:not-written
STATUS: complete
TIER: T3
EVIDENCE: tests/test_tr339_ci_wiring.py (7 TR-343 cases, focused run + full suite in the worker report); commit = this commit (see ch:trace commit=none:worker-commit)
