#!/usr/bin/env python3
"""Is main's CI green? A gate a landing cannot ignore.

Why this exists: this repo ran three consecutive red CI runs while work kept
landing on main, and its own guard could not see it — the guard executes the
suite WITH this box's live state present, so it passes here and fails in CI.
The failure is environmental, and nothing in the repo ever asked CI itself.

Contract:
  exit 0 -> CI green (or gh unavailable and --require-green not set)
  exit 1 -> RED. Do not land more work on it.
  exit 2 -> usage/argument error

What "RED" means (TR-240): the newest COMPLETED run of the sha being checked —
OR, when that sha has no completed run yet (the normal case for a brand-new
commit), the newest completed run of the branch tip itself. A commit that CI
has not judged inherits the branch's verdict; "my sha is unjudged" must never
read as green while main is failing. In that fallback case the refusal names
the branch-tip run so an operator can inspect the run that is actually red.

Deliberate choices, stated so they are not mistaken for oversights:
  * gh missing or unauthenticated => SKIP, not fail. A network blip or a runner
    without credentials must never wedge the fleet. Skipping is announced.
  * A failed branch-tip lookup in the fallback case is the same SKIP (the state
    is genuinely unknowable), never a fabricated green.
  * --require-green turns that skip into a failure, for callers that would
    rather stop than proceed blind.
  * The escape hatch is an explicit env var, and it prints a warning. No gate in
    this repo may be overridden silently.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

REPO = "coding-hermes/task-router"
REPO_CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args: str) -> str:
    p = subprocess.run(["git", *args], cwd=REPO_CWD, capture_output=True, text=True)
    return p.stdout.strip()


def tip(branch: str) -> str:
    for ref in (f"origin/{branch}", branch, "HEAD"):
        sha = _git("rev-parse", ref)
        if sha:
            return sha
    return ""


def runs_for(sha: str, limit: int = 10) -> list[dict]:
    if not shutil.which("gh"):
        return []
    p = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            REPO,
            "--commit",
            sha,
            "--limit",
            str(limit),
            "--json",
            "conclusion,status,displayTitle,createdAt,databaseId,workflowName",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "gh run list failed").strip()[:300])
    return json.loads(p.stdout or "[]")


def _judge(sha: str, branch: str, quiet: bool, require_green: bool) -> int:
    """Decide one commit: 0 proceed, 1 refuse. May print a SKIP and return 0.

    Only the FIRST call may take the branch-tip fallback (TR-240): a fallback
    sha that is itself unjudged is a genuinely unknown state, not grounds to
    consult the tip twice.
    """
    try:
        runs = runs_for(sha)
    except Exception as e:  # gh missing/unauthenticated/offline -> announced skip
        print(
            f"CI GATE: SKIP — CI state unknowable ({e}). Proceeding.", file=sys.stderr
        )
        return 1 if require_green else 0

    completed = [r for r in runs if r.get("status") == "completed"]
    running = [r for r in runs if r.get("status") != "completed"]
    if not completed:
        note = f"{len(running)} run(s) in flight" if running else "no runs found"
        if sha != tip(branch):
            # TR-240: the pushed sha is unjudged, so it inherits the branch's
            # verdict — an unjudged sha must never read as green on a red main.
            tip_sha = tip(branch)
            if not tip_sha:
                print(
                    f"CI GATE: SKIP — {sha[:9]} unjudged and branch {branch!r} "
                    f"has no resolvable tip. Proceeding.",
                    file=sys.stderr,
                )
                return 1 if require_green else 0
            print(
                f"CI GATE: {sha[:9]} has no completed run ({note}) — checking "
                f"{branch} tip {tip_sha[:9]}.",
                file=sys.stderr,
            )
            return _judge(tip_sha, branch, quiet, require_green)
        print(
            f"CI GATE: {sha[:9]} has no completed run ({note}) — proceeding.",
            file=sys.stderr,
        )
        return 1 if require_green else 0

    newest = completed[0]
    concl = newest.get("conclusion")
    if concl == "success":
        if not quiet:
            print(
                f"CI GATE: OK — {REPO}@{sha[:9]} newest run is success "
                f"({newest.get('workflowName')}, run {newest.get('databaseId')})."
            )
        return 0

    where = f"@{sha[:9]}" + (" (branch tip)" if sha == tip(branch) else "")
    print(
        f"CI GATE: RED — {REPO}{where} newest completed run concluded {concl!r}.",
        file=sys.stderr,
    )
    print(
        f"          run {newest.get('databaseId')}: {newest.get('displayTitle')}",
        file=sys.stderr,
    )
    print(
        f"          Inspect: gh run view {newest.get('databaseId')} --repo {REPO} --log-failed",
        file=sys.stderr,
    )
    print(
        "          Landing more work on a red main is how three red runs became four.",
        file=sys.stderr,
    )
    if os.environ.get("CI_GATE_BYPASS") or os.environ.get("ALLOW_RED_PUSH"):
        which = (
            "CI_GATE_BYPASS" if os.environ.get("CI_GATE_BYPASS") else "ALLOW_RED_PUSH"
        )
        print(
            f"          OVERRIDDEN by {which}=1 — proceeding anyway. This should be rare and is logged.",
            file=sys.stderr,
        )
        return 0
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate a landing on main's CI state.")
    ap.add_argument("--branch", default="main")
    ap.add_argument(
        "--sha", default="", help="commit to check (default: the branch tip)"
    )
    ap.add_argument(
        "--require-green",
        action="store_true",
        help="fail (not skip) when CI state cannot be determined",
    )
    ap.add_argument("--quiet", action="store_true", help="silence the OK line")
    a = ap.parse_args()

    sha = a.sha or tip(a.branch)
    if not sha:
        msg = f"CI GATE: cannot resolve a commit for branch {a.branch!r}"
        print(msg, file=sys.stderr)
        return 1 if a.require_green else 0
    return _judge(sha, a.branch, a.quiet, a.require_green)


if __name__ == "__main__":
    sys.exit(main())
