# task-router release readiness — 2026-11-26 (tick task-router-releng-2026-11-26-06-37-45)

board root: /home/kara/task-router (shared checkout, == origin/main)
product checkout: /home/kara/task-router (same tree; remote coding-hermes/task-router)
candidate: 359a8cda71f42b4c83239a164904762fe1710f91 (origin/main tip at measurement)
prior certified surface: aa8c081e (2026-11-15 pass); delta aa8c081e..359a8cda = 399 non-merge commits, 201 changed paths (material — fresh census run)
probing: measurements taken in the shared checkout (read-only) plus a detached worktree at the candidate for the driver preflight; product checkout untouched by this pass.

## Decision: NO CUT

Authorization and channel creation are owner-gated; every mechanical gate now passes.
Rule fired: minor bump due and authorized-pending — census v0.2.0..359a8cda = 1881 non-merge
commits, 0 breaking subjects, 0 breaking footers (conventional-commit-census.py, sum check
1881/1881) → bump rule minor → v0.3.0, matching the promoted version surface.

## Delta vs 2026-11-15 pass (only surfaces that moved)

| surface | 2026-11-15 reading | 2026-11-26 reading |
|---|---|---|
| version surface | 0.2.0 / [Unreleased] — driver refused | 0.3.0 / [0.3.0] 2026-11-22 (d4a48e45); pins agree — FLIPPED |
| driver preflight | REFUSAL: [Unreleased] | CHECK PASSED at 359a8cda (clean detached tree) — FLIPPED |
| exact-tip CI | 43682318136 green at aa8c081e | 45741267768 success at 359a8cda (2026-11-26T02:16Z) — NEW |
| disk | 92%, 141G free | 86%, 247G free — improved |
| serving 9391 | flag-only stale, source current | loaded ef5b89b4 (11-23 13:22Z), loaded_source_sha 5981145c != live 5d598490 — MATERIALLY STALE |
| serving 9092 | flag-only stale, source current | loaded 216a7bd8 (11-23 19:05Z), loaded_source_sha == live 5d598490 — flag-only stale |
| dirty-tree gate | not measured as blocker | REFUSAL on 6 runtime-residue entries in shared checkout (new row) — NEW |

## Unchanged surfaces (re-measured, carried)

- Tags/releases: 0 local tags, 0 remote tags, 0 GitHub Releases (`git tag -l`, `gh release list` empty). First publication remains channel creation.
- Authorization: RELEASE-2026-10-22-001 pending (updated 2026-11-23), silence default stands. Scheduler tick prompts and agent-authored board text are not consent (2026-11-15 correction carried).
- gitreins verdict gap: newest PASS 681fdacc (2026-11-24, TR-368 judge) is 7 product paths behind the candidate; three 2026-11-25 verdicts are task-scoped FAILs (TR-367 class at edda077b / 0c208953), not release-gate verdicts. No verdict binds the candidate.
- Census rule: zero breaking since v0.2.0 → minor, as on 2026-11-14/15.

## Blockers in the driver's own gate order

1. Dirty-tree gate (false positive): driver refuses in the shared checkout on 6 entries —
   M .gitreins/tasks.yaml (gitreins runtime), ?? .coding-hermes/evidence/* (lane evidence),
   ?? data/*.bak-* (operator backups), ?? data/state/outcomes.jsonl.imported (import residue).
   None are product source. In a clean detached worktree at the same SHA the gate passes and
   CHECK PASSED prints. Filed as RELEASE-2026-11-26-001 with RED-CONTROL acceptance.
2. Authorization (owner): RELEASE-2026-10-22-001 — decision recorded R4 minor due. The
   2026-11-15 escalation named a silence default, but the SAME tick's refire corrected the
   record: tick prompts and agent-authored board text are not consent, so silence leaves the
   cut uncut. An explicit owner instruction naming version and baseline SHA is required.
3. First-publication channel creation (owner): no tag-triggered publisher has ever run; the
   publish path is code nobody has executed (0 tag-push runs in CI history).

## Row dispositions this pass

- RELEASE-2026-10-22-001: note — all mechanical gates green at 359a8cda; only owner legs remain.
- RELEASE-2026-11-14-001: note — re-measured 0 tags/0 releases; census re-confirmed minor v0.3.0.
- RELEASE-2026-11-14-002: note — verdict gap re-measured (above).
- RELEASE-2026-11-08-001: note — 9391 materially stale, 9092 flag-only (above).
- RELEASE-2026-11-14-004: note — disk improved to 86% / 247G free.
- RELEASE-2026-11-09-001: audit event — premise stale: driver on main (RELEASE-2026-11-03-001
  complete); wt/RELEASE-2026-11-03-001-r2 is 3 commits ahead, NOT an ancestor, driver content
  differs from main; foreman to reconcile or close.
- RELEASE-2026-11-26-001: NEW — driver dirty-tree gate false positive on runtime residue.

## Observations (measured, deliberately not filed)

- events.jsonl carries multiple null-id events (pre-existing board vintage); numeric ids are
  unique, max 1794115000000000141. The 2026-11-15 duplicate-id hazard (...049) is resolved.
- Shared checkout holds foreign runtime residue (listed under blocker 1); left untouched.
- WAVE_BUDGET 2 unused: every remaining blocker is an owner decision or a foreman-owned row;
  dispatching would duplicate pending work.

## Receipts

- CI at candidate: run 45741267768, workflow ci, headSha 359a8cda, completed/success.
- Driver: scripts/release_driver.py check --tag v0.3.0 → CHECK PASSED (clean tree, nothing written);
  same command in shared checkout → REFUSAL: worktree is dirty (6 entries).
- Census: conventional-commit-census.py f04ac474..359a8cda → 1881 non-merge, breaking 0, bump_rule=minor.
- Health: 9391/health loaded_commit ef5b89b4b14b stale=true; 9092/health loaded_commit 216a7bd831ac
  stale=true, loaded_source_sha == live_source_sha 5d598490e59c9d23.
- df /: 1.8T total, 1.5T used, 247G free, 86%.
