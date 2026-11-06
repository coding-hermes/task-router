# INT-CI-19 — measured acceptance arms (tick task-router-ops-2026-11-06-00-02-50)

Fix commit: ecf8ca6 "fix(INT-CI-19): derive the -ops family in the probe library and author the 4
uncovered primaries" (worker ecf8ca6 via glm-5.3-flash @ zai-glm in worktree
/home/kara/proj-wt-intci19; published 564938d..ecf8ca6 to origin/main).

## Arm 1 — the red test now passes on the fixing tree

    $ cd /home/kara/proj-wt-intci19 && ~/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_tr251_boundary_and_replay.py
    ...................                                                      [100%]
    19 passed in 2.60s

Baseline on the same worktree BEFORE the fix (HEAD 564938d):

    FAILED tests/test_tr251_boundary_and_replay.py::test_live_library_covers_every_active_project
    1 failed, 17 passed in 0.84s

## Arm 2 — RED proof (implementation file reverted inside the worker's worktree)

    $ git checkout 564938d -- scripts/outcome_probe_projects.py tests/test_tr251_boundary_and_replay.py
    $ ~/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_tr251_boundary_and_replay.py \
        -k "covers_every_active_project or folds_the_ops_lane"
    E  AssertionError: probe library missing: ['bunker-ops', 'code-factory-ops', ...]
    FAILED tests/test_tr251_boundary_and_replay.py::test_live_library_covers_every_active_project
    $ git checkout HEAD -- scripts/outcome_probe_projects.py tests/test_tr251_boundary_and_replay.py
    $ git status --porcelain      # empty
    19 passed

## Arm 3 — coverage is complete (mechanism arm, not a count shortcut)

    $ ~/.hermes/venvs/board/bin/python3 scripts/outcome_probe_projects.py > primaries.txt
    $ ls outcome-probes | sed 's/\.json$//' | sort -u > have.txt
    $ comm -23 <(sort -u primaries.txt) have.txt
    (no output — every derived primary has a probe file)

Before the fix the same comparison named 11 uncovered primaries: bunker-ops, code-factory,
code-factory-ops, coding-hermes-scheduler-ops, crier-ops, fleet-ops-check, gitreins-ops,
hermes-dagger-ops, lane-cadence-check, lane-repair, task-router-ops.

The single remaining `-ops` token in the derivation output is `fleet-ops-check` — a command lane
whose id CONTAINS `-ops` and does not END with it; it now carries its own probe file.

## Arm 4 — the four new probe files pass the shipped validator (no template placeholders)

    $ ~/.hermes/venvs/board/bin/python3 validate_probes.py
    code-factory: errors=[] project=code-factory probes=1 kinds=['outcome']
    fleet-ops-check: errors=[] project=fleet-ops-check probes=1 kinds=['outcome']
    lane-cadence-check: errors=[] project=lane-cadence-check probes=1 kinds=['outcome']
    lane-repair: errors=[] project=lane-repair probes=1 kinds=['outcome']
    VALID

## Arm 5 — no out-of-scope surface touched

    $ git show --name-status ecf8ca6
    A outcome-probes/code-factory.json
    A outcome-probes/fleet-ops-check.json
    A outcome-probes/lane-cadence-check.json
    A outcome-probes/lane-repair.json
    M scripts/outcome_probe_projects.py
    M tests/test_tr251_boundary_and_replay.py

`data/tables/projects.jsonl` untouched; no `.coding-hermes/` or `.gitreins/` path in the commit.

## Arm 6 — hosted CI on the fixing head

Run 42143620609 on ecf8ca6 (`ci`, branch main). Classified separately from the local arms above:
this head is expected RED on the five TR-363-derived tests owned by board row INT-CI-20, which is
a different root cause on the same tree (see the row for the exact failing set).
