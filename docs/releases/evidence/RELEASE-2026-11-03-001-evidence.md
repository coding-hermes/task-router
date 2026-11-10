# RELEASE-2026-11-03-001 — release-driver evidence (DRY-RUN ONLY)

Row: RELEASE-2026-11-03-001 (task-router board, worktree
`wt/RELEASE-2026-11-03-001-r2`). Recorded 2026-11-09 by the implementation
worker. NOTHING in this row published, tagged, pushed or created a Release:
every command below is read-only against the repo/remotes, or writes only
into temp fixtures and gitignored `dist/`.

Implements the driver the documented procedure now invokes
(docs/releases/release-readiness.md#driver). Runtime serving behavior is
unchanged: no `task_router/` or serving-path file was touched.

## 1. Pre-fix red control (executed before the driver existed)

Command:
```
/home/kara/.hermes/venvs/board/bin/python3 -m pytest -q tests/test_release_driver.py
```
Run BEFORE `scripts/release_driver.py` was written (transcript preserved in
the session scratch, excerpt verbatim):
```
ERROR tests/test_release_driver.py::test_check_passes_on_clean_matching_tagfree_repo
ERROR tests/test_release_driver.py::test_refuses_existing_local_tag - FileNot...
... (11 errors, all FileNotFoundError: the driver file does not exist)
ERROR tests/test_release_driver.py::test_execute_path_refuses_before_mutation_when_gh_missing
1 passed, 3 skipped, 11 errors in 1.04s
```
The one pass is `test_repo_pins_agree_right_now` (a dynamic repo-state
assertion that needs no driver); the three skips are the setuptools-guarded
build arms (board venv has no setuptools). Every driver-dependent arm was
RED pre-fix — the suite is falsifiable, not vacuous.

## 2. Focused suite (green)

Command:
```
.venv/bin/python -m pytest -q tests/test_release_driver.py
```
Result: **15 passed in 2.30s**. Arms: build+SHA256SUMS creation and
`sha256sum -c` verification; tamper detection via `verify-checksums`;
check-happy-path; refusals for existing local tag / existing remote tag /
unverifiable remote / dirty worktree (untracked AND modified) /
non-ancestor base / unresolvable landed-ref / `[Unreleased]` heading /
mismatched pins / tag-version mismatch; repo pins agree right now;
end-to-end dry-run creates no local or remote tag; `--execute` with `gh`
missing refuses BEFORE any mutation (tree stays tag-free).

## 3. AC3 — the five named refusals, each an executed non-zero command

| Refusal | Executed proof (suite fixture = scratch git repos; live = this repo) | Observed |
|---|---|---|
| existing local tag | suite: `git tag -a v0.3.0` then driver `check --tag v0.3.0` | exit != 0, `local tag 'v0.3.0' already exists` |
| existing remote tag | suite: tag pushed to bare origin, local tag deleted, driver `check` | exit != 0, `remote tag 'v0.3.0' already exists` |
| existing tag, live | `make release-check TAG=v0.3.0` on the real repo | exit 2 — refuses first at pins (see §5a); tag checks are suite-proven and run in the same preflight chain |
| dirty worktree | suite: untracked file, then modified tracked file | exit != 0, `worktree is dirty` |
| non-ancestor commit | suite: divergent side branch SHA passed as `--base` | exit != 0, `base <sha> is not an ancestor of main` |
| mismatched version pin | suite: `__init__ 0.2.0` vs heading `[0.3.0]`; live: `[Unreleased]` newest heading | exit != 0, `version pin mismatch`; live: exit 2, `REFUSAL: CHANGELOG's newest heading is [Unreleased] — promote it ...` |

## 4. AC2 — real build: wheel + sdist + verifiable SHA256SUMS

Command: `make release-build` (real repo, venv python 3.11, setuptools 84.2.1):
```
BUILD OK: 2 artifacts + SHA256SUMS in <worktree>/dist
  task_router-0.2.0.tar.gz  849886 bytes
  task_router-0.2.0-py3-none-any.whl  33065 bytes
verified: task_router-0.2.0-py3-none-any.whl, task_router-0.2.0.tar.gz — digests recompute
exit=0
```
`dist/SHA256SUMS` (recorded digests):
```
b8340105f6dc0f17fbd44b829c8d31c800d2f2d8445cf30726b3cfd93a06d01c  task_router-0.2.0-py3-none-any.whl
fd69d79f5ec25dee12af6669e493517e467c5ac26fbb7224597a9aca5a8368ce  task_router-0.2.0.tar.gz
```
Independent consumer verification (executed):
```
$ sha256sum -c SHA256SUMS        (cwd dist/)
task_router-0.2.0-py3-none-any.whl: OK
task_router-0.2.0.tar.gz: OK
verify-exit=0
```
Version-pin validation against the newest CHANGELOG heading is exercised by
the suite (§2) and live in §5a/§5b.

## 5. AC5 — end-to-end dry-run, no tag, no Release

### 5a. Real repo dry-run (documents the current gate state — expected refusal)

```
$ make release-check TAG=v0.3.0 ; .venv/bin/python scripts/release_driver.py cut --tag v0.3.0
REFUSAL: CHANGELOG's newest heading is [Unreleased] — promote it to [<version>] - <date> and bump both version pins before cutting
exit=2
```
Expected: the repo's pins are 0.2.0 with `[Unreleased]` newest (§1) — the
NO-CUT doctrine holds and the driver refuses to even plan a cut until the
owner promotes. The green full path is proven on a fixture (5b).

### 5b. Full green dry-run (promoted-pins fixture; scratch dirs, no external state)

Fixture: `~/.hermes/cache/scratch/tr-rel-e2e/repo` (pins 0.3.0, promoted
`## [0.3.0] - 2026-11-09` heading) + local bare `origin.git`; created with
`git init`, committed, pushed `main` — all under
`GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null`.

Command: `release_driver.py cut --tag v0.3.0 --repo . --remote .../origin.git --landed-ref main`

Result (verbatim key lines):
```
ok: version pins agree (pyproject=0.3.0 __init__=0.3.0 CHANGELOG newest=[0.3.0] tag=v0.3.0)
ok: worktree clean (git status --porcelain empty)
ok: local tag absent (v0.3.0)
ok: remote tag absent (v0.3.0 on .../origin.git; read-only ls-remote)
ok: base cb513fda5bdf is an ancestor of main (cb513fda5bdf)
ok: CHANGELOG section for [0.3.0] extractable
== build: wheel + sdist + SHA256SUMS -> .../dist
ok: built + verified: task_router-0.3.0-py3-none-any.whl, task_router-0.3.0.tar.gz, SHA256SUMS

PUBLISH PLAN (dry-run — NOT executed):
  git -C ... tag -a v0.3.0 -m task-router v0.3.0 cb513fda5bdff8061f4e4c608da8c968845c0b44
  git -C ... push .../origin.git refs/tags/v0.3.0
  gh release create v0.3.0 --verify-tag --title task-router v0.3.0 --notes-file <notes> <assets incl. SHA256SUMS>

DRY-RUN complete — no tag created, nothing pushed, no Release created. To publish (the repo's ONE publish path): re-run with --execute.
exit=0
```
Post-run state (executed):
```
local tags:  []
remote tags: []
```
Fixture dist digests (recorded by the dry-run, verified in-process):
```
d7957a66722a37991650548b15dd1c32ef6a6b01ae0e19a3993cd4ca8f6ec863  task_router-0.3.0-py3-none-any.whl
4ffb17f75d2da662687f78ed6edff27504bed2301bef8db0168459a56e3f72ca  task_router-0.3.0.tar.gz
```
Suite twin: `test_dry_run_creates_no_tag_and_no_release` (same assertions,
hermetic — survives scratch pruning).

## 6. AC4 — exactly one publish path; the unused one named

Executed, live, read-only (2026-11-09):
```
$ git tag -l                                -> []          (zero local tags)
$ git ls-remote --tags origin               -> []          (zero remote tags, ever)
$ ls .github/workflows/                     -> ci.yml      (only file; no release.yml)
$ grep -n -A6 '^on:' .github/workflows/ci.yml
on:
  push:
    branches: [main]
  pull_request:
  # Release verification: CI must run (and stay green) on the v0.2.0 tag
  # commit, but tag pushes are not push-events on branches — dispatch it.
  workflow_dispatch:
```
⇒ The chosen publish path is the driver (`cut --execute`, §3.3 of the
readiness doc). The deliberately unused alternative is a tag-triggered
workflow: none exists, ci.yml deliberately does not fire on tag pushes, and
nothing in CI or the Makefile invokes `--execute`.

## 7. AC1 — the documented procedure invokes the driver

`make help` (verbatim excerpt):
```
make release-check    RELEASE-001: fail-closed preflight (tags/dirty/ancestor/pins)
make release-build    RELEASE-001: wheel + sdist + SHA256SUMS into dist/
make release-dry-run  RELEASE-001: full cut, NO tag, NO Release (dry-run default)
```
docs/releases/release-readiness.md §3.2 now runs `make release-check` /
`make release-build` / `make release-dry-run` + the unchanged
`gitreins-guard-tests.sh` / `ci_gate_check.py --require-green` gates — the
hand-derived checklist is gone.

## 8. AC6 — dispatchable without owner authorization

Executed state changes in this row: (a) new files under the worktree
(scripts/release_driver.py, tests/test_release_driver.py, this artifact,
doc/Makefile edits); (b) gitignored build residue `dist/`, `build/`,
`task_router.egg-info/` in the worktree and fixture dirs; (c) throwaway
scratch fixture repos under the Hermes scratch dir. ZERO pushes, ZERO tags,
ZERO Releases, ZERO API calls: `git tag -l` and `git ls-remote --tags origin`
are both empty after all of the above (§6), and the pre-push gate was never
invoked. The `--execute` publish path is documented (§3.3) but has no caller
in repo, CI, or Makefile.

---

ch:trace row=RELEASE-2026-11-03-001 spec=docs/releases/release-readiness.md#driver test=tests/test_release_driver.py doc=docs/releases/release-readiness.md prompt=/home/kara/.hermes/cache/scratch/task-router-foreman-2026-11-09-12-54-10/RELEASE-2026-11-03-001-brief.txt evidence=docs/releases/evidence/RELEASE-2026-11-03-001-evidence.md witness=none:terminal-transcripts-captured-verbatim-in-this-artifact verdict=none:foreman-will-judge commit=none:worker-commit-pending memory=none:foreman-closeout
