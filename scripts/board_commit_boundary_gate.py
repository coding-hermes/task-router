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

TR-348 arm — the priority vocabulary boundary. The docs-audit row writer
(lives outside this repo) copied documentation severities P4/P5 into the
`priority` field verbatim; the board vocabulary is CLOSED at P0-P3 and eight
DOC rows landed that way, turning main's CI red. A diff that INTRODUCES an
off-vocabulary priority value (off-vocabulary or corrupt-fragment per the
fleet census classifier) is REFUSED. A value already on the OLD side is
history, not a new emission — re-appending an existing row must not be
blocked, which is what makes a normalization pass (board_priority_normalize)
landable while the class stays closed forward. The canonical mapping for the
docs severities lives in scripts/board_priority_normalize.py (P4/P5 -> P3,
original label kept in notes/title); this gate is the enforcement half that
makes the TR-112 class unable to recur silently.

A row that flips to complete with NONE of these is REFUSED with exit 1 and
the refusal names the row id, the file, and the missing evidence forms.

INT-CI-30 — bound run ids must be RESOLVABLE FORMS. The boundary gate and
the CI evidence-replay gate (scripts/evidence_replay.py) must AGREE on what
"evidence-bound" means. A completion flip's bound run id
(evidence_run_id / detail.evidence[].run_id) must be one of the store
decision's forms: (a) an all-digit GitHub Actions run id (>= 6 digits),
(b) a verdict-form run id that resolves to
.gitreins/history/<date>/<id>/verdict.json under the repo that owns the
board, or (c) the row states a reason: evidence_none /
witness=none:<reason>. Anything else — hand-written labels, tick-directory
names (foreman-tick-...), .gitreins/history paths used as ids, free prose —
is REFUSED: a local tick label is gitignored state that can never resolve
in CI, so a row carrying one passed THIS gate while the replay gate reded
the SAME diff (TR-1000976681059305 @ 01251fa4, CI run 44922361578). Rows
with NO bound run id keep the TR-249 evidence_state forms (measured number
/ artifact path / stated NULL) via board_complete_gate.

Regression fixture (pinned in tests/test_tr251_boundary_and_replay.py):
TR-011's shape — status=complete, 4 acceptance_criteria, guard_result=PASS,
no evidence_run_id — must be REFUSED. Four criteria and a green guard are
exactly what a false completion carries; the boundary gate exists because
those do not prove the outcome.

Usage:
  python3 scripts/board_commit_boundary_gate.py            # staged diff
  python3 scripts/board_commit_boundary_gate.py --base A --head B
  python3 scripts/board_commit_boundary_gate.py --old-file F --new-file F2

ch:trace row=TR-251 spec=docs/traceability-doctrine.md#hard-rules test=tests/test_tr251_boundary_and_replay.py evidence=docs/evidence-run-store-decision.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from board_complete_gate import TERMINAL, evidence_state  # noqa: E402
from board_priority_census import classify as classify_priority  # noqa: E402
from board_priority_census import VOCABULARY as PRIORITY_VOCABULARY  # noqa: E402

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


#: INT-CI-30: a bound evidence-run id must be a RESOLVABLE FORM
#: (docs/evidence-run-store-decision.md). The replay gate
#: (evidence_replay.resolve_run) refuses anything else, so the boundary
#: gate must refuse it too — otherwise a row passes here and reds CI on the
#: same diff (TR-1000976681059305 @ 01251fa4 bound a local foreman-tick-...
#: directory label; CI run 44922361578 went red).
CI_RUN_ID = re.compile(r"[0-9]{6,}")   # form (a): all digits, >= 6 digits
VERDICT_DOC = "docs/evidence-run-store-decision.md"
ACCEPTED_FORMS = (
    "(a) a GitHub Actions run id (all digits, >= 6 digits), "
    "(b) a verdict-form run id that resolves to "
    ".gitreins/history/<date>/<id>/verdict.json, or "
    "(c) the row states evidence_none / witness=none:<reason>")


def _bound_run_ids(row: dict) -> list[str]:
    """All bound evidence-run ids: a top-level evidence_run_id, plus
    detail.evidence[].run_id (the SG-126 envelope). Same extraction the
    replay gate's _row_run_ids performs, so both gates judge the same set."""
    runs: list[str] = []
    v = str(row.get("evidence_run_id") or "").strip()
    if v:
        runs.append(v)
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = None
    if not isinstance(detail, dict):
        return runs
    for ev in detail.get("evidence") or []:
        if isinstance(ev, dict):
            rid = str(ev.get("run_id") or "").strip()
            if rid and rid not in runs:
                runs.append(rid)
    return runs


def _verdict_form_resolves(run_id: str, repo: str) -> bool:
    """Form (b): .gitreins/history/<date>/<run_id>/verdict.json exists under
    the repo that owns the board (the store decision's GitReins verdict
    history). Same lookup evidence_replay.resolve_run performs."""
    history = os.path.join(repo, ".gitreins", "history")
    if not os.path.isdir(history):
        return False
    for date in os.listdir(history):
        if os.path.exists(os.path.join(history, date, run_id, "verdict.json")):
            return True
    return False


def _run_id_form_problem(run_id: str, repo: str) -> str | None:
    """None when run_id is a resolvable form; otherwise a short reason the
    refusal can quote."""
    rid = str(run_id or "").strip()
    if not rid:
        return "empty run id"
    if CI_RUN_ID.fullmatch(rid):
        return None  # form (a): a GitHub Actions run id
    if rid.lower().startswith("none:"):
        return ("a stated-reason null belongs in the evidence_none field or "
                "witness=none:<reason>, not in the run-id slot")
    if "/" in rid or "\\" in rid:
        return ("a filesystem path is not a run id — .gitreins/history paths "
                "name the verdict STORE, never an id")
    if _verdict_form_resolves(rid, repo):
        return None  # form (b): a verdict-form run that resolves
    return ("not a GitHub Actions run id (all digits, >= 6 digits) and no "
            f".gitreins/history/<date>/{rid}/verdict.json resolves — a "
            "hand-written label or local tick-directory name is gitignored "
            "state that can never resolve")


def _unresolvable_run_refusal(row: dict, problems: list[tuple[str, str]]) -> str:
    quoted = "; ".join(f"run {r!r}: {why}" for r, why in problems)
    return (
        f"{row.get('id')}: completion REFUSED by board_commit_boundary_gate "
        f"(TR-251, INT-CI-30): bound evidence run id is not a resolvable form "
        f"({quoted}). Accepted forms per {VERDICT_DOC}: {ACCEPTED_FORMS}. "
        "Rebind the row (--evidence-run-id / detail.evidence[].run_id) to a "
        "resolvable form, or state the reason.")


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


# ---------------------------------------------------------------------------
# TR-348: the priority-vocabulary arm. A row whose LAST state in the diff
# carries a priority value that was NOT already carried by that id on the OLD
# side is a fresh emission — the write path failed to canonicalize. Absent on
# both sides is a shape, not a spelling (REVIEW-TR-002); absent -> present is
# judged; present -> present compares the last carried values, so re-appends
# and history rewrites of an already-off-vocabulary row pass.
# ---------------------------------------------------------------------------

def priorities_introduced(old_lines: list[str], new_lines: list[str]) -> list[dict]:
    """Rows whose NEW last-state carries an off-vocabulary priority the OLD
    side did not already carry for that id. `why` names the census class."""
    old = _rows_from_lines(old_lines)
    new = _rows_from_lines(new_lines)
    introduced = []
    for rid, row in new.items():
        value = row.get("priority")
        kind = classify_priority(value)
        if kind in ("canonical", "absent"):
            continue
        prev = old.get(rid) or {}
        if prev.get("priority") == value:
            continue  # already carried on the old side: history, not a new emission
        introduced.append({"id": rid, "priority": value, "why": kind})
    return introduced


def check_introduced_priorities(introduced: list[dict]) -> list[str]:
    """Refusal lines for freshly-emitted off-vocabulary priorities."""
    refusals = []
    for item in introduced:
        refusals.append(
            f"{item['id']}: board write REFUSED by board_commit_boundary_gate (TR-348): "
            f"priority {item['priority']!r} is {item['why']} — the board vocabulary is "
            f"closed at {PRIORITY_VOCABULARY}. Map docs-audit severities before filing "
            f"(scripts/board_priority_normalize.py: P4/P5 -> P3, original label kept "
            f"in notes/title), then re-file.")
    return refusals


def check_flipped_rows(flipped: list[dict], repo: str | None = None) -> list[str]:
    """Refusal lines [] for the flipped rows; non-empty = the gate fails.

    A stated-reason null passes (form c). Bound run ids must be RESOLVABLE
    forms (a)/(b) — anything else is REFUSED (INT-CI-30). Rows with no bound
    run id keep the TR-249 evidence_state forms (measured number / artifact
    path / stated NULL) via board_complete_gate."""
    repo = repo or REPO
    refusals = []
    for row in flipped:
        if _has_stated_null(row):
            continue
        runs = _bound_run_ids(row)
        if runs:
            problems = [(r, why) for r in runs
                        if (why := _run_id_form_problem(r, repo))]
            if problems:
                refusals.append(_unresolvable_run_refusal(row, problems))
            continue  # every bound run id is a resolvable form (or refused above)
        ok_ev, why_ev = evidence_state(row)
        if ok_ev:
            continue
        refusals.append(
            f"{row.get('id')}: completion REFUSED by board_commit_boundary_gate (TR-251): "
            f"row flips to status={row.get('status')} with unbound evidence "
            f"({why_ev}). Bind an evidence run (--evidence-run-id, stored per "
            f"{VERDICT_DOC}), or state evidence_none/witness=none:<reason>.")
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
    introduced = priorities_introduced(old, new)
    refusals = check_introduced_priorities(introduced)
    refusals += check_flipped_rows(flipped, repo=args.repo)
    if refusals:
        for r in refusals:
            print(r, file=sys.stderr)
        print(f"board_commit_boundary_gate: {len(refusals)} refusal(s) "
              f"({len(introduced)} off-vocabulary priority emission(s), "
              f"{len(flipped)} unbound completion(s))",
              file=sys.stderr)
        return 1
    print(f"board_commit_boundary_gate: OK "
          f"({len(flipped)} completion flip(s) in the diff, all evidence-bound; "
          f"{len(introduced)} off-vocabulary priority emission(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
