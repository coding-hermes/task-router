# task-router release readiness — 2026-11-14 05:50Z re-anchor

Tick: task-router-releng-2026-11-14-04-57-03
Role: releng satellite (verify only)
Repository: coding-hermes/task-router
Remote: origin = git@github.com:coding-hermes/task-router.git
Forge: GitHub, default branch main

## Decision

NO CUT. This is a first-publication release surface: origin has zero tags and zero GitHub Release objects. No owner authorization for first publication is present in the board. The natural proposal remains MINOR v0.3.0, but it is not an authorization. The repository's own release driver also refuses v0.3.0 until the changelog and version pins are prepared.

## Certified candidate

Candidate SHA: b29ce2af4c37d6bf727081af7da825646a1be98d

Verified:

- `git rev-parse origin/main` = b29ce2af4c37d6bf727081af7da825646a1be98d
- `git ls-remote origin refs/heads/main` = b29ce2af4c37d6bf727081af7da825646a1be98d
- The canonical checkout is clean except that local main is one commit behind origin/main; no local release action was performed.
- A detached worktree was created at the exact remote candidate for executed build/check probes.

## Baseline and semver inventory

No valid release tag exists locally or remotely, and `gh release list --repo coding-hermes/task-router` returns `[]`. A since-tag range therefore cannot be named honestly; the full non-merge history is the inventory baseline.

A NUL-delimited record parser checked every non-merge log record on origin/main: 1,913 records, 0 malformed records, and the class sum reconciles exactly to 1,913:

- feat: 269
- fix: 318
- docs: 95
- chore: 292
- test: 53
- refactor: 3
- perf: 0
- ci: 11
- build: 0
- other: 872
- breaking: 0

No `!` conventional subject and no `BREAKING CHANGE`/`BREAKING-CHANGE` footer was found. The history is feature-bearing, so the proposed bump is MINOR v0.3.0; first publication still requires explicit owner confirmation.

## Version and repository-owned tooling

The candidate tree contains the repository-owned Python release path:

- `scripts/release_driver.py`
- Makefile targets `release-check`, `release-build`, and `release-dry-run`
- `docs/releases/release-readiness.md`

No Go, GoReleaser, Dockerfile/registry, npm package, or Docker publishing path was detected. The release surface is a Python wheel plus sdist.

Version state is not prepared:

- `pyproject.toml`: `version = "0.2.0"`
- `task_router/__init__.py`: `__version__ = "0.2.0"`
- `CHANGELOG.md`: newest heading is `[Unreleased]`, followed by `[0.2.0] — 2026-09-24`

Executed from the exact candidate worktree:

- `/usr/bin/python3 scripts/release_driver.py check --tag v0.3.0 --base b29ce2af4c37d6bf727081af7da825646a1be98d` → exit 2; refusal: newest CHANGELOG heading is `[Unreleased]` and both version pins must be bumped.
- `/usr/bin/python3 scripts/release_driver.py build` → exit 0; produced strategy-only `task_router-0.2.0-py3-none-any.whl` (33,063 bytes), `task_router-0.2.0.tar.gz` (871,572 bytes), and verified `SHA256SUMS`. These are not v0.3.0 publishable assets.

## Hosted CI

Exact candidate run:

- Run 43563172395
- SHA b29ce2af4c37d6bf727081af7da825646a1be98d
- Workflow `ci`, push event
- Status `in_progress`, conclusion unset
- Job `test` in progress; full test suite in progress; guard, fresh-clone, registry, null-debt, TR-251, outcome-probe, and proxy verification steps pending

An in-progress exact-SHA run is not release-green. Earlier run 43532815403 at f4f23709 is not borrowed because the candidate has advanced. No CI red is asserted for b29ce2af; the gate is pending/unproven and blocks a cut.

## GitReins and dogfood

The newest stored GitReins verdict is `.gitreins/history/2026-11-08/37001239/verdict.json`, passed=true but evaluated against a3e61bb085840ac7f09705aee77502aa093b19ea. No `.gitreins` record references b29ce2af4c37d6bf727081af7da825646a1be98d. Candidate-bound GitReins release evidence is therefore UNVERIFIED, not red.

The latest committed dogfood evidence is `DOGFOOD-2026-11-12-134125.txt`, run 45, marked PROMISING-BUT-ROUGH. It reports seeded-corpus failures, an install-agent death before UI/test legs, circuit-state and effort-plane divergence, and served-plane drift. Fresh-host installation itself was proven, but the battery is stale relative to b29ce2af and is not a release waiver.

## Deployment and host posture

Read-only health probes at 2026-11-14 05:50:58Z:

- `http://127.0.0.1:9092/health` → HTTP 200, `stale=true`, loaded commit 6cbc6aefa573, repo commit 70e2beed2c2e
- `http://127.0.0.1:9391/health` → HTTP 200, `stale=true`, loaded commit 6cbc6aefa573, repo commit 70e2beed2c2e
- `http://127.0.0.1:9093/health` → HTTP 404; no staleness surface, therefore unmeasured

Host filesystem probe: `/dev/nvme0n1p2` is 97% used with 54G available. `/home/kara/.hermes/cache/scratch` is 88G and task-router `data/state` is 130G. No cleanup or service restart was performed by this lane.

## Release surface and mutations

Remote tags: none. GitHub Releases: none. No tag, version edit, changelog promotion, release asset upload, GitHub Release, service restart, or cleanup was performed.

This artifact records readiness only. The next release gate is explicit owner authorization for task-router v0.3.0 and its baseline, followed by version preparation, terminal green CI on the exact prepared SHA, candidate-bound GitReins evidence, a fresh repository-driver build, and only then the driver's owner-gated publish path.

## Known issues

- First-publication authorization is absent; no release may be cut.
- Version preparation is absent; the driver refuses v0.3.0.
- Exact candidate CI run 43563172395 is in progress, not green.
- No GitReins verdict covers b29ce2af.
- Dogfood is stale and PROMISING-BUT-ROUGH, with failed/unverified cells.
- The :9092 and :9391 listeners report stale=true; :9093 is unmeasured.
- Release host disk usage is 97% with 54G free; no cleanup was authorized or performed.
