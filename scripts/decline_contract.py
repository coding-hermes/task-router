#!/usr/bin/env python3
"""decline_contract.py — every non-complete board row must state WHY it is not done.

THE QUESTION THIS ANSWERS
-------------------------
Bane, 2026-11-06: "tell me what is on the board now that it says it can not
work on — it has to give a reason why it cannot complete the tasks". Before
TR-393 that answer required reading rows by hand. This script makes it
machine-checkable: a row in `blocked`, `failed` or `review` (i.e. tried and
stopped) must carry a top-level `decline` object:

    "decline": {"code": "OTHER_REPO",
                "reason": "...",
                "evidence": "path@sha:line",
                "needs": "...",
                "routed_to": "...",   # optional extra, accepted
                "by": "..."}          # optional extra, accepted

`code` / `reason` / `evidence` / `needs` are required and non-empty. `code`
comes from a CLOSED vocabulary:

    PREMISE_FALSE        the row's premise was re-measured and does not hold
    OWNER_DECISION       waiting on an owner ruling the board may not take itself
    MISSING_EVIDENCE     the evidence its own acceptance criteria require does not exist
    DEPENDENCY_UNLANDED  gated on another row / service / merge that has not landed
    CI_RED               blocked on a red CI run
    ENVIRONMENT          blocked on the box/host, not the code
    SUPERSEDED           duplicate or fold: a successor row owns the work
    UNCHECKABLE          cannot be verified with any available mechanism
    OTHER_REPO           the work lives in a repository this board does not track

A `pending` row may have no decline — it has not been tried yet. Rows in
`complete` / `done` / `closed` are out of scope entirely.

MODES
-----
    --validate [--board PATH]   scan a board file (default the repo board);
                                exit 1 naming every violating row
    --staged [--repo PATH]      pre-commit mode: read the STAGED blob of the
                                board (`git show :<board>`), never the working
                                tree, so an unstaged edit cannot hide a
                                violation. Fast-path exit 0 when the staged
                                diff does not touch the board.
    --report  [--board PATH]    the owner's one-command answer: one line per
                                non-complete row, `<id>  <status>  <code or ->
                                <needs or reason, truncated>`.

ESCAPE HATCH (staged mode only)
-------------------------------
    ALLOW_DECLINE_GAP=1 git commit ...
prints a loud warning naming EVERY violation and exits 0. Never silent.

Exit codes mirror scripts/model_data_commit_gate.py:
    0 clean   1 violations   2 internal error (unreadable board, bad JSON line)

Python stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

BOARD = ".coding-hermes/board/tasks.jsonl"

#: Statuses that mean "tried and stopped": these MUST carry a decline.
REQUIRED_STATUSES = ("blocked", "failed", "review")

#: Statuses that mean the row is finished; the report skips them.
COMPLETE_STATUSES = ("complete", "done", "closed")

REQUIRED_KEYS = ("code", "reason", "evidence", "needs")

VOCABULARY = (
    "PREMISE_FALSE",
    "OWNER_DECISION",
    "MISSING_EVIDENCE",
    "DEPENDENCY_UNLANDED",
    "CI_RED",
    "ENVIRONMENT",
    "SUPERSEDED",
    "UNCHECKABLE",
    "OTHER_REPO",
)

BYPASS_ENV = "ALLOW_DECLINE_GAP"

#: Report column width for the needs/reason text.
REPORT_FIELD_WIDTH = 96


class BoardError(Exception):
    """Internal error: unreadable board or a line that is not a JSON object."""


# ---------------------------------------------------------------------------
# board parsing
# ---------------------------------------------------------------------------
def parse_board_text(text, source):
    """Rows of a JSONL board. A bad line is an INTERNAL error, not a skip:
    silently dropping a row would let a violating row hide behind its own
    corruption — the opposite of what this contract is for."""
    rows = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception as exc:
            raise BoardError("%s:%d is not valid JSON (%s)" % (source, lineno, exc))
        if not isinstance(row, dict):
            raise BoardError("%s:%d is a JSON %s, not an object"
                             % (source, lineno, type(row).__name__))
        rows.append(row)
    return rows


def read_board(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise BoardError("cannot read board %s: %s" % (path, exc))


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------
def validate_rows(rows):
    """Violation dicts for every required row whose decline breaks the contract."""
    violations = []
    for row in rows:
        rid = row.get("id") or "<row without id>"
        status = row.get("status")
        if status not in REQUIRED_STATUSES:
            continue
        decline = row.get("decline")
        if decline is None:
            violations.append(_v(rid, status, "decline",
                                 "decline is missing — a row in status '%s' must state "
                                 "why it cannot complete" % status))
            continue
        if not isinstance(decline, dict):
            violations.append(_v(rid, status, "decline",
                                 "decline is a %s, not an object"
                                 % type(decline).__name__))
            continue
        missing = [k for k in REQUIRED_KEYS if k not in decline]
        if missing:
            violations.append(_v(rid, status, ",".join(missing),
                                 "decline is missing required key(s): %s"
                                 % ", ".join(missing)))
            continue
        empty = [k for k in REQUIRED_KEYS
                 if not isinstance(decline.get(k), str) or not decline.get(k).strip()]
        if empty:
            violations.append(_v(rid, status, ",".join(empty),
                                 "decline.%s is empty" % ", decline.".join(empty)))
            continue
        code = decline["code"]
        if code not in VOCABULARY:
            violations.append(_v(rid, status, "code",
                                 "decline.code %r is outside the closed vocabulary" % (code,)))
    return violations


def _v(rid, status, field, problem):
    return {"id": rid, "status": status, "field": field, "problem": problem}


def render(violations):
    lines = [
        "",
        "=" * 72,
        "DECLINE CONTRACT VIOLATED — %d row(s) in %s lack a valid decline:"
        % (len(violations), "/".join(REQUIRED_STATUSES)),
        "=" * 72,
    ]
    for v in violations:
        lines.append("  %s (status=%s): %s" % (v["id"], v["status"], v["problem"]))
    lines.append("  closed vocabulary: %s" % ", ".join(VOCABULARY))
    lines.append("  fix: give the row a decline object (code, reason, evidence, needs)")
    lines.append("       grounded in the row's own text, or move it to a complete status.")
    lines.append("  deliberate, disclosed exception (staged commits only):")
    lines.append("       %s=1 git commit ...   — prints every violation, never silent." % BYPASS_ENV)
    lines.append("")
    return "\n".join(lines)


def ok_summary(rows):
    required = [r for r in rows if r.get("status") in REQUIRED_STATUSES]
    return "decline_contract: OK — %d row(s) in %s, all carry a valid decline" % (
        len(required), "/".join(REQUIRED_STATUSES))


# ---------------------------------------------------------------------------
# report mode
# ---------------------------------------------------------------------------
def _one_line(text):
    return " ".join(str(text).split())


def report_lines(rows):
    out = []
    for row in rows:
        if row.get("status") in COMPLETE_STATUSES:
            continue
        decline = row.get("decline")
        code = "-"
        detail = "-"
        if isinstance(decline, dict):
            if decline.get("code"):
                code = decline["code"]
            detail = decline.get("needs") or decline.get("reason") or "-"
            detail = _one_line(detail) if detail != "-" else "-"
            if len(detail) > REPORT_FIELD_WIDTH:
                detail = detail[:REPORT_FIELD_WIDTH - 1] + "…"
        out.append("%s\t%s\t%s\t%s" % (row.get("id") or "<no-id>",
                                       row.get("status") or "<no-status>",
                                       code, detail))
    return out


# ---------------------------------------------------------------------------
# staged mode (git plumbing)
# ---------------------------------------------------------------------------
def _git(args, repo):
    return subprocess.run(["git"] + args, capture_output=True, text=True,
                          cwd=repo, timeout=120)


def staged_paths(repo):
    """Paths the index changes vs HEAD. Empty (fast-path) without HEAD or git."""
    if _git(["rev-parse", "--verify", "HEAD"], repo).returncode != 0:
        return []
    p = _git(["diff", "--cached", "--name-only", "HEAD"], repo)
    if p.returncode != 0:
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def staged_board_text(repo):
    """The board AS STAGED. `git show :path` reads stage 0 of the index —
    what this commit would contain — not what the working tree happens to
    hold right now."""
    p = _git(["show", ":%s" % BOARD], repo)
    if p.returncode != 0:
        raise BoardError("board is staged but `git show :%s` failed (rc=%d): %s"
                         % (BOARD, p.returncode, (p.stderr or "")[-300:]))
    return p.stdout


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------
def run_validate(board_path):
    rows = parse_board_text(read_board(board_path), board_path)
    violations = validate_rows(rows)
    if violations:
        print(render(violations), file=sys.stderr)
        return 1
    print(ok_summary(rows), file=sys.stderr)
    return 0


def run_staged(repo):
    if BOARD not in staged_paths(repo):
        return 0  # fast path: the staged diff does not touch the board
    rows = parse_board_text(staged_board_text(repo), "index:%s" % BOARD)
    violations = validate_rows(rows)
    if os.environ.get(BYPASS_ENV) == "1":
        print("%s: WARNING: %s=1 — the board decline contract is bypassed for "
              "this commit. Staged board: %s"
              % (os.path.basename(__file__), BYPASS_ENV, BOARD), file=sys.stderr)
        for v in violations:
            print("  %s (status=%s): %s" % (v["id"], v["status"], v["problem"]),
                  file=sys.stderr)
        return 0
    if violations:
        print(render(violations), file=sys.stderr)
        return 1
    print(ok_summary(rows), file=sys.stderr)
    return 0


def run_report(board_path):
    rows = parse_board_text(read_board(board_path), board_path)
    for line in report_lines(rows):
        print(line)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="board decline contract: structured reasons for every "
                    "non-complete row (TR-393)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true",
                      help="scan a board file and fail on contract violations")
    mode.add_argument("--staged", action="store_true",
                      help="pre-commit: validate the STAGED blob of the board")
    mode.add_argument("--report", action="store_true",
                      help="one line per non-complete row: id, status, code, needs")
    ap.add_argument("--board", default=None,
                    help="board path for --validate/--report "
                         "(default: <repo>/%s)" % BOARD)
    ap.add_argument("--repo", default=ROOT,
                    help="repository root for --staged (default: this repo)")
    args = ap.parse_args(argv)

    repo = os.path.realpath(args.repo)
    board_path = args.board or os.path.join(repo, BOARD)

    try:
        if args.validate:
            return run_validate(board_path)
        if args.staged:
            return run_staged(repo)
        return run_report(board_path)
    except BoardError as exc:
        print("%s: ERROR: %s" % (os.path.basename(__file__), exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
