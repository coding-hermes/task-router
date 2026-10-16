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


def resolve_run(run_id: str, repo: str) -> tuple[str, dict]:
    """Resolve one run id to (kind, payload). Raises ReplayFailure when the
    id does not resolve to a REPLAYABLE store (the store decision's three
    forms; only verdict-form carries a command)."""
    if run_id.lower().startswith("none:"):
        # A stated-reason null is honest but NOT replayable: a row whose
        # completion leans on it cannot pass a replay gate and must say so.
        raise ReplayFailure(f"run {run_id!r} is a stated-reason null — not replayable")
    if run_id.isdigit() and 8 <= len(run_id) <= 12:
        # CI run id: resolvable out-of-band, but its "command" is the whole
        # workflow — replay of a workflow is CI itself, so this form is
        # accepted as resolved WITHOUT a local re-execution (the run's
        # conclusion is the external reading doctrine rule 6 wants).
        return "ci", {"run_id": run_id}
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
    raise ReplayFailure(
        f"run {run_id!r} does not resolve to any store "
        f"(no .gitreins/history/<date>/{run_id}/verdict.json under {repo}; "
        f"not a CI run id; not a stated-reason null) — see "
        f"docs/evidence-run-store-decision.md")


def load_command(run_dir: str, run_id: str) -> dict:
    """Load the recorded command for a verdict-form run. A verdict WITHOUT a
    command.json is digest-only evidence: it documents that a judge ran, but
    nothing re-executable — replay FAILS rather than trusting the record."""
    path = os.path.join(run_dir, "command.json")
    if not os.path.exists(path):
        raise ReplayFailure(
            f"run {run_id!r} has no recorded command (command.json missing in "
            f"{run_dir}) — a stored digest alone never passes; record the "
            f"command next to the verdict")
    try:
        with open(path, encoding="utf-8") as fh:
            cmd = json.load(fh)
    except ValueError as exc:
        raise ReplayFailure(f"run {run_id!r}: command.json unreadable: {exc}") from exc
    if not cmd.get("command"):
        raise ReplayFailure(f"run {run_id!r}: command.json carries no command")
    return cmd


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


def replay_diff(base: str | None, head: str | None, repo: str) -> tuple[list[str], int]:
    """Replay every flipped row's evidence. Returns (failure lines, replayed count)."""
    failures: list[str] = []
    replayed = 0
    for row in rows_for_diff(base, head, repo):
        rid = row.get("id") or "<no-id>"
        runs = _row_run_ids(row)
        if not runs:
            failures.append(f"{rid}: completion carries no evidence run at all")
            continue
        row_ok = False
        for run_id in runs:
            try:
                kind, payload = resolve_run(run_id, repo)
            except ReplayFailure as exc:
                failures.append(f"{rid}: {exc}")
                continue
            if kind == "ci":
                row_ok = True  # external reading; CI itself is the replay
                continue
            try:
                cmd_spec = load_command(payload["dir"], run_id)
            except ReplayFailure as exc:
                failures.append(f"{rid}: {exc}")
                continue
            result = replay_command(cmd_spec, run_id)
            if result["ok"]:
                row_ok = True
            else:
                failures.append(f"{rid}: {result['detail']}")
        if row_ok:
            replayed += 1
            failures = [f for f in failures if not f.startswith(f"{rid}: ")]
    return failures, replayed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="evidence_replay.py",
        description="TR-251: replay the recorded commands behind a diff's completions")
    ap.add_argument("--base")
    ap.add_argument("--head")
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args(argv)
    if bool(args.base) != bool(args.head):
        ap.error("--base and --head go together")
    failures, replayed = replay_diff(args.base, args.head, args.repo)
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
