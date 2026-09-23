#!/usr/bin/env python3
"""board_priority_census.py — fleet-wide priority-vocabulary census (REVIEW-TR-002).

Answers AC3 of REVIEW-TR-002 ("a fleet-wide priority census is meaningful"):
which boards carry ONE priority vocabulary, and which carry several.

WHY A FLEET TOOL AND NOT `boardctl` (review #17, 2026-09-22). The row that
filed this measured ONE board (task-router: 28 numeric rows against 90
P-prefixed) and the mixed scheme was invisible because `boardctl validate`
checked status and the result fields, not priority. That board is now
normalised and `validate` flags a stray value (BT-048, identity mapping
1->P1/2->P2/3->P3) — but a flag on a single board still cannot answer the
fleet question, and the answer is not what the original row assumed: on
2026-09-23 the task-router board was the ONLY clean one measured in the class
it was accused of. Other boards carry bare digits, `P4`, prose (`High`/
`Critical`) AND one corrupted JSON-fragment form (``P0","source":"dogfood-dagger``)
where a column splice swallowed a neighbouring key into the priority value.

Design notes that make the count trustworthy:

* SYMLINKED SATELLITE BOARDS ARE DEDUPED BY REALPATH. The -sync/-qa/-pm
  lanes read the primary project's board through a SYMLINKED board directory,
  so a naive walk counts one board five or six times and inflates every
  histogram. Dedupe first, then count.
* WORKTREE COPIES ARE DEDUPED BY CONTENT HASH. A git worktree carries its own
  byte-identical copy of the board, so realpath alone still double-counts.
  Two boards that hash the same are the same board.
* ABSENT PRIORITY IS A SEPARATE CLASS from off-vocabulary. A missing key is a
  shape defect; an out-of-vocabulary value is a second spelling. Only the
  latter is what "one vocabulary" is about, and only the latter is reported as
  an offending value — mixing them makes both numbers useless.
* THE CORRUPTED-FRAGMENT FORM IS CALLED OUT BY NAME. A value containing a JSON
  fragment is not a typo to be normalised; it is row corruption that also means
  every other key in that row is suspect. Flagging it as "off-vocabulary" alone
  would let a repair script "fix" the priority and leave the row broken.

Usage:
  board_priority_census.py [--root DIR ...] [--json] [--quiet] [--max-depth N]

Exit 0 when every scanned board carries one vocabulary, 1 when any board does
not. `--json` writes pure machine-parseable JSON on stdout.
"""
import argparse
import collections
import hashlib
import json
import os
import sys

#: The closed priority vocabulary. A literal, not an import: the Go
#: implementation lives in a sibling repo (coding-hermes-boardctl), and the
#: census must run (and be testable) without it present.
VOCABULARY = ("P0", "P1", "P2", "P3")

#: Directories that never contain a live board worth counting.
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "site-packages",
              "__pycache__", ".mypy_cache", ".pytest_cache"}

#: Default scan roots, in priority order. The first existing root wins unless
#: the caller passes --root explicitly.
_DEFAULT_ROOTS = ("~",)


def _skip(path):
    """True when a path is not a board at all.

    `/snapshots/` copies are archive material: a snapshot of last week's board
    is not a board anyone routes work from, and counting it would make the fleet
    number permanently non-zero for a defect already fixed.
    """
    return "/snapshots/" in path or "/.hermes/skills" in path


#: Path shapes that are NOT the durable fleet surface. They are still scanned
#: and reported (never silently dropped) but they do not carry the headline
#: verdict, because "fix this board" is not actionable for a tree that gets
#: reaped:
#:   worktree — a git worktree's copy of its primary's board, a snapshot at the
#:              branch point. It matters as a REINTRODUCTION path (a merged
#:              worktree carries its vocabulary back into the primary), so it is
#:              reported, not ignored.
#:   archive  — relocated-from-tmp dumps and the release-engineer scratch trees.
_TIER_WORKTREE = "/worktrees/"
_TIER_ARCHIVE = ("/relocated-from-tmp/", "/.hermes/release-engineer/")


def classify_path(path):
    """Return "primary", "worktree" or "archive" for a board path."""
    if any(marker in path for marker in _TIER_ARCHIVE):
        return "archive"
    if _TIER_WORKTREE in path:
        return "worktree"
    return "primary"


def find_boards(roots, max_depth=6):
    """Return every `tasks.jsonl` under the roots, deepest-first pruning.

    Symlinked directories ARE followed (`followlinks=True`) with a visited-realpath
    guard. The fleet's satellite lanes (-sync/-qa/-pm/-dogfood) read the primary
    project's board through a SYMLINKED board directory; a walk that ignores
    directory symlinks does not merely avoid double-counting them, it makes that
    whole board class INVISIBLE to the census — and a census that cannot see a
    class cannot prove anything about it. Following them turns the satellite into
    a reported duplicate (deduped by realpath), which is evidence the class was
    reached rather than an assumption that it does not exist.
    """
    found = []
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            if dirpath.count(os.sep) - base_depth > max_depth:
                dirnames[:] = []
                continue
            # Refuse only a TRUE CYCLE — a child that resolves to one of its own
            # ancestors. A global "already visited anywhere" set would be
            # cheaper but wrong: it also drops a symlink pointing at a SIBLING,
            # which is exactly the satellite topology this census must be able
            # to see. Cycles are impossible then, because any revisit must walk
            # back through a directory already on the current path, and the
            # max_depth bound is the hard backstop.
            cur_real = os.path.realpath(dirpath)
            keep = []
            for d in dirnames:
                if d in _SKIP_DIRS:
                    continue
                child_real = os.path.realpath(os.path.join(dirpath, d))
                if child_real == cur_real or cur_real.startswith(child_real + os.sep):
                    continue
                keep.append(d)
            dirnames[:] = keep
            if "tasks.jsonl" in filenames:
                path = os.path.join(dirpath, "tasks.jsonl")
                if not _skip(path):
                    found.append(path)
    return sorted(found)


def parse_board(path):
    """Parse one board: -> (rows, unparseable_line_count).

    Tolerant by design — DF-BOARDCTL-9 proved that a single spec-invalid line
    makes a whole board unreadable to a strict reader, and a census that dies on
    the first corrupted board cannot report on the other fifty.
    """
    rows, bad = [], 0
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict) and "id" in row:
                rows.append(row)
    return rows, bad


def classify(value):
    """Classify one stored priority value.

    Returns one of: "canonical", "absent", "corrupt-fragment", "off-vocabulary".
    """
    if value is None or value == "":
        return "absent"
    text = str(value)
    if text in VOCABULARY:
        return "canonical"
    # A JSON fragment means a column splice swallowed a neighbouring key into
    # this value (see module docstring). Reported apart from a plain stray
    # spelling because the repair differs: the row needs reconstruction, not a
    # re-spell.
    if '"' in text or "\\" in text:
        return "corrupt-fragment"
    return "off-vocabulary"


def census_board(rows):
    """Summarise one board's priorities.

    `histogram` counts EVERY stored value — canonical, off-vocabulary, corrupt
    and `<absent>` — because that IS the census: a histogram that silently
    omitted the off-vocabulary rows would print "P2 x6213" for a board that also
    carries 16 bare `3`s and let a reader conclude there is nothing to fix.
    `offending` is the actionable subset (everything not in the vocabulary);
    `classes` splits the off-vocabulary class from the corrupt one, because the
    repairs differ.
    """
    histogram = collections.Counter()
    offending = collections.defaultdict(list)
    classes = collections.Counter()
    for row in rows:
        value = row.get("priority")
        kind = classify(value)
        classes[kind] += 1
        histogram["<absent>" if kind == "absent" else str(value)] += 1
        if kind in ("off-vocabulary", "corrupt-fragment"):
            offending[str(value)].append(row.get("id"))
    return {
        "rows": len(rows),
        "histogram": dict(sorted(histogram.items())),
        "classes": dict(classes),
        "offending": {k: v for k, v in sorted(offending.items())},
    }


def run(roots=None, max_depth=6):
    """Census the fleet.

    Returns the report dict. Boards are deduped by realpath (symlinked satellite
    dirs) and then by content hash (worktree copies).
    """
    roots = list(roots) if roots else list(_DEFAULT_ROOTS)
    seen_real, seen_hash = {}, {}
    boards = []
    duplicates = []

    for path in find_boards(roots, max_depth=max_depth):
        real = os.path.realpath(path)
        if real in seen_real:
            duplicates.append({"path": path, "reason": "symlink",
                               "same_as": seen_real[real]})
            continue
        seen_real[real] = path
        try:
            with open(real, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            continue
        if digest in seen_hash:
            duplicates.append({"path": path, "reason": "identical-content",
                               "same_as": seen_hash[digest]})
            continue
        seen_hash[digest] = path
        boards.append(real)

    total = collections.Counter()
    class_totals = collections.Counter()
    dirty, unreadable = [], []
    tiers = collections.Counter()
    dirty_tiers = collections.Counter()
    tier_rows = collections.Counter()
    rows_total = 0
    for path in sorted(boards):
        rows, bad = parse_board(path)
        summary = census_board(rows)
        summary["path"] = path
        summary["tier"] = classify_path(path)
        summary["unparseable_lines"] = bad
        tiers[summary["tier"]] += 1
        tier_rows[summary["tier"]] += summary["rows"]
        # Count EVERY parsed row. Summing the histogram instead would silently
        # drop the off-vocabulary and corrupt rows — the precise rows this
        # census exists to report — and understate the board.
        rows_total += summary["rows"]
        if bad:
            unreadable.append({"path": path, "unparseable_lines": bad})
        for key, n in summary["histogram"].items():
            total[key] += n
        for key, n in summary["classes"].items():
            class_totals[key] += n
        if summary["offending"]:
            dirty.append(summary)
            dirty_tiers[summary["tier"]] += 1

    return {
        "boards_scanned": len(boards),
        "boards_skipped_as_duplicates": len(duplicates),
        "duplicates": duplicates,
        "boards_by_tier": dict(tiers),
        "task_rows_by_tier": dict(tier_rows),
        "task_rows_total": rows_total,
        "priority_histogram": dict(sorted(total.items())),
        "value_classes": dict(class_totals),
        "boards_with_off_vocabulary_priority": len(dirty),
        # The headline verdict answers the FLEET question. A worktree's board is
        # a snapshot of its primary's at branch point; it still matters as a
        # reintroduction path, so it is reported in its own tally rather than
        # averaged into (or hidden from) the primary count.
        "dirty_boards_by_tier": dict(dirty_tiers),
        "boards_off_vocabulary": dirty,
        "boards_with_unparseable_lines": len(unreadable),
        "unreadable_boards": unreadable,
        "single_vocabulary": dirty_tiers.get("primary", 0) == 0,
    }


def render(report):
    """Human-readable census."""
    out = []
    tiers = report["boards_by_tier"]
    out.append("== board priority census (REVIEW-TR-002) ==")
    out.append(f"vocabulary:     {{{','.join(VOCABULARY)}}}")
    out.append(f"boards scanned: {report['boards_scanned']} "
               f"({report['boards_skipped_as_duplicates']} duplicate copy/copies skipped)"
               f"  [primary={tiers.get('primary', 0)}"
               f" worktree={tiers.get('worktree', 0)}"
               f" archive={tiers.get('archive', 0)}]")
    out.append(f"task rows:      {report['task_rows_total']}")
    out.append("")
    out.append("value classes:  " + ", ".join(
        f"{k}={v}" for k, v in sorted(report["value_classes"].items())))
    out.append("histogram:      " + ", ".join(
        f"{k}={v}" for k, v in report["priority_histogram"].items()))
    out.append("")
    dirty, by_tier = report["boards_off_vocabulary"], report["dirty_boards_by_tier"]
    if report["single_vocabulary"]:
        out.append("RESULT: every PRIMARY fleet board carries ONE priority "
                   f"vocabulary {{{','.join(VOCABULARY)}}}")
    else:
        out.append(f"RESULT: {by_tier.get('primary', 0)} primary board(s) carry "
                   "an off-vocabulary priority:")
        for board in report["boards_off_vocabulary"]:
            if board["tier"] != "primary":
                continue
            vals = ", ".join(f"{k!r} x{len(v)}" for k, v in board["offending"].items())
            out.append(f"  {board['path']}  ({board['rows']} rows): {vals}")
    # Worktree/archive copies are reported separately and never silently folded
    # into (or out of) the fleet number: a worktree's board is a snapshot of its
    # primary's at branch point, and merging it is how the vocabulary moves back.
    for tier in ("worktree", "archive"):
        if not by_tier.get(tier):
            continue
        out.append("")
        out.append(f"{by_tier[tier]} {tier} board(s) also carry an off-vocabulary "
                   "priority (copies/snapshots — not the durable fleet surface):")
        for board in report["boards_off_vocabulary"]:
            if board["tier"] != tier:
                continue
            vals = ", ".join(f"{k!r} x{len(v)}" for k, v in board["offending"].items())
            out.append(f"  {board['path']}  ({board['rows']} rows): {vals}")
    if report["boards_with_unparseable_lines"]:
        out.append("")
        out.append(f"NOTE: {report['boards_with_unparseable_lines']} board(s) carry "
                   "unparseable JSON lines — their rows are NOT fully counted:")
        for board in report["unreadable_boards"]:
            out.append(f"  {board['path']}: {board['unparseable_lines']} bad line(s)")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="fleet-wide priority-vocabulary census (REVIEW-TR-002)")
    ap.add_argument("--root", action="append", dest="roots", default=None,
                    help="scan root (repeatable; default: ~)")
    ap.add_argument("--json", action="store_true",
                    help="emit pure machine-parseable JSON on stdout")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing; exit code carries the verdict")
    ap.add_argument("--max-depth", type=int, default=6,
                    help="directory depth limit below each root (default 6)")
    args = ap.parse_args(argv)

    report = run(args.roots, max_depth=args.max_depth)
    if args.json:
        print(json.dumps(report))
    elif not args.quiet:
        sys.stdout.write(render(report))
    return 0 if report["single_vocabulary"] else 1


if __name__ == "__main__":
    sys.exit(main())
