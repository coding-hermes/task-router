TR-367 — Board-task intake daemon restore — AC evidence (2026-11-25, tick task-router-foreman-2026-11-25-07-30-27)

Row: TR-367 P1 "Board-task intake daemon is dead: tasks are no longer auto-resolved on submission"
Worker: glm-5.3-flash @ ollama-cloud (R1 commit d94bfc67, worktree /home/kara/worktrees/task-router-TR-367, branch wt/TR-367)
Rework: glm-5.3-flash @ ollama-cloud (R2 commit 98058b82, branch wt/TR-367-R2)
Merged: 69d52f43 (R1 supervision wiring) and 0c208953 (R2 cold-start seed on every start) to main; both pushed, origin/main parity 0.

PREMISE (measured before dispatch)
- data/state/intake-resolves.jsonl frozen at 48 records since 2026-10-13T09:10:37Z; no board_task_intake.py process on the host; committed unit absent (no supervisor of any kind).

LIVELINESS INCIDENT (foreman-verified live, disclosed)
- R1 arm resolved 9 pre-existing rows in 95s (ledger 48->57: TR-117 board-created 2026-09-23, DOC-5..12, TR-1000976681059243) — cold-start seeding only ran on an EMPTY seen file while the first poll read the whole board from offset 0. Foreman stopped + disabled the service and restored the ledger to exactly the 48 frozen records (backup /tmp/intake-resolves-20261125-pretrim.jsonl). R2 seeds every pre-existing pending id as seen on EVERY daemon start; regression test test_daemon_start_with_stale_seen_never_mass_resolves RED-proven on the pre-R2 tree.

AC VERIFICATION (at merged main 0c208953, re-run independently by the foreman)
- pytest tests/test_board_task_intake.py -> 27 passed (24 R1 + 3 new: stale-seen RED, appended-row-after-stale-seen, fresh-state daemon path). Gate re-verified in the worktree AND on merged main.
- Sandbox (re-run by foreman, two-plane exports per TR-319): cold start with stale seen over the 598-row live board copy -> ledger delta 0 (499 ids seeded as seen); appended row TR-367-R2-AC3 -> exactly one record with chain_head xkiro/nemotron-3-nano-omni $0.056/M + complexity_source jev; second start delta 0; live ledger untouched.
- Contract proof LIVE on this host: daemon re-armed 2026-11-25T02:44:42-05. Cold start delta 0 pre-existing resolves (the one new record is the documented board-intake-recovery of the dangling claim the killed R1 daemon left, id QA-TASKROUTER-4, completed with the error contract). 549 ids seeded as seen; resolves ledger flat at 49 across two subsequent intervals; service active/enabled (Restart=always), MainPID 3886904-class under systemd --user; pgrep proof: supervised python3 process running scripts/board_task_intake.py against the canonical board.
- Artifacts committed: scripts/task-router-intake.service (systemd --user unit, arm step documented in header), scripts/intake_supervise.py (check/arm/status), docs/board-task-intake.md supervision section, .gitignore .sandbox/ rule.
- GitReins: task TR-367 created 2026-11-25, started, completed this tick -> verdict in .gitreins/history (see board row verdict field).
- CI: see board row ci fields for the merged main run.
