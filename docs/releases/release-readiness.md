# Release readiness — task-router, current state: NO CUT

Row: RELEASE-2026-10-04-001 (readiness/documentation only — this is not a
release). Status as recorded: **2026-10-11**, HEAD `7a2f99b`.
Updated **2026-11-09** (RELEASE-2026-11-03-001): §3's hand-derived pre-cut
checklist is now an executable, fail-closed release driver
(`scripts/release_driver.py` behind `make release-check` /
`make release-build` / `make release-dry-run`). The documented procedure
invokes the driver — it is never re-derived by hand. That row itself ran
DRY-RUNS ONLY (no tag, no push, no Release); its evidence:
`docs/releases/evidence/RELEASE-2026-11-03-001-evidence.md`.

## 1. Current release surface: none

Measured live on 2026-10-11 (not recalled from docs); re-verified 2026-11-09
(read-only `git tag -l` and `git ls-remote --tags origin` — both empty):

| Check | Command | Result |
|---|---|---|
| Tags | `git tag -l`; `gh api repos/coding-hermes/task-router/tags` | **zero tags, ever** (`[]`) |
| Releases | `gh release list` | **zero releases** |
| In-flight CI | `gh run list --limit 6` | HEAD run `38178630630` in_progress; HEAD~1 `38174792603` success; last failure `38135792741` at 2026-10-11T11:30Z |
| Version pins | `pyproject.toml`, `task_router/__init__.py` | both `0.2.0` (0.2.0 shipped UNRELEASED — no tag was ever cut for it) |

Conclusion of the v0.2.0 readiness report (`v0.2.0-readiness.md` §7: "tag
`v0.2.0`, push the tag, dispatch CI…") was never executed: the release
handoff lapsed after the 2026-09-24 merge. The repo therefore has a release
*procedure* on paper and zero release *artifacts*.

## 2. First-cut decision: BLOCKED / NOT AUTHORIZED

No owner authorization for a cut exists. This row deliberately does NOT
invent one. Before any release action, the owner must:

1. **Authorize or decline the cut** (explicit statement, not silence).
2. **Pick the version.** `CHANGELOG.md` currently has a populated
   `[Unreleased]` section (TR-299 verified-outcome feedback leg, SCHED-GAP-1713
   per-lane ingress). By this repo's own semver rationale
   (`v0.2.0-readiness.md` §1 — pre-1.0, 0.y is the minor axis, feature work
   ⇒ MINOR), the natural candidate is `0.3.0`, decided by the owner.
3. **Pick the tag baseline SHA** — normally `origin/main` HEAD at cut time,
   with CI green on that exact SHA.

Until that decision exists: **NO tag, NO GitHub Release, no version bump.**
The driver enforces this shape mechanically: `cut --execute` is the only
publishing command in the repo, it refuses every unsafe preflight, and
nothing in CI or the Makefile invokes it (§3.4).

## 3. The release driver <a id="driver"></a>

`scripts/release_driver.py` is the repository-owned release driver
(RELEASE-2026-11-03-001). Fail-closed by construction: every check is an
executed, read-only command, and a check that cannot run is a refusal — never
a pass. Exit codes: 0 = passed / dry-run complete; 2 = refusal; 3 =
environment failure (missing backend/tool). Run it inside the venv
(`make venv install`; uv-built venvs need an explicit
`pip install setuptools` — python3.11 venvs bundle it).

| Verb | What it does | Make target |
|---|---|---|
| `check` | every preflight refusal below, read-only | `make release-check TAG=vX.Y.Z [BASE=<sha>]` |
| `build` | wheel + sdist + SHA256SUMS into `dist/`, digests verified twice (in-process recompute + `sha256sum -c` format) | `make release-build` |
| `verify-checksums` | recompute `dist/SHA256SUMS` against the artifacts (consumer-side check) | — |
| `cut` | `check` + `build` + the publish plan. DRY-RUN by default: creates **no tag, no push, no Release**. `--execute` is the one publish path (§3.3) | `make release-dry-run TAG=vX.Y.Z [BASE=<sha>]` |

### 3.1 Refusals (exit 2, explicit wording)

| Condition | How it is detected (executed command) |
|---|---|
| local tag already exists | `git tag --list <tag>` |
| remote tag already exists | `git ls-remote --tags <remote> refs/tags/<tag>` (read-only) |
| remote cannot be verified | `ls-remote` failure ⇒ refusal (fail-closed: an unreadable remote is never treated as "no tag") |
| dirty worktree (modified or untracked) | `git status --porcelain` |
| base not landed | `git merge-base --is-ancestor <base> <landed-ref>` |
| unresolvable `--base` / `--landed-ref` | `git rev-parse --verify <ref>^{commit}` |
| newest CHANGELOG heading is `[Unreleased]` | first `## [...]` heading parse |
| version pins disagree | `pyproject.toml` `project.version` == `task_router/__init__.py` `__version__` == newest `## [x.y.z]` heading |
| tag not matching the pinned version | `--tag` must equal `v` + pinned version |

### 3.2 Pre-cut procedure (driver invocation — replaces the old manual checklist)

```bash
# 0. Owner authorization + version + baseline SHA (§2) — still required.
#    The driver REFUSES until the promotion is done; the sed commands below
#    remain the promotion mechanism (two pins, no other location):
#      rename "## [Unreleased]" to "## [<version>] - <date>" in CHANGELOG.md
#      and start a fresh empty [Unreleased]; then bump both pins:
sed -i 's/^version = "0.2.0"/version = "<version>"/' pyproject.toml
sed -i 's/^__version__ = "0.2.0"/__version__ = "<version>"/' task_router/__init__.py

# 1. Preflight — every refusal above, as executed read-only commands:
make release-check TAG=v0.3.0 BASE=$(git rev-parse origin/main)

# 2. Artifacts — wheel + sdist + SHA256SUMS, digests verified:
make release-build

# 3. Dry-run of the whole cut — prints the exact publish plan, creates nothing:
make release-dry-run TAG=v0.3.0 BASE=$(git rev-parse origin/main)

# 4. Full suite green on the release tree (unchanged gate):
bash scripts/gitreins-guard-tests.sh

# 5. CI green on main (unchanged gate):
python3 scripts/ci_gate_check.py --require-green
```

Anything `release-check` refuses stops the procedure — the checklist cannot
be partially hand-waved past a red driver.

### 3.3 The ONE publish path: driver `cut --execute` (owner-gated)

The repo's single publishing mechanism is:

```bash
.venv/bin/python scripts/release_driver.py cut --tag v0.3.0 \
  --base <owner-chosen-sha> --execute
```

It re-runs every refusal from §3.1, rebuilds + re-verifies the artifacts,
checks its own prerequisites (`gh` on PATH) **before** the first mutation,
then: `git tag -a <tag> <base>` → `git push origin refs/tags/<tag>` →
`gh release create <tag> --verify-tag --notes-file <promoted CHANGELOG section>`
with `dist/` + `SHA256SUMS` as release assets. Owner authorization (§2) is
required to run it: RELEASE-2026-11-03-001 itself never did — it exercised
only the dry-run (evidence artifact).

CI verification of the tagged commit stays the established manual pattern
(tag pushes fire nothing; dispatch via `workflow_dispatch`, then
`gh run watch`) — the same procedure the 0.2.0 plan specified.

### 3.4 The deliberately unused alternative: a tag-triggered workflow

A tag-triggered GitHub Actions workflow would be the other natural publish
path; it is deliberately NOT used. Proven live 2026-11-09:

- `.github/workflows/` contains exactly one file, `ci.yml`;
- its `on:` block is `push: branches: [main]`, `pull_request`,
  `workflow_dispatch` — **a tag push triggers nothing, by design** (the
  in-file comment names this: "tag pushes are not push-events on branches —
  dispatch it");
- there is no `release.yml` and no release job anywhere in CI.

Keeping publishing inside the driver (not a workflow) means the destructive
actions stay behind the fail-closed preflight, the dry-run, and the owner
gate — one auditable code path instead of a second, implicit one.

### 3.5 Post-cut consumer smoke (fresh venv, real download)

```bash
rm -rf /tmp/tr-smoke && python3 -m venv /tmp/tr-smoke
/tmp/tr-smoke/bin/pip install --quiet task-router=="$TAG"
/tmp/tr-smoke/bin/python -c "from importlib.metadata import version; print(version('task-router'))"
/tmp/tr-smoke/bin/python -c "import task_router; print(task_router.__version__)"
```

The CLI ships with NO `--version` flag (`task_router/cli.py` defines none),
so the honest smoke is the `__version__` import above; a `router --help`
invocation proves the entry point installs and dispatches. The wheel + sdist
built by `make release-build` (with their SHA256SUMS) are the release assets;
re-download the published artifacts (`gh release download "$TAG"` into an
empty dir) and `pip install <downloaded file>` before the smoke to exercise
the artifact itself rather than the index entry.

## 4. Release gate — the no-cut conditions

A cut may proceed ONLY when ALL of the following hold (evidence recorded in
this file or its successor at cut time):

1. Owner authorization statement exists (who, when, version, baseline SHA).
2. CHANGELOG `[Unreleased]` promoted + version bumped in
   `pyproject.toml` AND `task_router/__init__.py` — mechanically proven by
   `make release-check TAG=v<version>` exiting 0 (the driver refuses
   `[Unreleased]` and any pin mismatch).
3. CI green on the exact tagged SHA (a `workflow_dispatch` run on that ref).
4. Tag pushed to origin.
5. `gh release view` shows the release with notes + assets.
6. Fresh-venv consumer smoke passes (§3.5 post-cut).

Anything less = NO CUT. This row's verdict: **NO CUT (blocked on owner
decision; zero prior releases; CI in flight at time of check).**

## 5. Evidence & cross-references

- Prior readiness sweep: `docs/releases/v0.2.0-readiness.md` (semver
  rationale §1, CI-at-cut §3, open-P1 dispositions §5, handoff §7 —
  the handoff that was never executed).
- Release body precedent: `docs/releases/v0.2.0-release-body.md`
  (categorized notes pulled verbatim from CHANGELOG).
- CI gate doctrine: `docs/ci-gate.md` (red-on-main must stop being ignorable;
  `ci_gate_check.py --require-green` is the pre-cut green check).
- CI workflow: `.github/workflows/ci.yml` (triggers: push-main, PR,
  workflow_dispatch; 75-min job timeout; full suite runs TWICE per job).
- Driver row evidence (RELEASE-2026-11-03-001): red-control transcript,
  refusal proofs, real build + checksum verification, end-to-end dry-run
  (no tag, no Release): `docs/releases/evidence/RELEASE-2026-11-03-001-evidence.md`.
- Live commands and outputs captured 2026-10-11: see §1 table.
