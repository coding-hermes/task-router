"""RELEASE-2026-11-03-001 — release-driver contracts.

The release driver (scripts/release_driver.py) replaces the hand-derived
pre-cut checklist in docs/releases/release-readiness.md with one fail-closed
command surface. Every arm here is an EXECUTED command against a scratch git
fixture — a refusal is proven by the driver's own non-zero exit and wording,
never by inspecting its source.

Pinned contracts:
  * build  — wheel + sdist land in --out-dir with a SHA256SUMS whose digests
    recompute; a tampered artifact is DETECTED by `verify-checksums`.
  * check  — exit 0 on a clean, pins-consistent, tag-free tree; non-zero with
    explicit wording for each refusal the row names:
      - existing local tag
      - existing remote tag (read-only `git ls-remote --tags`)
      - unverifiable remote (fail-closed: a dead remote is NOT "no tag")
      - dirty worktree (untracked or modified)
      - non-ancestor base commit
      - unresolvable landed ref
      - [Unreleased] as the newest CHANGELOG heading
      - mismatched version pins (pyproject / __init__ / CHANGELOG heading)
      - tag name not matching the pinned version
  * cut    — DRY-RUN (default) completes end to end, builds artifacts, and
    creates NO tag locally or on the remote; --execute is the single publish
    path and its prerequisites (e.g. `gh`) are checked BEFORE the first
    mutation, so a missing tool refuses with the tree still tag-free.

Red control: the pre-fix run of this file (driver absent) failed every arm
because the command could not execute — transcript preserved in
docs/releases/evidence/RELEASE-2026-11-03-001-evidence.md.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DRIVER = REPO / "scripts" / "release_driver.py"

#: build arms need the PEP 517 backend (setuptools) — the documented
#: procedure runs inside `make venv install`, where it is always present.
HAS_SETUPTOOLS = importlib.util.find_spec("setuptools") is not None


def _git_env():
    """Ambient-git-config isolation: scratch repos must not inherit the
    global core.hooksPath (fleet hooks fire on commits) or any identity."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def run_driver(*args, cwd=None, env=None):
    return subprocess.run(
        [sys.executable, str(DRIVER), *args],
        cwd=str(cwd) if cwd else str(REPO),
        capture_output=True,
        text=True,
        env=env if env is not None else os.environ,
    )


def scratch_repo(tmp_path):
    """A git repo with one commit on main and a bare origin holding it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git("init", "-b", "main", cwd=repo).returncode == 0
    (repo / "README.md").write_text("fixture\n")
    assert git("add", ".", cwd=repo).returncode == 0
    assert git("commit", "-m", "init", cwd=repo).returncode == 0
    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert git("init", "--bare", "-b", "main", cwd=origin).returncode == 0
    assert git("remote", "add", "origin", str(origin), cwd=repo).returncode == 0
    assert git("push", "-q", "origin", "main", cwd=repo).returncode == 0
    return repo, origin


def write_project_pins(
    root, version="0.3.0", changelog_headings=None, pyproject_version=None, init_version=None
):
    """Give a directory the three pin surfaces the driver validates."""
    pyproject_version = pyproject_version or version
    init_version = init_version or version
    headings = changelog_headings if changelog_headings is not None else [version]
    lines = ["# Changelog — fixture", ""]
    for h in headings:
        lines += [f"## [{h}] - 2026-11-09", "", "- fixture entry.", ""]
    (root / "CHANGELOG.md").write_text("\n".join(lines))
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        '[project]\nname = "task-router-fixture"\n'
        f'version = "{pyproject_version}"\n\n'
        '[tool.setuptools]\npackages = ["task_router"]\n'
    )
    pkg = root / "task_router"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{init_version}"\n')


def write_release_pins(
    repo, version="0.3.0", changelog_headings=None, pyproject_version=None, init_version=None
):
    """write_project_pins + commit (for git-tree fixtures)."""
    write_project_pins(repo, version, changelog_headings, pyproject_version, init_version)
    assert git("add", ".", cwd=repo).returncode == 0
    assert git("commit", "-m", "pins", cwd=repo).returncode == 0


@pytest.fixture()
def fixture_repo(tmp_path):
    """Clean scratch repo whose pins agree at 0.3.0, with an empty-of-tags origin."""
    repo, origin = scratch_repo(tmp_path)
    write_release_pins(repo)
    return repo, origin


# ---------------------------------------------------------------------------
# build: artifacts + verifiable checksums
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_SETUPTOOLS, reason="setuptools not installed — run `make venv install`")
def test_build_creates_wheel_sdist_and_verifiable_sums(tmp_path):
    write_project_pins(tmp_path)
    out = tmp_path / "dist"
    r = run_driver("build", "--project-dir", str(tmp_path), "--out-dir", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    artifacts = sorted(p.name for p in out.iterdir() if p.name != "SHA256SUMS")
    assert len(artifacts) == 2, artifacts
    assert any(n.endswith(".whl") for n in artifacts), artifacts
    assert any(n.endswith(".tar.gz") for n in artifacts), artifacts
    sums = (out / "SHA256SUMS").read_text().strip().splitlines()
    assert len(sums) == 2
    for line in sums:
        assert re.match(r"^[0-9a-f]{64}  \S+$", line), line
    # the recorded digests recompute against the artifacts on disk
    import hashlib

    for line in sums:
        digest, name = line.split(None, 1)
        assert hashlib.sha256((out / name.strip()).read_bytes()).hexdigest() == digest
    # and the standard tool agrees — the file is verifiable by consumers
    chk = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"], cwd=str(out), capture_output=True, text=True
    )
    assert chk.returncode == 0, chk.stdout + chk.stderr


@pytest.mark.skipif(not HAS_SETUPTOOLS, reason="setuptools not installed — run `make venv install`")
def test_verify_checksums_detects_tampering(tmp_path):
    write_project_pins(tmp_path)
    out = tmp_path / "dist"
    assert (
        run_driver("build", "--project-dir", str(tmp_path), "--out-dir", str(out)).returncode == 0
    )
    wheel = next(p for p in out.iterdir() if p.name.endswith(".whl"))
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    r = run_driver("verify-checksums", "--dir", str(out))
    assert r.returncode != 0
    assert "MISMATCH" in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# check: the five named refusals, each an executed non-zero exit
# ---------------------------------------------------------------------------


def test_check_passes_on_clean_matching_tagfree_repo(fixture_repo):
    repo, origin = fixture_repo
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_refuses_existing_local_tag(fixture_repo):
    repo, origin = fixture_repo
    assert git("tag", "-a", "v0.3.0", "-m", "x", cwd=repo).returncode == 0
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
    )
    assert r.returncode != 0
    assert "local tag" in r.stdout + r.stderr


def test_refuses_existing_remote_tag(fixture_repo):
    repo, origin = fixture_repo
    assert git("tag", "-a", "v0.3.0", "-m", "x", cwd=repo).returncode == 0
    # simulate the tag already having been pushed: push it, drop the local one
    assert git("push", "-q", "origin", "refs/tags/v0.3.0", cwd=repo).returncode == 0
    assert git("tag", "-d", "v0.3.0", cwd=repo).returncode == 0
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
    )
    assert r.returncode != 0
    assert "remote tag" in r.stdout + r.stderr


def test_refuses_unverifiable_remote(fixture_repo):
    """Fail-closed: a remote that cannot be read is a refusal, never 'no tag'."""
    repo, _origin = fixture_repo
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(tmp_path_nonexistent(repo)),
        "--landed-ref",
        "main",
    )
    assert r.returncode != 0
    assert "cannot verify remote" in r.stdout + r.stderr


def tmp_path_nonexistent(repo):
    return repo.parent / "does-not-exist.git"


def test_refuses_dirty_worktree(fixture_repo):
    repo, origin = fixture_repo
    (repo / "untracked.txt").write_text("dirt\n")
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
    )
    assert r.returncode != 0
    assert "dirty" in r.stdout + r.stderr
    # a modified tracked file dirties too
    (repo / "untracked.txt").unlink()
    (repo / "README.md").write_text("modified\n")
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
    )
    assert r.returncode != 0
    assert "dirty" in r.stdout + r.stderr


def test_refuses_non_ancestor_base(fixture_repo):
    repo, origin = fixture_repo
    assert git("checkout", "-q", "-b", "divergent", cwd=repo).returncode == 0
    (repo / "divergent.txt").write_text("side branch\n")
    assert git("add", ".", cwd=repo).returncode == 0
    assert git("commit", "-m", "diverge", cwd=repo).returncode == 0
    side_sha = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
        "--base",
        side_sha,
    )
    assert r.returncode != 0
    assert "not an ancestor" in r.stdout + r.stderr


def test_refuses_unresolvable_landed_ref(fixture_repo):
    repo, origin = fixture_repo
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "no-such-ref",
    )
    assert r.returncode != 0
    assert "cannot resolve" in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# check: version-pin validation against the newest CHANGELOG heading
# ---------------------------------------------------------------------------


def test_refuses_unpromoted_unreleased_heading(fixture_repo):
    repo, origin = fixture_repo
    write_release_pins(repo, changelog_headings=["Unreleased", "0.3.0"])
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
    )
    assert r.returncode != 0
    assert "Unreleased" in r.stdout + r.stderr


def test_refuses_mismatched_pins(fixture_repo):
    repo, origin = fixture_repo
    write_release_pins(repo, init_version="0.2.0")
    r = run_driver(
        "check",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
    )
    assert r.returncode != 0
    assert "version pin" in r.stdout + r.stderr


def test_refuses_tag_not_matching_pinned_version(fixture_repo):
    repo, origin = fixture_repo
    r = run_driver(
        "check",
        "--tag",
        "v0.9.9",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
    )
    assert r.returncode != 0
    assert "does not match" in r.stdout + r.stderr


def test_repo_pins_agree_right_now():
    """The repo's own pin surfaces agree today (dynamic — survives bumps)."""
    version = re.search(
        r'^version\s*=\s*"([^"]+)"', (REPO / "pyproject.toml").read_text(), re.M
    ).group(1)
    init_v = re.search(
        r'^__version__\s*=\s*"([^"]+)"', (REPO / "task_router" / "__init__.py").read_text(), re.M
    ).group(1)
    assert version == init_v
    headings = re.findall(r"^##\s*\[([^\]]+)\]", (REPO / "CHANGELOG.md").read_text(), re.M)
    newest_promoted = next(h for h in headings if h.strip().lower() != "unreleased")
    assert newest_promoted.strip() == version


# ---------------------------------------------------------------------------
# cut: dry-run creates nothing; execute-path prerequisites checked first
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_SETUPTOOLS, reason="setuptools not installed — run `make venv install`")
def test_dry_run_creates_no_tag_and_no_release(fixture_repo):
    repo, origin = fixture_repo
    r = run_driver(
        "cut",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
        "--project-dir",
        str(repo),
        "--out-dir",
        str(repo / "dist"),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no Release created" in r.stdout
    assert git("tag", "-l", cwd=repo).stdout.strip() == ""
    remote_refs = git("ls-remote", "--tags", str(origin), cwd=repo).stdout
    assert remote_refs.strip() == ""
    # the dry-run built real artifacts and a verifiable checksum file
    assert (repo / "dist" / "SHA256SUMS").exists()


def test_execute_path_refuses_before_mutation_when_gh_missing(fixture_repo):
    """--execute is the single publish path; its prerequisites gate BEFORE the
    first mutation — with gh unavailable, the tree stays tag-free."""
    repo, origin = fixture_repo
    bindir = repo.parent / "bin"
    bindir.mkdir()
    gitreal = subprocess.run(
        ["sh", "-c", "command -v git"], capture_output=True, text=True
    ).stdout.strip()
    (bindir / "git").symlink_to(gitreal)
    env = _git_env()
    env["PATH"] = str(bindir)
    r = run_driver(
        "cut",
        "--tag",
        "v0.3.0",
        "--repo",
        str(repo),
        "--remote",
        str(origin),
        "--landed-ref",
        "main",
        "--project-dir",
        str(repo),
        "--out-dir",
        str(repo / "dist"),
        "--execute",
        env=env,
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "gh" in out
    assert git("tag", "-l", cwd=repo).stdout.strip() == ""
    assert git("ls-remote", "--tags", str(origin), cwd=repo).stdout.strip() == ""
