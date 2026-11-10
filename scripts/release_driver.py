#!/usr/bin/env python3
"""Release driver for task-router (RELEASE-2026-11-03-001).

One in-repo, fail-closed command surface from "release-ready tree" to
"published tag + GitHub Release". It replaces the hand-derived pre-cut
checklist in docs/releases/release-readiness.md — the documented procedure
now invokes `make release-check` / `make release-build` /
`make release-dry-run` (spec anchor: docs/releases/release-readiness.md#driver).

Doctrine:
  * REFUSALS are the product. Every preflight is an executed, read-only
    command and is fail-closed: a check that cannot run (unresolvable ref,
    dead remote, absent tool) is a refusal with explicit wording and a
    non-zero exit — never a silent pass.
  * DRY-RUN IS THE DEFAULT. `cut` without --execute runs every check, builds
    every artifact, and prints the publish plan. It creates no tag, pushes
    nothing, and creates no Release.
  * ONE PUBLISH PATH. `cut --execute` is the repo's only release mechanism:
    annotated tag at --base, tag push to origin, then `gh release create`
    with the promoted CHANGELOG section as notes and dist/ + SHA256SUMS as
    assets. The deliberately unused alternative — a tag-triggered GitHub
    Actions workflow — does not exist: .github/workflows/ci.yml fires on
    push-main / pull_request / workflow_dispatch only, and a tag push
    triggers nothing by design.

Refused (exit 2, explicit wording) by `check` and `cut`:
  * existing local tag                          (`git tag --list`)
  * existing remote tag                         (read-only `git ls-remote --tags`)
  * remote that cannot be verified              (fail-closed)
  * dirty worktree                              (`git status --porcelain`)
  * --base not an ancestor of --landed-ref      (`git merge-base --is-ancestor`)
  * unresolvable --base / --landed-ref          (`git rev-parse --verify`)
  * [Unreleased] as the newest CHANGELOG heading (promote before cutting)
  * version pins disagreeing with the newest CHANGELOG heading
    (pyproject.toml `project.version` == task_router/__init__.py
    `__version__` == newest `## [x.y.z]` heading)
  * --tag not equal to "v" + pinned version

Exit codes: 0 = passed / dry-run complete; 2 = refusal; 3 = environment or
tool failure (missing interpreter feature, build backend, gh); anything else
is an unexpected bug.

This script is an OPERATOR tool. It is not imported by the runtime package
and changes no serving behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.version_info < (3, 11):
    print(
        "ERROR: release_driver.py needs Python 3.11+ (tomllib); got "
        f"{sys.version_info.major}.{sys.version_info.minor}",
        file=sys.stderr,
    )
    sys.exit(3)

import tomllib  # noqa: E402  (after the version gate)

#: The newest CHANGELOG heading must be a promoted version, never this.
UNRELEASED_HEADING = "unreleased"

SUMS_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")


class Refusal(Exception):
    """A fail-closed preflight refusal — the named release conditions."""


class ToolError(Exception):
    """The environment cannot run the release (missing tool/backend)."""


# ---------------------------------------------------------------------------
# subprocess plumbing — every check below is an executed command
# ---------------------------------------------------------------------------


def run(argv, cwd):
    return subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True)


def git(repo, *args):
    return run(["git", "-C", str(repo), *args], cwd=str(repo))


def resolve_commit(repo, ref):
    p = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if p.returncode != 0 or not p.stdout.strip():
        raise Refusal(
            f"cannot resolve '{ref}' to a commit (git rev-parse failed: "
            f"{(p.stderr or p.stdout).strip() or 'unknown revision'})"
        )
    return p.stdout.strip()


# ---------------------------------------------------------------------------
# preflight checks — each names its refusal explicitly
# ---------------------------------------------------------------------------


def check_version_pins(repo, tag):
    """pyproject version == __init__ __version__ == newest CHANGELOG heading,
    and --tag is that version with a leading v."""
    pyproject_path = repo / "pyproject.toml"
    init_path = repo / "task_router" / "__init__.py"
    changelog_path = repo / "CHANGELOG.md"
    for path in (pyproject_path, init_path, changelog_path):
        if not path.is_file():
            raise Refusal(f"version pin surface missing: {path}")

    try:
        with open(pyproject_path, "rb") as fh:
            pyproject_version = tomllib.load(fh)["project"]["version"]
    except (KeyError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"cannot read version pin from {pyproject_path}: {exc}")

    init_text = init_path.read_text()
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_text, re.M)
    if not m:
        raise Refusal(f"cannot read version pin from {init_path}: no __version__ assignment found")
    init_version = m.group(1)

    changelog = changelog_path.read_text()
    heading = re.search(r"^##\s*\[([^\]]+)\]", changelog, re.M)
    if not heading:
        raise Refusal(f"cannot read version pin from {changelog_path}: no '## [...]' heading found")
    newest = heading.group(1).strip()
    if newest.lower() == UNRELEASED_HEADING:
        raise Refusal(
            "CHANGELOG's newest heading is [Unreleased] — promote it to "
            "[<version>] - <date> and bump both version pins before cutting"
        )

    problems = []
    if pyproject_version != newest:
        problems.append(
            f"pyproject.toml version={pyproject_version!r} != newest CHANGELOG heading [{newest}]"
        )
    if init_version != newest:
        problems.append(
            f"task_router/__init__.py __version__={init_version!r} != "
            f"newest CHANGELOG heading [{newest}]"
        )
    expected_tag = f"v{newest}"
    if tag != expected_tag:
        problems.append(
            f"--tag {tag!r} does not match pinned version "
            f"{newest!r} (expected tag {expected_tag!r})"
        )
    if problems:
        raise Refusal("version pin mismatch: " + "; ".join(problems))
    print(
        f"ok: version pins agree (pyproject={pyproject_version} "
        f"__init__={init_version} CHANGELOG newest=[{newest}] tag={tag})"
    )
    return newest


def check_tree_clean(repo):
    p = git(repo, "status", "--porcelain")
    if p.returncode != 0:
        raise Refusal(f"cannot verify worktree state: {p.stderr.strip()}")
    if p.stdout.strip():
        dirt = "\n".join(p.stdout.strip().splitlines()[:10])
        raise Refusal(
            f"worktree is dirty ({len(p.stdout.strip().splitlines())} "
            f"entries) — commit or stash first:\n{dirt}"
        )
    print("ok: worktree clean (git status --porcelain empty)")


def check_local_tag_absent(repo, tag):
    p = git(repo, "tag", "--list", tag)
    if p.returncode != 0:
        raise Refusal(f"cannot list local tags: {p.stderr.strip()}")
    if p.stdout.strip():
        raise Refusal(f"local tag '{tag}' already exists — refusing to cut over an existing tag")
    print(f"ok: local tag absent ({tag})")


def check_remote_tag_absent(repo, tag, remote):
    # Read-only verification: `git ls-remote --tags` queries without writing.
    p = git(repo, "ls-remote", "--tags", remote, f"refs/tags/{tag}")
    if p.returncode != 0:
        raise Refusal(
            f"cannot verify remote tags on '{remote}' — fail-closed, refusing "
            f"to assume the tag is absent ({(p.stderr or p.stdout).strip()})"
        )
    if p.stdout.strip():
        raise Refusal(
            f"remote tag '{tag}' already exists on '{remote}' — "
            "refusing to cut over an existing tag"
        )
    print(f"ok: remote tag absent ({tag} on {remote}; read-only ls-remote)")


def check_base_landed(repo, base_ref, landed_ref):
    base_sha = resolve_commit(repo, base_ref)
    landed_sha = resolve_commit(repo, landed_ref)
    p = git(repo, "merge-base", "--is-ancestor", base_sha, landed_sha)
    if p.returncode != 0:
        raise Refusal(
            f"base {base_sha[:12]} is not an ancestor of {landed_ref} "
            f"({landed_sha[:12]}) — tag only commits already landed on "
            f"{landed_ref}"
        )
    print(f"ok: base {base_sha[:12]} is an ancestor of {landed_ref} ({landed_sha[:12]})")
    return base_sha, landed_sha


def run_preflight(repo, tag, remote, landed_ref, base_ref="HEAD"):
    version = check_version_pins(repo, tag)
    check_tree_clean(repo)
    check_local_tag_absent(repo, tag)
    check_remote_tag_absent(repo, tag, remote)
    base_sha, landed_sha = check_base_landed(repo, base_ref, landed_ref)
    return version, base_sha, landed_sha


# ---------------------------------------------------------------------------
# build — PEP 517 hooks in-process (no network, no build isolation needed)
# ---------------------------------------------------------------------------


def build_artifacts(project_dir: Path, out_dir: Path):
    try:
        import setuptools.build_meta as build_meta
    except ImportError as exc:
        raise ToolError(
            "setuptools is not importable — the PEP 517 build backend is "
            "required. Run `make venv install` (python3.11 venvs bundle it) "
            f"or install it explicitly: pip install setuptools ({exc})"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = os.getcwd()
    os.chdir(project_dir)
    try:
        sdist_name = build_meta.build_sdist(str(out_dir.resolve()))
        wheel_name = build_meta.build_wheel(str(out_dir.resolve()))
    finally:
        os.chdir(saved)
    artifacts = [out_dir / sdist_name, out_dir / wheel_name]
    for path in artifacts:
        if not path.is_file():
            raise ToolError(f"build reported '{path.name}' but it is absent")
    return artifacts


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sums(out_dir: Path, artifacts) -> Path:
    lines = [f"{sha256_file(p)}  {p.name}" for p in sorted(artifacts, key=lambda p: p.name)]
    sums = out_dir / "SHA256SUMS"
    sums.write_text("\n".join(lines) + "\n")
    return sums


def verify_sums(out_dir: Path):
    """Recompute every digest in SHA256SUMS against the artifacts on disk."""
    sums = out_dir / "SHA256SUMS"
    if not sums.is_file():
        raise ToolError(f"{sums} missing — nothing to verify")
    bad = []
    seen = []
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        m = SUMS_LINE.match(line)
        if not m:
            bad.append(f"unparseable line: {line!r}")
            continue
        digest, name = m.groups()
        artifact = out_dir / name
        if not artifact.is_file():
            bad.append(f"MISSING: {name} (recorded in SHA256SUMS, absent on disk)")
            continue
        actual = sha256_file(artifact)
        if actual != digest:
            bad.append(f"MISMATCH: {name} recorded={digest} actual={actual}")
        seen.append(name)
    if bad:
        raise ToolError("SHA256SUMS verification failed:\n  " + "\n  ".join(bad))
    if not seen:
        raise ToolError("SHA256SUMS names no artifacts")
    return seen


# ---------------------------------------------------------------------------
# CHANGELOG section (release notes for the execute path)
# ---------------------------------------------------------------------------


def changelog_section(repo: Path, version: str) -> str:
    text = (repo / "CHANGELOG.md").read_text()
    m = re.search(rf"^##\s*\[{re.escape(version)}\][^\n]*\n(.*?)(?=^##\s*\[|\Z)", text, re.M | re.S)
    if not m or not m.group(1).strip():
        raise ToolError(
            f"no CHANGELOG section found for [{version}] — release notes would be empty"
        )
    return m.group(1).strip()


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_check(args):
    repo = Path(args.repo).resolve()
    run_preflight(repo, args.tag, args.remote, args.landed_ref, args.base or "HEAD")
    print(f"CHECK PASSED: {args.tag} is cuttable from {repo} (nothing was written)")
    return 0


def cmd_build(args):
    project_dir = Path(args.project_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    if not (project_dir / "pyproject.toml").is_file():
        raise ToolError(f"no pyproject.toml in {project_dir}")
    artifacts = build_artifacts(project_dir, out_dir)
    sums = write_sums(out_dir, artifacts)
    verified = verify_sums(out_dir)
    print(f"BUILD OK: {len(artifacts)} artifacts + {sums.name} in {out_dir}")
    for p in artifacts:
        print(f"  {p.name}  {p.stat().st_size} bytes")
    print(f"verified: {', '.join(verified)} — digests recompute")
    return 0


def cmd_verify_checksums(args):
    out_dir = Path(args.dir).resolve()
    verified = verify_sums(out_dir)
    print(f"CHECKSUMS OK: {', '.join(verified)} match SHA256SUMS in {out_dir}")
    return 0


def cmd_cut(args):
    repo = Path(args.repo).resolve()
    project_dir = Path(args.project_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    version, base_sha, landed_sha = run_preflight(
        repo, args.tag, args.remote, args.landed_ref, args.base or "HEAD"
    )
    changelog_section(repo, version)  # refuse before building if notes are empty
    # Check execute-only publishing prerequisites before any build side effect.
    # A missing release client must be reported as such, not masked by a
    # backend/build-environment error.
    if args.execute and not shutil.which("gh"):
        raise ToolError(
            "gh is not available on PATH — the GitHub Release leg "
            "cannot run; refusing to tag a half-publishable cut"
        )
    print(f"ok: CHANGELOG section for [{version}] extractable")
    print(f"== build: wheel + sdist + SHA256SUMS -> {out_dir}")
    artifacts = build_artifacts(project_dir, out_dir)
    sums = write_sums(out_dir, artifacts)
    verified = verify_sums(out_dir)
    print(f"ok: built + verified: {', '.join(verified + [sums.name])}")

    tag_cmd = [
        "git",
        "-C",
        str(repo),
        "tag",
        "-a",
        args.tag,
        "-m",
        f"task-router {args.tag}",
        base_sha,
    ]
    push_cmd = ["git", "-C", str(repo), "push", args.remote, f"refs/tags/{args.tag}"]
    assets = [str(p) for p in sorted(out_dir.iterdir())]
    release_cmd = [
        "gh",
        "release",
        "create",
        args.tag,
        "--verify-tag",
        "--title",
        f"task-router {args.tag}",
        "--notes-file",
        "<notes>",
        *assets,
    ]

    if not args.execute:
        print()
        print("PUBLISH PLAN (dry-run — NOT executed):")
        for cmd in (tag_cmd, push_cmd, release_cmd):
            print("  " + " ".join(cmd))
        print()
        print(
            "DRY-RUN complete — no tag created, nothing pushed, "
            "no Release created. To publish (the repo's ONE publish path): "
            "re-run with --execute."
        )
        return 0

    # ---- execute path: the single publish path ---------------------------
    # Prerequisites are verified BEFORE the first mutation, so a missing
    # tool refuses with the tree still tag-free (all-or-nothing preflight).
    if not shutil.which("gh"):
        raise ToolError(
            "gh is not available on PATH — the GitHub Release leg "
            "cannot run; refusing to tag a half-publishable cut"
        )
    if not shutil.which("git"):
        raise ToolError("git is not available on PATH")
    notes = changelog_section(repo, version)

    print(f"== EXECUTE: tagging {args.tag} at {base_sha[:12]}")
    p = run(tag_cmd, cwd=str(repo))
    if p.returncode != 0:
        raise ToolError(f"git tag failed: {(p.stderr or p.stdout).strip()}")
    print(f"== EXECUTE: pushing {args.tag} to {args.remote}")
    p = run(push_cmd, cwd=str(repo))
    if p.returncode != 0:
        raise ToolError(f"git push of tag failed: {(p.stderr or p.stdout).strip()}")
    print(f"== EXECUTE: gh release create {args.tag} ({len(assets)} assets incl. SHA256SUMS)")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
        tf.write(notes)
        notes_path = tf.name
    try:
        p = run(
            [
                "gh",
                "release",
                "create",
                args.tag,
                "--verify-tag",
                "--title",
                f"task-router {args.tag}",
                "--notes-file",
                notes_path,
                *assets,
            ],
            cwd=str(repo),
        )
    finally:
        os.unlink(notes_path)
    if p.returncode != 0:
        raise ToolError(f"gh release create failed: {(p.stderr or p.stdout).strip()}")
    print(
        f"PUBLISHED: {args.tag} tagged at {base_sha[:12]}, pushed to "
        f"{args.remote}, GitHub Release created with {len(assets)} assets"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common(p):
    p.add_argument(
        "--tag", required=True, help="the release tag, e.g. v0.3.0 (must equal 'v'+pinned version)"
    )
    p.add_argument("--repo", default=".", help="the task-router checkout (default: cwd)")
    p.add_argument(
        "--remote", default="origin", help="git remote whose tags are checked (default: origin)"
    )
    p.add_argument(
        "--landed-ref",
        default="origin/main",
        help="--base must be an ancestor of this ref (default: origin/main)",
    )
    p.add_argument("--base", default=None, help="commit to tag (default: HEAD)")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="release_driver.py",
        description="task-router release driver (RELEASE-2026-11-03-001) — "
        "fail-closed checks, dry-run by default, one publish path.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="run every preflight refusal (read-only)")
    add_common(p_check)
    p_check.set_defaults(func=cmd_check, base_default="HEAD")

    p_build = sub.add_parser("build", help="build wheel + sdist + SHA256SUMS")
    p_build.add_argument(
        "--project-dir", default=".", help="project root holding pyproject.toml (default: cwd)"
    )
    p_build.add_argument("--out-dir", default="dist", help="artifact output dir (default: dist/)")
    p_build.set_defaults(func=cmd_build)

    p_verify = sub.add_parser("verify-checksums", help="recompute SHA256SUMS digests against dist/")
    p_verify.add_argument(
        "--dir", default="dist", help="dir holding SHA256SUMS + artifacts (default: dist/)"
    )
    p_verify.set_defaults(func=cmd_verify_checksums)

    p_cut = sub.add_parser("cut", help="checks + build + publish plan; --execute to publish")
    add_common(p_cut)
    p_cut.add_argument(
        "--project-dir", default=None, help="project root for the build (default: --repo)"
    )
    p_cut.add_argument("--out-dir", default=None, help="artifact output dir (default: <repo>/dist)")
    p_cut.add_argument(
        "--execute",
        action="store_true",
        help="ACTUALLY tag, push the tag and create the GitHub "
        "Release (the repo's one publish path). Default is "
        "a dry-run that creates nothing.",
    )
    p_cut.set_defaults(func=cmd_cut)

    args = ap.parse_args(argv)
    if args.cmd == "cut":
        if args.project_dir is None:
            args.project_dir = args.repo
        if args.out_dir is None:
            args.out_dir = str(Path(args.repo).resolve() / "dist")
    try:
        return args.func(args)
    except Refusal as exc:
        print(f"REFUSAL: {exc}", file=sys.stderr)
        return 2
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
