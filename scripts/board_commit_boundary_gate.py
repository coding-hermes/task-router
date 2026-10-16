#!/usr/bin/env python3
"""board_commit_boundary_gate.py — TR-251 mechanism (4): the commit-boundary gate.

Doctrine: docs/traceability-doctrine.md — the completion gate (TR-249) lives
inside boardctl's write path, but append-only JSONL has OTHER writers: hand
appends, cron, foremen, git merges. A diff can flip a row to complete without
ever passing through the gated writer. The gate therefore belongs at the
COMMIT/PUSH boundary too: refuse a diff that flips a row to complete with
unbound evidence.

Contract (board row TR-251, criterion 5)
----------------------------------------
Scan a diff (default: staged changes; --diff <git diff args> for CI) for task
rows whose status becomes `complete`/`done` in the NEW lines and was not
already complete in the OLD lines. For every such row REQUIRE at least one of:

  1. an evidence run bound on the row — evidence_run_id / detail.evidence[]
     (the boardctl --evidence-run-id shape), or
  2. a stated-reason null — evidence_none / witness=none:<reason> (doctrine
     hard rule 3), or
  3. the row's evidence already satisfies the TR-249 gate's measured-number /
     artifact-path forms (the gate this module deliberately does NOT
     duplicate — it reuses board_complete_gate.evidence_state).

A row that flips to complete with NONE of these is REFUSED with exit 1 and
the refusal names the row id, the file, and the missing evidence forms.

Regression fixture (pinned in tests/test_board_commit_boundary_gate.py):
TR-011's shape — status=complete, 4 acceptance_criteria, guard_result=PASS,
no evidence_run_id — must be REFUSED. Four criteria and a green guard are
exactly what a false completion carries; the boundary gate exists because
those do not prove the outcome.

Usage:
  python3 scripts/board_commit_boundary_gate.py            # staged diff
  python3 scripts/board_commit_boundary_gate.py --base A --head B
  python3 scripts/board_commit_boundary_gate.py --old-file F --new-file F2

ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_board_commit_boundary_gate.py evidence=docs/evidence-run-store-decision.md
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from board_complete_gate import TERMINAL, evidence_state  # noqa: E402

BOARD_BASENAME = os.path.join(".coding-hermes", "board", "tasks.jsonl")


def _rows_from_lines(lines: list[str]) -> dict[str, dict]:
    """id -> LAST row (append-only JSONL; the last occurrence is the state)."""
    latest: dict[str, dict] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("id"):
            latest[str(row["id"])] = row
    return latest


def _has_bound_run(row: dict) -> bool:
    """The boardctl --evidence-run-id shapes: a top-level evidence_run_id, or
    detail.evidence[].run_id (the SG-126 envelope)."""
    if str(row.get("evidence_run_id") or "").strip():
        return True
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            return False
    if not isinstance(detail, dict):
        return False
    for ev in detail.get("evidence") or []:
        if isinstance(ev, dict) and str(ev.get("run_id") or "").strip():
            return True
    return False


def _has_stated_null(row: dict) -> bool:
    for field in ("evidence_none",):
        v = row.get(field)
        if isinstance(v, str) and v.strip():
            return True
    w = row.get("witness")
    if isinstance(w, str) and w.strip().lower().startswith("none:") and w.strip()[5:].strip():
        return True
    return False


def rows_flipped_to_complete(old_lines: list[str], new_lines: list[str]) -> list[dict]:
    """Rows whose status becomes terminal-complete in NEW and was not
    already terminal in OLD (by id, last-occurrence semantics)."""
    old = _rows_from_lines(old_lines)
    new = _rows_from_lines(new_lines)
    flipped = []
    for rid, row in new.items():
        status = str(row.get("status") or "").strip().lower()
        if status not in TERMINAL:
            continue
        prev = old.get(rid)
        prev_status = str((prev or {}).get("status") or "").strip().lower()
        if prev_status in TERMINAL:
            continue  # already-terminal: history is not re-judged
        flipped.append(row)
    return flipped


def check_flipped_rows(flipped: list[dict]) -> list[str]:
    """Refusal lines [] for the flipped rows; non-empty = the gate fails."""
    refusals = []
    for row in flipped:
        if _has_bound_run(row) or _has_stated_null(row):
            continue
        ok_ev, why_ev = evidence_state(row)
        if ok_ev:
            continue
        refusals.append(
            f"{row.get('id')}: completion REFUSED by board_commit_boundary_gate (TR-251): "
            f"row flips to status={row.get('status')} with unbound evidence "
            f"({why_ev}). Bind an evidence run (--evidence-run-id, stored per "
            f"docs/evidence-run-store-decision.md), or state evidence_none/witness=none:<reason>.")
    return refusals


def _diff_lines(base: str | None, head: str | None, repo: str) -> tuple[list[str], list[str]]:
    """(old, new) tasks.jsonl line sets for a base..head diff. The OLD side is
    the file content at base; the NEW side at head — union-merge safe: rows
    are matched by id, not by line position."""
    def show(rev: str) -> list[str]:
        out = subprocess.run(
            ["git", "show", f"{rev}:{BOARD_BASENAME}"],
            capture_output=True, text=True, cwd=repo)
        if out.returncode != 0:
            return []
        return out.stdout.splitlines()

    if base and head:
        return show(base), show(head)
    # Working-tree / staged mode: OLD = HEAD, NEW = staged-or-worktree file.
    old = show("HEAD")
    path = os.path.join(repo, BOARD_BASENAME)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            new = fh.read().splitlines()
    else:
        new = []
    return old, new


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="board_commit_boundary_gate.py",
        description="TR-251: refuse a diff that flips a board row to complete with unbound evidence")
    ap.add_argument("--base", help="git rev for the OLD side (with --head)")
    ap.add_argument("--head", help="git rev for the NEW side (with --base)")
    ap.add_argument("--old-file", help="path to the old tasks.jsonl (file mode)")
    ap.add_argument("--new-file", help="path to the new tasks.jsonl (file mode)")
    ap.add_argument("--repo", default=REPO,
                    help="repo whose board is diffed (default: this checkout)")
    args = ap.parse_args(argv)

    if bool(args.base) != bool(args.head):
        ap.error("--base and --head go together")
    if args.old_file or args.new_file:
        if not (args.old_file and args.new_file):
            ap.error("--old-file and --new-file go together")
        with open(args.old_file, encoding="utf-8") as fh:
            old = fh.read().splitlines()
        with open(args.new_file, encoding="utf-8") as fh:
            new = fh.read().splitlines()
    elif args.base:
        old, new = _diff_lines(args.base, args.head, args.repo)
    else:
        old, new = _diff_lines(None, None, args.repo)

    flipped = rows_flipped_to_complete(old, new)
    refusals = check_flipped_rows(flipped)
    if refusals:
        for r in refusals:
            print(r, file=sys.stderr)
        print(f"board_commit_boundary_gate: {len(refusals)} completion(s) refused",
              file=sys.stderr)
        return 1
    print(f"board_commit_boundary_gate: OK "
          f"({len(flipped)} completion flip(s) in the diff, all evidence-bound)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
