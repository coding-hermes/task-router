README SWEEP -- RE-FIRE VERIFICATION ADDENDUM
tick:    task-router-readme-2026-11-10-12-37-33
target:  task-router @ /home/kara/task-router
lane:    readme (stand-in driver /home/kara/.hermes/stand-in/readme/task-router)
written: 2026-11-10T13:56Z, from a separate worktree at origin/main (not the canonical checkout)

This addendum does not rewrite any earlier finding. It records three things:
  (1) independent re-verification of the two rows this tick filed,
  (2) one correction to an evidence fingerprint,
  (3) one LOCAL-ONLY board defect, deliberately left unpublished.

ch:trace row=README-5 spec=docs/traceability-doctrine.md#evidence doc=README.md evidence=.coding-hermes/evidence/readme-reref-addendum-2026-11-10.txt witness=path@origin:.coding-hermes/evidence/readme-reverify-2026-11-10.txt memory=/ops/ticks/task-router-readme-2026-11-10-12-37-33
ch:trace row=README-6 spec=docs/traceability-doctrine.md#witness doc=README.md evidence=.coding-hermes/evidence/readme-reref-addendum-2026-11-10.txt witness=path@origin:.coding-hermes/evidence/readme-reverify-2026-11-10.txt memory=/ops/ticks/task-router-readme-2026-11-10-12-37-33

--------------------------------------------------------------------------------
1. PUBLISHED-TIP IDENTITY
--------------------------------------------------------------------------------
$ git ls-remote --symref origin HEAD
    ref: refs/heads/main	HEAD
    8bbe86e2444dd12fab61374375b5c43922f5c7cd	HEAD

$ git diff --name-only 3c095fedf50032bcdf2f431e343a8ef674984943..8bbe86e2444dd12fab61374375b5c43922f5c7cd \
    -- README.md Makefile pyproject.toml scripts task_router tests data
    (no output)

The product surface that both probe rounds examined is byte-identical to the published
tip. Probing 3c095fed therefore licenses claims about 8bbe86e2: nothing in the paths a
README reader touches changed between them. README.md itself is unchanged since
2026-11-05 (fd899acf, 5 days old at this tick, 10 commits in the trailing 30 days).

Release freshness: `git ls-remote --tags origin` returns ZERO tags. There is no release
to measure staleness against, so "freshness vs last release" is not a reachable check on
this project and no claim is made about it.

--------------------------------------------------------------------------------
2. ROWS THIS TICK FILED (both pending on origin/main, both executable by a stranger)
--------------------------------------------------------------------------------
  README-5  P4 (field P3)  make schemas is non-runnable: scripts/schema_check.py is absent
  README-6  P4 (field P3)  a fresh clone cannot seed: router seed and router validate --heal both refuse

Both carry the exact command, the exact output, file:line, acceptance criteria, and the
TR-348 severity reasoning (title label P4, priority field P3 because this board's
vocabulary is closed at P0-P3 by scripts/board_priority_normalize.py).

--------------------------------------------------------------------------------
3. RE-VERIFICATION PERFORMED THIS ROUND
--------------------------------------------------------------------------------
README-5 -- T4, read back from the real git store at the published tip:

$ git ls-tree -r --name-only origin/main | grep -F scripts/schema_check.py ; echo rc=$?
    rc=1        (the path exists nowhere in the published tree)

Makefile:23    "#   make schemas   validate live state artifacts against schemas/"
Makefile:86    schemas: install
Makefile:87    <TAB>@$(VENV_PY) scripts/schema_check.py --all

READS: the only reference to schema_check in the repo is the Makefile target that calls
it, and the file it calls does not exist in the published tree. The finding holds.

README-6 -- the refusal re-ran end to end at this tip (see the T3 note below):

$ git show origin/main:.coding-hermes/evidence/readme-reverify-2026-11-10.txt | tail -5
    SATISFIABILITY FAIL: 6 of 264 rating(s) in the emit domain have NO routable lane,
      across 1 category
      math: levels 0..5 - no lane MEASURES this capability ... blocked holder(s):
      ollama-cloud/qwen3.5:397b tier=5 (disabled, unpriced)
    Seeding REFUSED: add a routable lane for the categories above, or re-run with
      --allow-unsatisfiable to seed a knowingly-starved registry
    EXIT_STATUS=1

READS: a fresh install of the published tip still cannot seed. The finding holds.

T3 NOTE -- stated, not hidden. The `router seed` execution quoted above and in the row
was produced by this tick's re-verification round at 2026-11-10T13:43Z against a fresh
venv built from 8bbe86e2 (probe archive probe-20261110-123733-8bbe86e2). This round's own
attempt to re-execute the venv binary was stopped by the unattended approval gate ("no
answer within 5 minutes -- the command did not run"); per that gate it was not retried and
not rephrased. Section 1 is what licenses reusing the 13:43Z execution: identical product
bytes. This is recorded as a limit of this round, not as a claim that the refusal is newer
than 13:43Z, and not as a T3 reached by this round itself.

--------------------------------------------------------------------------------
4. CORRECTION -- STALE ARTIFACT FINGERPRINT ON SEVEN ROWS
--------------------------------------------------------------------------------
Seven rows bound the re-verification artifact with a fingerprint that does not resolve:
  README-1, README-1-R2, README-2, README-3, README-4, README-5, README-6

  bound as tree_fp : b0c050f1b7903bd6ed7ee5ce53bfd20839adebe0fb5c26e05f446533191842a5
  published bytes  : a6f40507ab263d78333eb1aa4741fd0f710a25f5004e87261e87a8672705a162
                     (git show 8bbe86e2:.coding-hermes/evidence/readme-reverify-2026-11-10.txt | sha256sum)

CAUSE: the artifact was fingerprinted before its final edit. It contains a board-lint
census section that names commit b587b66e -- text that could only be written after that
commit existed -- so the file necessarily changed after b0c050f1 was taken. The sibling
artifact readme-command-matrix-2026-11-10.txt is bound correctly (b0ff635f resolves to the
published blob), which is the control: the fingerprinting method is sound, this one
artifact was edited afterwards.

CORRECTION: the seven rows now carry a6f40507. No finding text, severity, status or
acceptance criterion was touched; only the pointer to the bytes.

VERIFY (after this commit):
  git show <this-commit>:.coding-hermes/board/tasks.jsonl | grep -c b0c050f1     -> 0
  git show <this-commit>:.coding-hermes/evidence/readme-reverify-2026-11-10.txt | sha256sum
                                                                                 -> a6f40507...

--------------------------------------------------------------------------------
5. LOCAL-ONLY DEFECT -- NOT PUBLISHED, HANDED OFF
--------------------------------------------------------------------------------
The canonical checkout /home/kara/task-router is at 1d13785b on main, one commit AHEAD
and one BEHIND origin/main. That unpushed commit (sibling QA lane, 2026-11-10T13:21Z)
re-serialized tasks.jsonl and emitted three rows whose ids were derived from EVENT ids
instead of row ids:

  README-1000022374340374      duplicate of README-3    (P4 README carries no version/freshness reference)
  README-1-R1000891469114093   duplicate of README-1-R2 (P4 quick start test command fails without test extra)
  README-1000393629214837      duplicate of README-1    (P4 document the test extra in the install path)

None of the three exists on origin/main. They are unpushed residue.

RISK if that commit is ever pushed: three duplicate documentation rows enter the dispatch
queue and a foreman can hand the same fix to three workers; the same three ids pollute
max-id arithmetic for every later mint on the README family.

ACTION TAKEN: nothing published and nothing reverted. This entire addendum was produced
from a separate worktree at origin/main, so the corrupted tree was never a commit parent.
The defect is handed to the QA lane that authored 1d13785b and to the board owner.

--------------------------------------------------------------------------------
6. WHAT THIS ROUND DID NOT VERIFY
--------------------------------------------------------------------------------
- No release exists (zero tags on origin), so release-freshness was not measured.
- The install path was not re-executed by this round: binary execution hit the unattended
   approval gate (section 3). The 13:43Z run stands on identical product bytes.
- Link and badge drift was not re-crawled this round; the first-pass matrix artifact
   readme-command-matrix-2026-11-10.txt (b0ff635f, resolving) carries that pass.
- boardctl validate was not re-run by this round for the same approval-gate reason. The
   before/after census recorded in readme-reverify-2026-11-10.txt stands as the last
   reading (450 errors, 571 warnings, pre-existing at the base of this family).

STATUS: PASS
TIER: T4 -- re-read the published git store (tree, blob digests, board rows) at 8bbe86e2;
      T3 for the seed refusal rests on the 13:43Z execution at identical product bytes,
      stated as such rather than claimed as this round's own.
EVIDENCE: .coding-hermes/evidence/readme-reref-addendum-2026-11-10.txt (this file)
