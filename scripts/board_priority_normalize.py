#!/usr/bin/env python3
"""board_priority_normalize.py — TR-348: the docs-audit severity adapter.

THE DEFECT (TR-348 AC3). The fleet docs-audit lane files rows with a
documentation severity of P4 (gap/wrong statement) or P5 (polish) — doctrine
lives in the filing skill, outside this repo. It copies that label into the
board's `priority` field verbatim, but the board vocabulary is CLOSED at
P0-P3 (`boardctl` normalizes case and bare digits on write and passes `P4`
through untouched). Eight DOC rows (DOC-5..7, 13..17) landed that way and
turned main's CI red — the TR-112 class (a second priority vocabulary
silently regrowing) recurring through the write path instead of by hand.

THE CONTRACT (the narrowest form that closes the class):
  * the board's `priority` field carries ONLY P0-P3 (or is absent — a
    missing key is a shape, not a second spelling, per REVIEW-TR-002);
  * an incoming documentation severity P4/P5 maps to **P3**, the lowest
    canonical tier, because a doc row is real work that must not outrank
    functional defects, and the original label stays legible on the row
    (title untouched, plus a `notes` provenance note naming both values);
  * anything else off-vocabulary is NEVER guessed — it is left untouched
    for the boundary gate (`board_commit_boundary_gate.py`) to refuse.

Why an adapter AND a gate: the writer lives outside this repo, so this repo
owns the contract at the two points it controls — the canonical form writers
can adopt, and the commit/push boundary every diff crosses (TR-251
precedent: append-only JSONL has writers that never pass through one
library). The gate refuses a diff that INTRODUCES an off-vocabulary value;
a row already carrying one keeps its history (not re-judged), so normalizing
existing rows (the c45ee4f shape) stays possible.

Subcommands
-----------
  (default)  scan the board, print what WOULD change (dry run, exit 0/3)
  --apply    rewrite ONLY the rows that change, line-wise: every untouched
             line keeps its exact bytes, the line count is asserted before
             and after, and the result is re-verified (one JSON object per
             line, P0-P3 or absent only) or the file is left untouched.

Exit codes: 0 nothing to do / success; 3 dry run found canonicalizable rows;
2 --apply had nothing to do (guard: an empty normalization must never reach
the board or a commit, mirroring board_append.py); 1 failure.

ch:trace row=TR-348 spec=docs/priority-vocabulary-review-tr-002.md test=tests/test_tr348_writer_priority_contract.py doc=docs/priority-vocabulary-review-tr-002.md evidence=docs/evidence/tr348-docs-audit-writer-contract-2026-10-26.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

#: The closed board vocabulary, single-sourced from the fleet census (the
#: classifier the board vocabulary pin is written against). Never re-literal
#: the vocabulary here: a drift between the two would let the adapter bless
#: values the census counts as offending.
from board_priority_census import VOCABULARY as CANONICAL_VOCABULARY  # noqa: E402
from board_priority_census import classify as classify_priority  # noqa: E402

#: Documentation severity -> canonical board priority. The docs doctrine
#: files gaps at P4 and polish at P5; both map to P3, the LOWEST canonical
#: tier, so a doc row can never outrank functional work while staying
#: visible. Exactly these two keys: any other off-vocabulary value is a
#: different defect class the adapter must not silently re-spell.
SEVERITY_MAP = {"P4": "P3", "P5": "P3"}

#: The provenance note a canonicalization leaves on the row. Mirrors the
#: "TR-088 priority migration: was N" convention: the old value stays
#: machine-checkable after the fact instead of only assertable.
CANON_NOTE_MARKER = "priority canonicalization"
CANON_NOTE = (f"TR-348 {CANON_NOTE_MARKER}: docs severity P4 -> board P3 "
              f"(vocabulary {'-'.join((CANONICAL_VOCABULARY[0], CANONICAL_VOCABULARY[-1]))})")


def canonical_priority(value: Any) -> str | None:
    """The canonical replacement for one stored priority value.

    Returns the canonical tier for a mapped severity (P4/P5 -> P3), None
    when the value must NOT be touched: already canonical, absent (a
    missing key is not a spelling), or off-vocabulary in a way the map
    does not cover — that class is refused at the boundary, never guessed.
    """
    text = None if value is None else str(value)
    if text in SEVERITY_MAP:
        return SEVERITY_MAP[text]
    return None


def _note_present(row: dict) -> bool:
    return CANON_NOTE_MARKER in str(row.get("notes") or "")


def _with_note(row: dict, note: str) -> dict:
    """Return row with `note` appended to `notes` (string or list aware)."""
    existing = row.get("notes")
    if isinstance(existing, list):
        row["notes"] = list(existing) + [note]
    elif isinstance(existing, str) and existing.strip():
        row["notes"] = existing.rstrip() + " ; " + note
    else:
        row["notes"] = note
    return row


def canonicalize_row(row: dict) -> tuple[dict, bool]:
    """Canonicalize ONE row's priority field.

    Returns (row, changed). On a change the ONLY mutations are the
    `priority` value and a `notes` provenance note (idempotent: present
    exactly once) — the title keeps its original "P4 —" wording, because
    the label is evidence of the filing severity and AC2's legibility
    requirement, not a second spelling of the field.
    """
    target = canonical_priority(row.get("priority"))
    if target is None or row.get("priority") == target:
        return row, False
    out = dict(row)
    out["priority"] = target
    if not _note_present(out):
        out = _with_note(out, CANON_NOTE)
    return out, True


def canonicalize_board_rows(rows: list[tuple[int, dict]]) -> list[tuple[int, dict, dict]]:
    """[(lineno, original, canonicalized)] for every row that changes."""
    changed = []
    for lineno, row in rows:
        new, delta = canonicalize_row(row)
        if delta:
            changed.append((lineno, row, new))
    return changed


def read_rows(path: str) -> list[tuple[int, dict]]:
    """Parse a JSONL board tolerantly -> [(lineno, row)] (1-indexed)."""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "id" in row:
                rows.append((lineno, row))
    return rows


def apply_canonicalization(path: str) -> int:
    """Line-wise rewrite of the canonicalizable rows. Returns an exit code.

    Safety laws (JSONL board doctrine): rewrite ONLY the target rows via
    json.dumps — every other line keeps its exact bytes; assert the line
    count is unchanged after the write (a missing trailing newline tears
    the board by merging rows); then re-verify the whole file (every line
    parses, every priority is canonical or absent) or fail loudly.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    lines = raw.decode("utf-8").splitlines(keepends=True)
    total = len(lines)
    by_lineno = {lineno: row for lineno, row in read_rows(path)}
    changes = canonicalize_board_rows(sorted(by_lineno.items()))
    if not changes:
        print("board_priority_normalize: nothing to canonicalize — board already canonical")
        return 2
    changed_linenos = {lineno for lineno, _, _ in changes}
    for lineno, _original, canonical in changes:
        original_ending = "\n" if lines[lineno - 1].endswith("\n") else ""
        lines[lineno - 1] = json.dumps(canonical, sort_keys=True) + original_ending
    new_raw = "".join(lines)
    new_total = new_raw.count("\n")
    if new_total != total:
        print(f"board_priority_normalize: REFUSING — rewrite changed the line count "
              f"({total} -> {new_total})", file=sys.stderr)
        return 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_raw)
    verify = read_rows(path)
    offending = [(ln, r.get("id"), r.get("priority")) for ln, r in verify
                 if classify_priority(r.get("priority")) not in ("canonical", "absent")]
    if len(verify) != len(by_lineno) or offending:
        print(f"board_priority_normalize: POST-WRITE VERIFY FAILED: "
              f"{len(verify)} rows (was {len(by_lineno)}), offending={offending}",
              file=sys.stderr)
        return 1
    for lineno, _, row in changes:
        print(f"normalized {row['id']} (line {lineno}): "
              f"priority now {row['priority']} — original severity kept in notes/title")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="board_priority_normalize.py",
        description="TR-348: map incoming docs-audit severities (P4/P5) onto the "
                    f"canonical board vocabulary {CANONICAL_VOCABULARY}")
    ap.add_argument("--board", default=os.path.join(REPO, ".coding-hermes", "board",
                                                    "tasks.jsonl"),
                    help="board tasks.jsonl path (default: this repo's board)")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the board (default: dry run)")
    args = ap.parse_args(argv)

    if args.apply:
        return apply_canonicalization(args.board)

    rows = read_rows(args.board)
    if not rows:
        print(f"board_priority_normalize: no parseable rows in {args.board}",
              file=sys.stderr)
        return 1
    changes = canonicalize_board_rows(rows)
    for lineno, _, row in changes:
        print(f"line {lineno}: {row['id']}: priority -> {row['priority']} "
              f"(docs severity kept in notes/title)")
    if not changes:
        print("board_priority_normalize: nothing to canonicalize — board already canonical")
        return 0
    print(f"board_priority_normalize: {len(changes)} row(s) canonicalizable "
          f"(dry run — re-run with --apply)", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
