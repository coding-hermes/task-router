# TR-357 closeout evidence — task-router-foreman-2026-11-02-23-24-19

Row: TR-357 (P1) "A price-0 lane sits at hop 0 of every foreman chain at 92% dry, and the
outcome store records success=null so nothing can demote it"
Selected as highest-priority EXECUTABLE pending row. P0 dispositions (recorded in wave
manifest .coding-hermes/waves/task-router-foreman-2026-11-02-23-24-19.json):
- RELEASE-2026-10-22-001: owner-authorization blocked (sibling tick
  task-router-foreman-2026-11-02-14-27-04 re-verified; its receipt
  .coding-hermes/evidence/release-readiness-verify-2026-11-02-14-27-04.json preserved untracked).
- TR-355: AC5 live acceptance is an owner-controlled :9092/:9391 cutover; blocker re-probed
  this tick UNCHANGED (:9092 pid 7563 loaded 187bcae615d3 since 2026-10-19; :9391 pid 1808674
  loaded 13ef7482e1d7; both stale vs repo 44d9257). Byte-identical to the sibling 09:28
  re-probe — kept on existing fresh evidence, no zero-value re-probe dispatched.

## Premise re-measurement (foreman, 2026-11-02)
- CONFIRMED: success=null defect — 34,040 of the last 50,000 data/state/outcomes.jsonl rows
  (68%); full-store snapshot 382,915 null / 48,887 false / 4,773 true (86% null).
- DRIFTED: the "price-0 lane at hop 0" example no longer reproduces — current
  hermes-dagger-foreman chain is 19 hops headed by xkiro-2/xiaomi/mimo-v2.6-pro at
  effective_price 0.015, no free lane present (registry churn since 2026-10-29). The row's
  deliverables are mechanism-shaped and remain live: verdict recording gap confirmed.
- Pre-solve lab consulted (off-by-one, localhost:8766): 4 problem classes probed,
  not_found — no cached answer; no non-trivial debugging arose to submit.

## Implementation (worker glm-5.3-flash@zai-glm, staged milestone briefs, wt/TR-357)
- M1 404a378: `apply_tick_verdicts()` / `_tick_verdicts()` in scripts/router_outcomes.py —
  every CONCLUDED scheduler tick writes a verdict (ticks.commits>0 => success=true,
  'tick_commits_observed'; commits=0 with a session => success=false,
  'tick_concluded_no_commits'); remaining nulls carry reasons (tick_not_concluded /
  no_scheduler_tick_for_session / scheduler_db_missing / scheduler_db_unreadable); sticky
  settled verdicts (TR-299), idempotent, fail-open reads. tests/test_outcome_verdicts.py 23.
- M2 3d42e3b: hop-0 eligibility in scripts/router_spawn.py — ROUTER_HOP0_MAX_DRY (0.65) /
  ROUTER_HOP0_MIN_KNOWN (8) / ROUTER_HOP0 kill switch; dry rate = (known-completed)/known over
  concluded outcomes; n<min_known stays eligible; ONE head swap, never an exclusion, no price
  touched; `hop0_ineligible` + reason + `hop0_eligibility` audit block (never silent).
  tests/test_hop0_eligibility.py 12. Docs: docs/configuration.md.
- M3 a964e9c: recorded demotion — scripts/router_demotions.py; >= ROUTER_DEMOTE_AFTER_FAILURES
  (default 3) consecutive observed failures demote a lane (position-0 displacement, one swap);
  every demote/clear appends a durable record (ts, provider, model, lane,
  consecutive_failure_count, threshold, verdicts, reason) to data/state/demotions.jsonl under
  flock; resolve surfaces `outcome_demotions`. Clear-on-success recorded. tests/test_lane_demotion.py 17.

## Foreman verification (independent re-runs)
- Trio modules (M1+M2+M3): 52/52 passed (run by foreman in wt/TR-357 after each milestone).
- Full suite on the MERGED tree (main @ 0399253, .venv pytest): 2775 passed, 3 skipped,
  1 failed in 976s — sole failure tests/test_tr260_projects_seed.py (pre-existing host red,
  21 enabled fleet lanes missing from projects.jsonl, owned by open row TR-358; untouched by
  this work). On the worktree runs the second named red
  (tests/test_validate.py bare-run registry.json-absent, row QA-TASKROUTER-3) also appeared —
  host-conditional, pre-existing, untouched by this work.
- Live-data smoke on a SCRATCH COPY of the outcome store (live DBs opened read-only, no live
  writes): apply-verdicts stamped 3,319 concluded sessions (+1,334 true, +1,985 false);
  every remaining null carries a reason (379,573 no_scheduler_tick_for_session). Known-n
  growth demonstrated on real row shapes.

## Gates
- Merge: 0399253 (merge of wt/TR-357 into main; worker commits 404a378, 3d42e3b, a964e9c;
  every commit carries ch:trace row=TR-357 + Co-authored-by trailer).
- Push: origin/main parity 0 after push (verified via fetch + rev-list).
- CI: exact-head run 41568765099 on 0399253cc1367e25c4d7fd064466ff202ea7fe11 = completed/success.
- GitReins: task TR-357 created/started in canonical checkout before dispatch; completed
  after merge. Tier 1 PASSED. Tier 2 agentic judge PASS with DEGRADED disclosure (lint/tests
  skipped: no staged files at judge time — work already committed; evaluator independently
  re-ran the trio (52 passed) and the full suite (2761 passed / 1 failed, the TR-358 red)).
  Verdict artifact: .gitreins/history/2026-11-03/7ee9803d/verdict.json
  {task_id: TR-357, passed: true, commit: 0399253cc1367e25c4d7fd064466ff202ea7fe11}.
  Printed judge id 12e347e3; stored artifact dir name is the run id (7ee9803d).

## Known limits / follow-ups
- Row premise drift recorded (hop-0 example stale; mechanism live).
- The live outcome store is not backfilled by this tick: the pass runs from `import-hermes`
  (and `apply-verdicts`); the next import-hermes cron run stamps the live store. Verified on
  scratch copy only (isolation doctrine).
- TR-358 (21 enabled lanes missing from projects.jsonl) and QA-TASKROUTER-3 remain the two
  named pre-existing host reds; both rows open on the board.
