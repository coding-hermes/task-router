# Release readiness — task-router, current state: NO CUT

Row: RELEASE-2026-10-04-001 (readiness/documentation only — this is not a
release). Status as recorded: **2026-10-11**, HEAD `7a2f99b`.

## 1. Current release surface: none

Measured live on 2026-10-11 (not recalled from docs):

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

## 3. How releases conceptually fire — tag-triggered procedure

There is no tag-triggered workflow (`.github/workflows/ci.yml` triggers on
`push: branches: [main]`, `pull_request`, and `workflow_dispatch` only — tag
pushes deliberately do not auto-run anything). Verification of the tag commit
is MANUAL, via `workflow_dispatch`, which is this repo's established pattern
(v0.2.0 readiness §6.3 and release body §Infrastructure: "CI gained
`workflow_dispatch` so the release tag commit is CI-verified").

### Pre-cut checklist (all manual, in order)

```bash
# 1. CHANGELOG promotion + version bump (NOT done in this readiness task):
#    rename "## [Unreleased]" to "## [<version>] — <date>" in CHANGELOG.md,
#    start a fresh empty [Unreleased]; bump version = in BOTH:
sed -i 's/^version = "0.2.0"/version = "<version>"/' pyproject.toml
sed -i 's/^__version__ = "0.2.0"/__version__ = "<version>"/' task_router/__init__.py
#    (two pins, no other location — grep '0\.2\.0' to confirm at cut time)

# 2. Full suite green on the release tree:
bash scripts/gitreins-guard-tests.sh

# 3. Commit the release prep, merge/land on main, push; CI on main green:
python3 scripts/ci_gate_check.py --require-green
```

### Cut procedure (fires only after owner authorization + pre-cut checklist)

```bash
TAG=v0.3.0                                # owner's chosen version
BASE=$(git rev-parse origin/main)         # owner's chosen baseline SHA

# CI green on the tagged SHA — REQUIRED before tagging:
gh workflow run ci.yml --ref main
gh run watch $(gh run list --workflow=ci --branch=main --limit 1 --json databaseId -q '.[0].databaseId')

# Tag + push (DESTRUCTIVE release actions — only on owner authorization):
git tag -a "$TAG" -m "task-router $TAG" "$BASE"
git push origin "$TAG"

# CI on the tagged commit (tag pushes do NOT trigger ci.yml — dispatch, then verify):
gh workflow run ci.yml --ref "$TAG"
sleep 15   # dispatched runs take a few seconds to register
gh run watch "$(gh run list --workflow=ci --event=workflow_dispatch --limit 1 --json databaseId -q '.[0].databaseId')"

# GitHub Release with notes + assets:
gh release create "$TAG" --verify-tag --title "task-router $TAG" \
  --notes "$(sed -n "/^## \[${TAG#v}\]/,/^## \[/p" CHANGELOG.md | sed '$d')"

# Verify the release surface is real (expect notes + source assets):
gh release view "$TAG" --json name,tagName,assets,isDraft,publishedAt
```

### Post-cut consumer smoke (fresh venv, real download)

```bash
rm -rf /tmp/tr-smoke && python3 -m venv /tmp/tr-smoke
/tmp/tr-smoke/bin/pip install --quiet task-router=="$TAG"
/tmp/tr-smoke/bin/python -c "from importlib.metadata import version; print(version('task-router'))"
/tmp/tr-smoke/bin/python -c "import task_router; print(task_router.__version__)"
```

The CLI ships with NO `--version` flag (`task_router/cli.py` defines none),
so the honest smoke is the `__version__` import above; a `router --help`
invocation proves the entry point installs and dispatches. Wheel/source
artifacts: this is a pure-stdlib package — `gh release create` with no asset
files publishes GitHub's auto-generated source archives; if a built wheel is
wanted, build with `python -m build` and pass the dist file to
`gh release create`. Re-download the published artifact
(`gh release download "$TAG"` into an empty dir) and
`pip install <downloaded file>` before the smoke if you want the artifact
itself exercised rather than the index entry.

## 4. Release gate — the no-cut conditions

A cut may proceed ONLY when ALL of the following hold (evidence recorded in
this file or its successor at cut time):

1. Owner authorization statement exists (who, when, version, baseline SHA).
2. CHANGELOG `[Unreleased]` promoted + version bumped in
   `pyproject.toml` AND `task_router/__init__.py` (both pins).
3. CI green on the exact tagged SHA (a `workflow_dispatch` run on that ref).
4. Tag pushed to origin.
5. `gh release view` shows the release with notes + assets.
6. Fresh-venv consumer smoke passes (§3 post-cut).

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
- Live commands and outputs captured 2026-10-11: see §1 table.
