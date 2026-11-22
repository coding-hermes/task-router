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

Updated **2026-11-22** (RELEASE-2026-11-15-001): **version surface promoted
to 0.3.0** — both pins bumped and the curated `[Unreleased]` CHANGELOG
section promoted to `## [0.3.0] - 2026-11-22`, with a fresh empty
`[Unreleased]` above it. This is surface promotion only, in prep for the due
minor cut authorized by `RELEASE-2026-10-22-001`: **no tag, no tag push, no
GitHub Release, no cut executed.**

## 1. Current release surface: none (version surface PROMOTED 2026-11-22)

Measured live on 2026-10-11 (not recalled from docs); re-verified 2026-11-09
(read-only `git tag -l` and `git ls-remote --tags origin` — both empty);
re-measured 2026-11-22 (RELEASE-2026-11-15-001, read-only commands):

| Check | Command | Result (2026-11-22) |
|---|---|---|
| Tags | `git tag -l`; `git ls-remote --tags origin` | **zero tags, ever** (both empty) |
| Releases | `gh release list` | **zero releases** |
| CI on main | `gh run list --limit 3` | 3/3 recent main-push runs completed/success (pre-promotion surface, HEAD `e03bfce0`) |
| Version pins | `pyproject.toml`, `task_router/__init__.py`, newest CHANGELOG heading | all `0.3.0` — promoted 2026-11-22 (RELEASE-2026-11-15-001) |

History: the pins were `0.2.0` at every prior measurement (2026-10-11 and
2026-11-09 — `0.2.0` shipped UNRELEASED in-tree at `f04ac474`, no tag was
ever cut for it); the 2026-11-22 promotion is the first time the surface
matches the proposed cut version.

Conclusion of the v0.2.0 readiness report (`v0.2.0-readiness.md` §7: "tag
`v0.2.0`, push the tag, dispatch CI…") was never executed: the release
handoff lapsed after the 2026-09-24 merge. The repo therefore has a release
*procedure* on paper and zero release *artifacts*.

## 2. First-cut decision: authorized (RELEASE-2026-10-22-001) — NOT EXECUTED

The owner gate that blocked the 2026-10-11 sweep has since moved:
`RELEASE-2026-10-22-001` owns the first-ever-publication decision and names
a default (cut `v0.3.0` from the promoted main SHA) — it is no longer a
standing NO-GO, so the due minor cut is authorized pending its own gates.
With the 2026-11-22 promotion, step 2 of the old checklist is done: the
version is pinned (`0.3.0` — both pins AND the newest CHANGELOG heading),
which the driver enforces mechanically. What remains before a cut:

1. ~~**Authorize or decline the cut**~~ — RELEASE-2026-10-22-001 names the
   due-minor decision (§4 still records any explicit owner statement at
   execute time).
2. ~~**Pick the version.**~~ DONE — `0.3.0`, promoted 2026-11-22
   (RELEASE-2026-11-15-001).
3. **Pick the tag baseline SHA** — normally `origin/main` HEAD at cut time,
   with CI green on that exact SHA.

Until the remaining gates are met: **no tag, NO GitHub Release.**
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
# 0. Owner authorization (RELEASE-2026-10-22-001) + version + baseline SHA.
#    The version promotion this step used to require is DONE (2026-11-22,
#    RELEASE-2026-11-15-001): both pins AND the newest CHANGELOG heading are
#    `0.3.0`, so the pin checks below pass unchanged. A future cut repeats
#    the same promotion mechanism (rename the freshly-empty [Unreleased],
#    bump both pins — two pins, no other location).
#      sed -i 's/^version = "0.3.0"/version = "<version>"/' pyproject.toml
#      sed -i 's/^__version__ = "0.3.0"/__version__ = "<version>"/' task_router/__init__.py

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
   `[Unreleased]` and any pin mismatch). **DONE 2026-11-22**
   (RELEASE-2026-11-15-001): the driver's full `check --tag v0.3.0
   --base origin/main` exits 0 in a clean detached verification worktree at
   the promoted SHA. The identical invocation in THIS shared checkout
   refuses — on the checkout's three pre-existing foreign entries (two
   untracked `data/quota_limits.json.bak*` files and the foreman-owned
   dirty `.gitreins/tasks.yaml` row update), not on the promotion surface.
3. CI green on the exact tagged SHA (a `workflow_dispatch` run on that ref).
4. Tag pushed to origin.
5. `gh release view` shows the release with notes + assets.
6. Fresh-venv consumer smoke passes (§3.5 post-cut).

Anything less = NO CUT. Verdict as of 2026-11-22 (RELEASE-2026-11-15-001):
**version surface PROMOTED, cut NOT executed** — gate 2 is the only one
met so far (§ above); CI-on-promoted-SHA, tag push, Release and consumer
smoke remain unexecuted, and zero tags / zero releases still hold (§1
measurement of 2026-11-22).

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
