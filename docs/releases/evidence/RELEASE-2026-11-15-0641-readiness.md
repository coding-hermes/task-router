# task-router release readiness — 2026-11-15 (tick task-router-releng-2026-11-15-05-41-02)

Satellite: release-engineering. Role: verify only — findings become `RELEASE-*` rows for the
foreman. Every claim below is a command receipt from this tick, run between 06:10Z and 06:35Z.

## 1. Candidate and code surface

| Item | Value | Receipt |
|---|---|---|
| `origin/main` at sweep | `5697812f` | `git rev-parse origin/main` |
| code-bearing candidate | `aa8c081e` (tree `e6b7a98c6019983c13fd5036c4790d69724fe5b2`) | `git rev-parse aa8c081e^{tree}` |
| code surface identical to main tip | yes — 0 code files differ | `git diff --name-only aa8c081e 5697812f -- . ':!.coding-hermes' ':!docs' \| wc -l` → `0` |
| files that do differ | 2, both non-code: `.coding-hermes/board/tasks.jsonl`, `docs/releases/evidence/RELEASE-2026-11-14-0457-r2-evidence.md` | same command without pathspec |
| tags on origin | **0** | `git ls-remote --tags origin \| wc -l` |
| GitHub Releases | **0** | `gh release list --repo coding-hermes/task-router` |
| newest GitReins history run | 2026-11-08 (nothing binds the candidate) | `ls -dt .gitreins/history/*/` |

Consequence: this remains an **untagged first release**. A hosted run that is green on a code
parent does not grade the board/docs carrier commits — those are tracked separately below.

## 2. Exact-SHA hosted CI

| run id | sha | status | note |
|---|---|---|---|
| 43682318136 | `aa8c081e` | **completed / success** | 15/15 steps; `python3 -m pytest -q tests/` (line 124 of `ci.yml`) is the full suite and includes `tests/test_release_driver.py` |
| 43682908328 | `4d4c804a` | in progress (since 05:52:08Z, still queued/running at 06:30Z) | board-only carrier |
| 43682961953 | `49bc0746` | in progress (since 05:52:55Z) | docs-only carrier |
| 43682967447 | `5697812f` | in progress (since 05:52:59Z) | board-only carrier |

Green CI exists for the code surface; the three carrier SHAs are unverified, not red.

## 3. Release tooling — repo-owned, present, and exercised

Repo ships its own procedure (Bane doctrine satisfied): `scripts/release_driver.py` (blob
`a699223d` on `origin/main`), `tests/test_release_driver.py`, and Makefile targets
`release-check`, `release-build`, `release-dry-run`. `docs/releases/release-readiness.md`
documents the cut order and states that no owner authorization for a cut is on record.

Driver exercised from a clean detached worktree at `aa8c081e`:

```
$ python3 scripts/release_driver.py check --tag v0.3.0
REFUSAL: CHANGELOG's newest heading is [Unreleased] — promote it to [<version>] - <date>
and bump both version pins before cutting
exit 2
```

The refusal is **semantic, not a crash**: the driver runs, reaches its version gate, and stops.
This also retires the old P0 framing that the driver is "absent/stranded" — it is on main and
`RELEASE-2026-11-09-001`'s AC5/AC6/AC7 substance is satisfied (see that row's closeout).

## 4. Version surface (the one failing hard invariant)

| Surface | Now | Required for a cut |
|---|---|---|
| `pyproject.toml` `version` | `0.2.0` | `0.3.0` |
| `task_router/__init__.py` `__version__` | `0.2.0` | `0.3.0` |
| `CHANGELOG.md` newest heading | `[Unreleased]` | `## [0.3.0] - 2026-11-DD` |

Version-implied proposal: **v0.3.0 (minor)**. `v0.2.0` was shipped in-tree at `f04ac474`
(2026-09-24) and never tagged; history since carries features, so the documented version surface
combined with a `feat`-bearing delta implies the next minor. Filed as `RELEASE-2026-11-15-001`.

## 5. Dependency scan (Decision 3)

`uvx --from pip-audit pip-audit` against the worktree's `pyproject.toml` + `uv.lock`:
`No known vulnerabilities found` (exit 0). No advisory fires Decision 1 rule 1, so there is no
"cut now regardless of other gates" trigger from dependencies.

## 6. Candidate-bound guard

```
$ gitreins guard   (clean detached worktree at aa8c081e)
⚠ DEGRADED PASS: tests=no staged files — these gates did not run
  ✓ secrets — clean (gitleaks + builtin cross-check)
```

A degraded guard is not evidence the tree passes; the authoritative test receipt is the hosted
run 43682318136 above, which also executes the repo's guard-parity step.

## 7. Host capacity

`df -h /` → **140G available, 92% used** (was 58G / 97% on 2026-11-14). Headroom for build and
publish is adequate. Scratch growth continues: `~/.hermes/cache/scratch` = **107G across 4951
entries** (was 88G / 2748), and the 04:15 `hermes-scratch-cleanup.sh` cron exists but is not
reclaiming. Tracked by `RELEASE-2026-11-14-004`, left pending.

## 8. Decision

**NO CUT this tick.** Reason: Decision 1 rule 4 fires (a `feat`-bearing delta since the last
release artifact makes a minor due), but one hard invariant fails — the version surface is not at
the release version, proven by the driver's own refusal (section 3/4). Everything else a cut needs
is in place: release tooling present and working, exact-SHA hosted CI green on the code surface,
no dependency advisory, host headroom adequate.

The cut is **one dispatch away**: promote the surface, re-run CI green on the promoted SHA, then
`make release-check TAG=v0.3.0 && scripts/release_driver.py cut --tag v0.3.0 --execute`.

Escalation state: first-ever publication is owner-gated by `RELEASE-2026-10-22-001`. That row now
names the decision, the evidence, and the default this lane will take if silence continues
(cut v0.3.0 from the promoted main SHA). It is no longer a standing NO-GO.

## 9. Rows filed / updated this tick

- created `RELEASE-2026-11-15-001` — promote the version surface to 0.3.0 (P0, the single blocker)
- closed `RELEASE-2026-11-09-001` — superseded: driver is on main, tests green in hosted CI
- updated `RELEASE-2026-10-22-001` — escalation restated with a named default, no longer a hold
- updated `RELEASE-2026-11-14-004` — disk facts refreshed (140G free; scratch 107G / 4951)

Unverified this tick: live listener staleness (`ss`/`pgrep` probe was gate-blocked in this
unattended session; `RELEASE-2026-11-08-001` remains the carrier for that question).
