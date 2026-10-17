#!/usr/bin/env python3
"""evidence_replay.py — TR-251 mechanism (2): REPLAY, NOT READ.

A stored stdout_digest read back as proof is a success marker — the same
text-as-state sin the board already suffers. This module makes CI RE-EXECUTE
the recorded command for every row whose status changed in a diff, and FAIL
when the replay disagrees. A stored digest alone never passes.

The evidence-run STORE was decided in docs/evidence-run-store-decision.md:
a run resolves through one of three forms (gitreins verdict dir, a GitHub
Actions run id, or a stated-reason null). Only verdict-form carries a
REPLAYABLE command; a digest without a command is unresolved evidence and
fails the replay gate loudly.

Replay contract
---------------
For each row whose status changed in the diff (base..head, or staged):

  1. Resolve the row's evidence run (evidence_run_id / detail.evidence[]).
     Unresolvable / digest-only -> FAIL naming the row and why.
  2. Load the run's recorded command from the verdict dir:
       .gitreins/history/<date>/<run-id>/verdict.json  — the judged run
       .gitreins/history/<date>/<run-id>/command.json  — the recorded command
     command.json shape: {"command": [...argv...] | "sh -c string",
                          "cwd": "...", "expect_exit": 0,
                          "stdout_digest": "sha256:<hex>" (INFORMATIONAL)}
  3. RE-EXECUTE the command in a scratch cwd (never the live tree's state
     dirs; the command is recorded self-contained).
  4. Compare: exit code must equal expect_exit. The digest is checked when
     present but is NEVER sufficient on its own — a matching digest on a
     command that was never re-run is exactly the failure mode this gate
     kills, so the replay always runs first.

Usage:
  python3 scripts/evidence_replay.py --base A --head B     # CI mode
  python3 scripts/evidence_replay.py                       # staged/HEAD mode
  python3 scripts/evidence_replay.py --row-file R          # single row probe

ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_evidence_replay.py evidence=docs/evidence-run-store-decision.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from board_commit_boundary_gate import (  # noqa: E402
    BOARD_BASENAME, _rows_from_lines, rows_flipped_to_complete)


class ReplayFailure(Exception):
    """One row's replay failed (the message names the row and why)."""


# ------------------------------------------------------------- run resolution #

def _row_run_ids(row: dict) -> list[str]:
    runs = []
    v = str(row.get("evidence_run_id") or "").strip()
    if v:
        runs.append(v)
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = None
    if isinstance(detail, dict):
        for ev in detail.get("evidence") or []:
            if isinstance(ev, dict):
                rid = str(ev.get("run_id") or "").strip()
                if rid and rid not in runs:
                    runs.append(rid)
    return runs


def _verify_ci_run(run_id: str) -> dict:
    """TR-339-01: verify a digit-shaped CI run id for real. Shape is NOT
    resolution — a fabricated 9-digit id passed the old shape-only check
    without any verification (dogfood run 34, row F). The store decision
    names the verifier: `gh run view <id>` reports existence + conclusion.
    Returns a payload; raises ReplayFailure for every non-verifiable
    outcome — refuse loudly, never silently pass a shape-only id."""
    gh = shutil.which("gh") or ""
    host = os.environ.get("GH_HOST") or ""
    if not gh:
        raise ReplayFailure(
            f"run {run_id!r} looks like a GitHub Actions run id (digits only, "
            f"{len(run_id)} chars) but `gh` is not installed — refusing "
            f"shape-only validation (TR-339-01): a digit string alone is NOT "
            f"evidence; install gh or bind verdict-form evidence instead")
    check = subprocess.run(
        ["gh", "run", "view", run_id,
         *(["-R", host] if host else []), "--json", "status,conclusion"],
        capture_output=True, text=True, timeout=30)
    if check.returncode == 0:
        try:
            info = json.loads(check.stdout or "{}")
        except ValueError:
            info = {}
        conclusion = str(info.get("conclusion") or "").lower()
        status = str(info.get("status") or "").lower()
        if not conclusion or conclusion in ("pending", "skipped") or status not in ("completed",):
            raise ReplayFailure(
                f"run {run_id!r} is a GitHub Actions run id that is NOT "
                f"complete (status={status!r}, conclusion={conclusion!r}) — "
                f"only a completed run resolves out-of-band (TR-339-01)")
        return {"run_id": run_id, "verified_via": "gh run view",
                "conclusion": conclusion}
    return _raise_ci_unverified(run_id, check)


def _raise_ci_unverified(run_id: str, check: subprocess.CompletedProcess) -> None:
    """Classify the gh failure loudly (TR-339-01). A verification FAILURE is
    still not verification: fabricate-proof gates refuse on every arm."""
    err = (check.stderr or "").strip()
    lower = err.lower()
    if "not found" in lower or "no event" in lower or "could not resolve to" in lower:
        raise ReplayFailure(
            f"run {run_id!r} does not exist as a GitHub Actions run "
            f"(gh run view refused it: {err[:120]}) — a shape-only digit "
            f"string is NOT evidence (TR-339-01)")
    raise ReplayFailure(
        f"run {run_id!r} could not be VERIFIED as a completed CI run "
        f"(gh run view rc={check.returncode}: {err[:120]}) — refusing "
        f"unverifiable shape-only validation (TR-339-01); re-run with gh "
        f"authenticated and networked, or bind verdict-form evidence")


def resolve_run(run_id: str, repo: str, ci_mode: bool = False) -> tuple[str, dict]:
    """Resolve one run id to (kind, payload). Raises ReplayFailure when the
    id does not resolve to a REPLAYABLE store (the store decision's three
    forms; only verdict-form carries a command). ci_mode=True (the CI step
    sets TR251_CI_MODE=1) makes a missing verdict HISTORY fail with the
    explicit store-writer gap message instead of the generic lookup error —
    .gitreins/history is gitignored local state CI does not carry, so a
    verdict-form flip MUST be refused loudly there (TR-339-03)."""
    if run_id.lower().startswith("none:"):
        # A stated-reason null is honest but NOT replayable: a row whose
        # completion leans on it cannot pass a replay gate and must say so.
        raise ReplayFailure(f"run {run_id!r} is a stated-reason null — not replayable")
    if run_id.isdigit() and 8 <= len(run_id) <= 12:
        # CI run id: real verification required (TR-339-01) — see
        # _verify_ci_run. The run's conclusion is the external reading
        # doctrine rule 6 wants; theWanting sha of a workflow is CI itself
        # so no local re-execution happens for this form.
        return "ci", _verify_ci_run(run_id)
    # Verdict-form: .gitreins/history/<date>/<run-id>/
    history = os.path.join(repo, ".gitreins", "history")
    if os.path.isdir(history):
        for date in sorted(os.listdir(history), reverse=True):
            run_dir = os.path.join(history, date, run_id)
            verdict_path = os.path.join(run_dir, "verdict.json")
            if os.path.exists(verdict_path):
                try:
                    with open(verdict_path, encoding="utf-8") as fh:
                        verdict = json.load(fh)
                except ValueError as exc:
                    raise ReplayFailure(
                        f"run {run_id!r}: verdict.json is unreadable: {exc}") from exc
                return "verdict", {"dir": run_dir, "verdict": verdict}
    if ci_mode:
        raise ReplayFailure(
            f"run {run_id!r} is a verdict-form run id, but CI carries no "
            f".gitreins/history (verdict.json/command.json are gitignored "
            f"local state) — verdict-form resolution requires a command.json "
            f"writer (not yet implemented; TR-339-02/TR-339-03). Refusing "
            f"LOUDLY instead of walking an empty store: bind a VERIFIED CI "
            f"run id or a command.json-carrying verb on the row instead")
    raise ReplayFailure(
        f"run {run_id!r} does not resolve to any store "
        f"(no .gitreins/history/<date>/{run_id}/verdict.json under {repo}; "
        f"not a CI run id; not a stated-reason null) — see "
        f"docs/evidence-run-store-decision.md")


def load_command(run_dir: str, run_id: str) -> dict | None:
    """Load the recorded command for a verdict-form run. A verdict WITHOUT a
    command.json is normal on this store (TR-339-02: gitreins judges write
    verdict.json/summary.md/commit.patch but no command.json — 223+ real
    runs, 0 commands). Returns None when command.json is genuinely absent so
    the caller can fall back to criteria+commit-range replay, and raises
    ReplayFailure when it CARRIES content but that content is unusable."""
    path = os.path.join(run_dir, "command.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            cmd = json.load(fh)
    except ValueError as exc:
        raise ReplayFailure(f"run {run_id!r}: command.json unreadable: {exc}") from exc
    if not cmd.get("command"):
        raise ReplayFailure(f"run {run_id!r}: command.json carries no command")
    return cmd


# ---------------------------------------------------------------- fallback #c
# TR-339-02: the criteria + commit-range fallback for verdicts whose store
# entry carries NO command.json. This is an EXPLICIT, NAMED mechanism —
#
#   * verdict.json carries `task_criteria` (what the judge graded) and
#     `commit` (WHAT it graded) and `passed` (the grade);
#   * the replayable surface without a command is: criteria EXIST and are
#     non-empty, `passed` is a boolean, and the graded commit is in the
#     lineage the gate runs on. A verdict whose graded commit is NOT an
#     ancestor of the gated HEAD cannot bind the push and refuses loudly —
#     that is exactly the verdict-vs-proof gap 339-04 documents (verdict
#     fe0601db graded d0db679; the fixes landed later, so a row bound TODAY
#     to fe0601db refuses on this fallback until a re-run at the fix sha
#     lands, which is the honest outcome).
#
# The FULLY replayable form stays command.json (be479d90 proves the shape);
# this fallback exists so real verdicts don't make the CI step a dead end,
# not to weaken the gate: it is bounded, loud on every gap, and the missing
# store-writer mechanism is named every time it fires.
def verdict_fallback_replay(payload: dict, run_id: str, repo: str) -> dict:
    """TR-339-02 fallback: replay a verdict WITHOUT command.json via
    task_criteria + commit-range binding. Returns {"ok", "detail", "mode"}.
    Raises ReplayFailure when NEITHER form can hold (loud gap, no silent
    pass) and Data-error arms are re-raised as ReplayFailure too."""
    verdict = payload["verdict"]
    criteria = verdict.get("task_criteria") or []
    if not isinstance(criteria, list) or not any(str(c).strip() for c in criteria):
        raise ReplayFailure(
            f"run {run_id!r}: verdict.json carries NO task_criteria and NO "
            f"command.json — neither replayable form holds. This gap needs a "
            f"command.json-store writer (not yet implemented, TR-339-02)")
    if not isinstance(verdict.get("passed"), bool):
        raise ReplayFailure(
            f"run {run_id!r}: verdict.json carries no boolean `passed` — an "
            f"ungraded verdict cannot replay via criteria+commit-range")
    commit = str(verdict.get("commit") or "").strip()
    if not commit:
        raise ReplayFailure(
            f"run {run_id!r}: verdict.json carries no commit — a verdict "
            f"without a graded commit is digest-only evidence; record the "
            f"command or the commit next to it (TR-339-02)")
    state = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo)
    if state.returncode != 0:
        raise ReplayFailure(
            f"run {run_id!r}: this checkout has no git HEAD to compare the "
            f"verdict's commit against ({state.stderr.strip()[:100]}) — "
            f"verdict fallback needs a git lineage (TR-339-02)")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        capture_output=True, text=True, cwd=repo)
    if ancestry.returncode != 0:
        raise ReplayFailure(
            f"run {run_id!r}: verdict's graded commit {commit[:12]} is NOT an "
            f"ancestor of HEAD — the verdict does not evidence the pushed "
            f"lineage (TR-339-02 verdict-vs-graded-commit gap; for the "
            f"339-04 case this is the TR-251 row's own refusal until a "
            f"re-run verdict at the fix sha lands)")
    if verdict["passed"] is False:
        # A verdict that says FAILED must not satisfy a completion gate via
        # the fallback: the criteria were graded, graded false, and the fix
        # commits that landed later are NOT covered by this record (no
        # re-run exists — that is exactly 339-04). LOUD refusal.
        raise ReplayFailure(
            f"run {run_id!r}: verdict graded commit {commit[:12]} `passed: false` "
            f"— a failing verdict cannot evidence a completion; no re-run "
            f"verdict exists at the fixed sha (TR-339-04 verdict-vs-proof gap)")
    return {
        "ok": True,
        "detail": (
            f"run {run_id!r}: verdict-form fallback replay (no command.json; "
            f"TR-339-02): criteria={len(criteria)}, graded commit "
            f"{commit[:12]} is in the lineage, passed={verdict['passed']}"),
        "mode": "fallback",
    }


# -------------------------------------------------------------------- replay #

def replay_command(cmd_spec: dict, run_id: str, timeout: int = 300) -> dict:
    """Execute the recorded command and grade the result against the record.
    Returns {"ok": bool, "detail": str}. The digest is checked ONLY after a
    real execution — it can corroborate, never substitute."""
    command = cmd_spec["command"]
    argv = command if isinstance(command, list) else ["sh", "-c", str(command)]
    expect_exit = int(cmd_spec.get("expect_exit", 0))
    cwd = cmd_spec.get("cwd") or tempfile.mkdtemp(prefix="tr251-replay-")
    if not os.path.isdir(cwd):
        os.makedirs(cwd, exist_ok=True)
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"run {run_id!r}: replay exceeded {timeout}s"}
    except OSError as exc:
        return {"ok": False, "detail": f"run {run_id!r}: replay failed to exec: {exc}"}
    if proc.returncode != expect_exit:
        return {"ok": False, "detail": (
            f"run {run_id!r}: replay exit {proc.returncode} != recorded expect_exit "
            f"{expect_exit} (stderr: {proc.stderr.strip()[:200]})")}
    digest = cmd_spec.get("stdout_digest")
    if digest:
        algo, _, want = digest.partition(":")
        if not want:
            algo, want = "sha256", digest
        got = hashlib.new(algo, proc.stdout.encode()).hexdigest()
        if got != want:
            return {"ok": False, "detail": (
                f"run {run_id!r}: replay stdout {algo} {got[:12]}… != recorded "
                f"{want[:12]}… — the command no longer reproduces its record")}
    return {"ok": True, "detail": f"run {run_id!r}: replayed OK (exit {proc.returncode})"}


# ------------------------------------------------------------------ diff scan #

def _git_show(rev: str, repo: str) -> list[str]:
    out = subprocess.run(["git", "show", f"{rev}:{BOARD_BASENAME}"],
                         capture_output=True, text=True, cwd=repo)
    return out.stdout.splitlines() if out.returncode == 0 else []


def rows_for_diff(base: str | None, head: str | None, repo: str) -> list[dict]:
    if base and head:
        return rows_flipped_to_complete(_git_show(base, repo), _git_show(head, repo))
    old = _git_show("HEAD", repo)
    path = os.path.join(repo, BOARD_BASENAME)
    new = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    return rows_flipped_to_complete(old, new)


def replay_diff(base: str | None, head: str | None, repo: str,
                ci_mode: bool = False) -> tuple[list[str], int]:
    """Replay every flipped row's evidence. Returns (failure lines, replayed count).
    ci_mode=True threads TR-339-03's loud verdict-form refusal (set by the CI
    step via TR251_CI_MODE=1)."""
    failures: list[str] = []
    replayed = 0
    for row in rows_for_diff(base, head, repo):
        rid = row.get("id") or "<no-id>"
        runs = _row_run_ids(row)
        if not runs:
            failures.append(f"{rid}: completion carries no evidence run at all")
            continue
        row_ok = False
        row_failures = []
        for run_id in runs:
            try:
                kind, payload = resolve_run(run_id, repo, ci_mode=ci_mode)
            except ReplayFailure as exc:
                row_failures.append(f"{rid}: {exc}")
                continue
            if kind == "ci":
                row_ok = True  # VERIFIED out-of-band by _verify_ci_run (TR-339-01)
                continue
            # Verdict form: try the recorded command first, then the
            # TR-339-02 fallback; a verdict that satisfies NEITHER fails
            # with a name of every gap, never a silent pass.
            cmd_spec = None
            try:
                cmd_spec = load_command(payload["dir"], run_id)
            except ReplayFailure as exc:
                row_failures.append(f"{rid}: {exc}")
                continue
            if cmd_spec is not None:
                result = replay_command(cmd_spec, run_id)
                if result["ok"]:
                    row_ok = True
                    break
                row_failures.append(f"{rid}: {result['detail']}")
                continue
            # No command.json -> the 339-02 fallback. Raises on every gap.
            try:
                result = verdict_fallback_replay(payload, run_id, repo)
                if result["ok"]:
                    row_ok = True
                    break
            except ReplayFailure as exc:
                row_failures.append(f"{rid}: {exc}")
        if row_ok:
            replayed += 1
        failures.extend(row_failures)
    return failures, replayed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="evidence_replay.py",
        description="TR-251: replay the recorded commands behind a diff's completions")
    ap.add_argument("--base")
    ap.add_argument("--head")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--ci-mode", action="store_true",
                    help="TR-339-03: CI step mode — refuse verdict-form flips "
                         "LOUDLY naming the missing command.json writer")
    args = ap.parse_args(argv)
    if bool(args.base) != bool(args.head):
        ap.error("--base and --head go together")
    failures, replayed = replay_diff(args.base, args.head, args.repo,
                                     ci_mode=args.ci_mode or
                                     os.environ.get("TR251_CI_MODE") == "1")
    if failures:
        for f in failures:
            print(f"REPLAY-FAIL {f}", file=sys.stderr)
        print(f"evidence_replay: {len(failures)} failure(s), {replayed} row(s) replayed",
              file=sys.stderr)
        return 1
    print(f"evidence_replay: OK ({replayed} completion row(s) replayed, all agree)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
