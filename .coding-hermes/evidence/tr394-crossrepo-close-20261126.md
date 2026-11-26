# TR-394 cross-repo closeout evidence (2026-11-26)

TR-394 (task-router board, P0, filed 2026-11-07: "Foreman spin: tasks-admitted lanes
bypass cooldown and a no-op tick costs nothing (69 spawns/day, 62 dry)") was
cross-repo routed on 2026-11-23 (tick task-router-foreman-2026-11-23-03-13-09) to
the owning lane coding-hermes-scheduler as **SCHED-GAP-17040** (P0). That row is
now **complete**. This artifact records the verification performed by the
task-router foreman tick task-router-foreman-2026-11-26-12-09-11 before closing
TR-394 here. All scheduler-repo facts were re-read from the scheduler repo this
tick, not copied from memory.

## Owning-lane closeout (repo ~/coding-hermes-scheduler/coding-herms-scheduler)

- Board row SCHED-GAP-17040: status **complete**, P0, updated
  2026-11-26T04:55:42Z; row commit_hash = graded tip **bf096cf8**.
- Close commit: **fb3c9c70** "board(scheduler): close SCHED-GAP-17040
  evidence-only — noop-pacing arm already landed (79510add/76264ed9), tier2 PASS".
- Product arm (landed 2026-11-06/07, 17 days BEFORE the gap row was filed —
  TR-394's 11-06 fleet measurement was of the PRE-fix state):
  - **79510add** "scheduler: pace tasks-admitted lanes on their no-op streak"
  - **76264ed9** "feat(scheduler): dry-run throttle — warn at five, suspend at ten"

## Acceptance-criteria evidence (all five TR-394 ACs)

| AC | Evidence |
|----|----------|
| 1. no-op/dry tick on a tasks-admitted lane defers next spawn by >= cooldown_floor_s | noop-pacing arm 79510add (no-op streak pacing) + 76264ed9 (warn at five, suspend at ten) |
| 2. consecutive no-ops escalate | the 76264ed9 throttle ladder (warn → suspend) + live `no_progress_ticks=2` recorded (stayed 0 pre-fix) |
| 3. Go test that FAILS on current main and PASSES with the fix, named in the commit | RED arm: the noop_pacing tests copied onto parent 0b357bb9 fail to build (`undefined tasksNoopCooldown`); GREEN: focused tests ok 0.126s; internal/scheduler full ok 190.3s; full-repo `go test ./...` 28/28 ok, 0 FAIL (solo) |
| 4. proven on the live lane: >=2h gap with zero work against the measured 2-3 min cadence | schedulerd binary rebuilt 2026-11-25 10:31 from main includes the arm; task-router-foreman spawns 69/day pre-fix → **4 spawns since 11-24**; a dry tick at 19:04:49 -05 was **not respawned for 4h20m+**; `noop_guard` events live. Load-gate confounder named in the close; the streak+pacing evidence is the non-confounded signal |
| 5. no restart of schedulerd without owner permission | no restart or rebuild performed by this lane; the rebuild is the scheduler lane's own disclosed action (binary rebuilt 11-25 10:31 from main) |

## Judge verdicts on the owning repo

- **4aa5c586: PASS** (task SCHED-GAP-17040, graded at bf096cf8, evaluated
  2026-11-26T05:03Z) — the passing verdict bound to the close.
- 470ae98e: FAIL (evaluated 04:37Z) — attributed in the close row: its
  in-sandbox `go test ./...` FAIL ran during the load-36 spike (disproven solo:
  28/28 ok); its missing evidence classes are historical/live-host facts a
  tree-graded judge cannot observe. Tier-1 PASS on both runs.

## Close action on this board

TR-394 closes as routed-complete: the owning lane's evidence satisfies every AC;
no product commit exists in this repo (the fix is schedulerd Go). The row binds
the hosted CI run that grades this repo's tip carrying this artifact.
