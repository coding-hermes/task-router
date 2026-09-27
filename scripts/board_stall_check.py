#!/usr/bin/env python3
"""board_stall_check.py — name every in-flight board row that stopped moving (TR-196).

Two rows on this board (TR-115, TR-117) sat dispatched with no live worker, no
worktree and no commit, and nothing looked: the row's own ``resolution`` field
said "worker work not recovered" while the board reported them as in flight.
TR-196 asks for the cheap, re-runnable check that names that state.

WHAT COUNTS AS IN-FLIGHT. The stall semantics live on ``worker_status`` — the
board's in-flight value there is ``dispatched`` (the TR-187 stall precedent) —
while ``status`` is the row's lifecycle terminal (complete/failed). The check
requires BOTH the lifecycle to be non-terminal AND ``worker_status`` in
(DISPATCHED, IN_PROGRESS); a completed row whose ``worker_status`` still reads
``dispatched`` (REVIEW-TR-001..004 — a stale mirror) is NOT in-flight, because
the lifecycle says the row finished. Terminal statuses are complete|done.

LIVENESS IS A PURE-FUNCTION PREDICATE. Worktrees whose paths are absent (the
TR-115/TR-117 shape: "branch removed without merge") are decided by path test
alone; a present worktree is live only when its ``git status --porcelain``
SUCCEEDS. An OSError on the subprocess (no git binary, a sandbox that forbids
exec) is not liveness — that arm returns ``worktree_check_error`` and leaves
the row flagged, because a check that cannot look must not declare life.

RECOVERY NEVER TOUCHES A ROW. The default mode only reports. ``--requeue`` is
an explicit, per-row --id-targeted action that appends a ``task_updated``
event whose detail carries ``status: pending`` AND the evidence triple
(no_worker / no_worktree / no_commit), then rewrites the ONE row by id,
setting ``worker_status: pending`` (the in-flight mirror is what went stale —
resetting ``status`` would clobber a genuine terminal, and the TR-187 requeue
proved pending is where a requeued row goes). No default mutation, no
age-based mutation: it does not silently reset.

AGE, NOT MTIME TRUST. Rows carry several clock spellings (``updated_at`` /
``updated``), none documented as reliable — and the shape this row exists for
proves the point: TR-115/TR-117's ``updated_at`` claimed a fresh touch during
the same window the worktree was being deleted. So the default age field is
``created_at``, overridable by ``--age-field``, measured against ``--now``
(never wall-clock, so the check is deterministic and re-runnable).

Evidence triple per flagged row:
  worker evidence  — liveness of the row's ``worktree`` (path absent, or a
                     present worktree whose porcelain read fails)
  commit evidence  — the row's own ``commit_hash`` field is set
  no live worker   — the liveness itself; there is no fleet-wide worker
                     registry to consult (named as a limitation).

Exit codes: 0 = no in-flight stalls, 1 = stalls found, 2 = --requeue run.

Usage:
  board_stall_check.py [--board tasks.jsonl] [--events events.jsonl]
                       [--age-hours N] [--age-field updated_at|created_at]
                       [--now ISO8601] [--requeue --id TR-xxx ...]
                       [--json] [--quiet]
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

#: In-flight worker_status vocabulary member this board actually uses. The
#: canonical status vocabulary (complete|done|failed|pending|review|todo) is a
#: DIFFERENT column: a row with status=complete and worker_status=dispatched
#: (REVIEW-TR-001..004) is a completed row with a stale mirror, not a stall.
IN_FLIGHT = ("dispatched", "in_progress")

#: Terminal lifecycle statuses. Anything else (pending, review, todo, failed
#: being requeued, off-vocabulary values) is non-terminal for this check.
TERMINAL = ("complete", "done")

#: Where recovery puts a row: the TR-187 requeue precedent. NOT a reset of
#: ``status`` — that would clobber a genuine terminal like complete.
REQUEUE_STATUS = "pending"

#: Default staleness age in hours, measured against the row's age field.
DEFAULT_AGE_HOURS = 6.0

#: Fields a --requeue rewrite preserves from the original row verbatim.
REQUEUE_PRESERVE_FIELDS = ("id", "branch", "created_at")

#: The event vocabulary member and actor the board already uses for row
#: updates (measured from events.jsonl: event_type=task_updated, actor=foreman).
EVENT_TYPE = "task_updated"
EVENT_ACTOR = "foreman"


def parse_ts(value):
    """Parse a board timestamp; None when absent or unparseable.

    The board carries two spellings (``2026-09-26T12:33:49Z`` and
    ``2026-09-26T12:33:49``); both parse, neither is trusted as FRESH — the
    field only has to beat the age bar. Z-suffixed strings are UTC; naive
    strings are read as UTC.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            return datetime.strptime(
                value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.strptime(
            value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_now(value):
    """Parse the --now instant (ISO-8601). None -> caller uses wall clock."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def worktree_state(path, git="git"):
    """Decide worktree liveness from the path alone (pure function).

    Returns one of:
      "worktree_present" — path exists and a `git status --porcelain` inside
                           it SUCCEEDS (the only "live worker proxy" that
                           exists on this host: no fleet worker registry).
      "worktree_gone"    — path is None/empty or does not exist (the
                           TR-115/TR-117 shape: "branch removed").
      "worktree_check_error" — path exists but the porcelain read FAILED
                           (OSError/no git): unknown, NOT live.
    """
    if not path or not os.path.exists(path):
        return "worktree_gone"
    try:
        proc = subprocess.run(
            [git, "status", "--porcelain"],
            cwd=path, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "worktree_check_error"
    if proc.returncode != 0:
        return "worktree_check_error"
    return "worktree_present"


def classify_row(row, now, age_hours, age_field="created_at", git="git"):
    """Classify one row -> None when healthy, else an evidence dict.

    In-flight requires BOTH worker_status in IN_FLIGHT and a non-terminal
    lifecycle status; a stale dispatched mirror on a complete row is not a
    stall. Old = age field beats age_hours. Evidence names worker, worktree
    and commit separately.
    """
    if row.get("worker_status") not in IN_FLIGHT:
        return None
    if row.get("status") in TERMINAL:
        return None
    ts = parse_ts(row.get(age_field))
    if ts is None:
        return None
    age_hours_measured = (now - ts).total_seconds() / 3600.0
    if age_hours_measured < age_hours:
        return None
    wt_state = worktree_state(row.get("worktree"), git=git)
    if wt_state == "worktree_present":
        return None  # a live worker proxy exists: not a stall, however old
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "status": row.get("status"),
        "worker_status": row.get("worker_status"),
        "age_field": age_field,
        "age_hours": round(age_hours_measured, 2),
        "age_threshold_hours": age_hours,
        "timestamp": row.get(age_field),
        "worktree": row.get("worktree"),
        "worktree_state": wt_state,
        "branch": row.get("branch"),
        "commit_hash": row.get("commit_hash"),
        "evidence": {
            "no_worker": True,          # no live worker registry exists
            "no_worktree": wt_state == "worktree_gone",
            "no_commit": not row.get("commit_hash"),
        },
    }


def find_stalls(rows, now, age_hours, age_field="created_at", git="git"):
    """The full check over a board -> list of flagged evidence dicts."""
    flagged = []
    for row in rows:
        verdict = classify_row(row, now, age_hours, age_field=age_field, git=git)
        if verdict is not None:
            flagged.append(verdict)
    return flagged


def load_board(path):
    """Read rows tolerantly (one bad line never kills the check)."""
    rows, bad = [], 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict) and row.get("id"):
                rows.append(row)
    return rows, bad


def render(report):
    """Human report; names rows and evidence, states the limitation."""
    out = ["== board stall check (TR-196) ==",
           f"board:      {report['board']}",
           f"rows:       {report['rows']} "
           f"({report['unparseable_lines']} unparseable line(s) skipped)",
           f"in-flight:  worker_status in {list(IN_FLIGHT)} "
           f"and status not in {list(TERMINAL)}",
           f"age rule:   {report['age_field']} older than "
           f"{report['age_hours']}h (measured against {report['now']})",
           ""]
    if not report["stalls"]:
        out.append("RESULT: no stalled in-flight rows — every in-flight row "
                    "has a worktree present (or is younger than the age bar).")
    else:
        out.append(f"RESULT: {len(report['stalls'])} in-flight row(s) with no "
                    "live worker and no commit past the age bar:")
        for s in report["stalls"]:
            ev = s["evidence"]
            out.append(
                f"  {s['id']}  status={s['status']}/{s['worker_status']}  "
                f"age={s['age_hours']}h (bar {s['age_threshold_hours']}h)")
            out.append(
                f"    worktree: {s['worktree_state']}"
                + (f" ({s['worktree']})" if s["worktree"] else " (row names none)")
                + f"  branch: {s['branch'] or 'none'}")
            out.append(
                f"    evidence: no_worker={ev['no_worker']} "
                f"no_worktree={ev['no_worktree']} no_commit={ev['no_commit']}"
                + ("" if s["commit_hash"] is None
                   else f" commit={s['commit_hash']}"))
        out.append("")
        out.append("LIMITATION: liveness is the worktree proxy + the commit "
                    "field; there is no fleet-wide worker registry to consult.")
        out.append("RECOVERY: requeue rows EXPLICITLY with --requeue --id ... "
                    "(appends a board event; never a silent reset).")
    return "\n".join(out) + "\n"


def _now_utc():
    return datetime.now(timezone.utc)


def _event_id(now):
    """Event id in the board's own shape: YYYYMMDDHHMMSS (events.jsonl)."""
    return int(now.strftime("%Y%m%d%H%M%S"))


def requeue(board_path, events_path, ids, evidence_by_id, now):
    """Explicit recovery: append ONE event per row, rewrite that row only.

    The event detail carries status:pending AND the evidence triple — the
    board event is the record; the row rewrite is the consequence. Refuses
    ids that were not flagged this run (recovery must cite fresh evidence,
    not a stale memory of a stall).
    """
    rows, _bad = load_board(board_path)
    by_id = {r.get("id"): r for r in rows}
    touched, events = [], []
    for tid in ids:
        if tid not in evidence_by_id:
            raise ValueError(
                f"{tid}: not flagged by this run — requeue needs fresh "
                "evidence, re-run the check first")
        row = by_id[tid]
        ev = evidence_by_id[tid]["evidence"]
        detail = {"status": REQUEUE_STATUS, "reason": "stall-requeue",
                  "no_worker": ev["no_worker"],
                  "no_worktree": ev["no_worktree"],
                  "no_commit": ev["no_commit"]}
        events.append({"id": _event_id(now), "timestamp": _ts_utc(now),
                       "event_type": EVENT_TYPE, "task_id": tid,
                       "actor": EVENT_ACTOR, "detail": json.dumps(detail,
                                                                  sort_keys=True),
                       "tick_number": None})
        # Rewrite the ONE row by id (read-modify-append style: every other
        # byte of the file passes through untouched).
        updated = dict(row)
        for keep in REQUEUE_PRESERVE_FIELDS:
            if keep in row:
                updated[keep] = row[keep]
        updated["worker_status"] = REQUEUE_STATUS
        updated["status"] = row.get("status", REQUEUE_STATUS)
        updated["updated_at"] = _ts_utc(now)
        if "updated" in row:
            updated["updated"] = _ts_utc(now)
        updated["stall_requeue"] = {
            "at": _ts_utc(now),
            "evidence": detail,
            "note": "worker_status mirror reset to pending after stall "
                    "evidence; lifecycle status untouched",
        }
        touched.append((tid, updated))
    with open(board_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    with open(board_path, "w", encoding="utf-8") as fh:
        for line in lines:
            stripped = line.strip()
            replaced = False
            if stripped:
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    for tid, updated in touched:
                        if parsed.get("id") == tid:
                            fh.write(json.dumps(updated) + "\n")
                            replaced = True
                            break
            if not replaced:
                fh.write(line)
    with open(events_path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return [tid for tid, _ in touched]


def _ts_utc(dt):
    """Board timestamp spelling: ISO-8601 Z, seconds precision, UTC."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="flag in-flight board rows with no live worker and no "
                    "commit past a stated age (TR-196)")
    ap.add_argument("--board", default="tasks.jsonl", help="board tasks.jsonl")
    ap.add_argument("--events", default="events.jsonl",
                    help="board events.jsonl (requeue target)")
    ap.add_argument("--age-hours", type=float, default=DEFAULT_AGE_HOURS,
                    help=f"staleness bar in hours (default {DEFAULT_AGE_HOURS})")
    ap.add_argument("--age-field", default="created_at",
                    help="row timestamp field to measure age against "
                         "(default created_at; updated_at is NOT trusted — "
                         "TR-115/TR-117 stayed 'fresh' while being reaped)")
    ap.add_argument("--now", default=None,
                    help="ISO-8601 instant to measure against (default: wall "
                         "clock; pin it for a deterministic re-runnable check)")
    ap.add_argument("--requeue", action="store_true",
                    help="explicitly requeue flagged rows named by --id "
                         "(appends a board event per row; no silent reset)")
    ap.add_argument("--id", action="append", dest="ids", default=[],
                    help="row id to requeue (repeatable; requires --requeue)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable report on stdout")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing; exit code carries the verdict")
    args = ap.parse_args(argv)

    now = parse_now(args.now) or _now_utc()
    rows, bad = load_board(args.board)
    stalls = find_stalls(rows, now, args.age_hours,
                         age_field=args.age_field)
    evidence_by_id = {s["id"]: s for s in stalls}

    if args.requeue:
        if not args.ids:
            print("ERROR: --requeue requires at least one --id (recovery is "
                  "never bulk and never silent)", file=sys.stderr)
            return 2
        requeue(args.board, args.events, args.ids, evidence_by_id, now)
        if not args.quiet:
            print(f"requeued {len(args.ids)} row(s) with board events: "
                  f"{', '.join(args.ids)}")
        return 2

    report = {
        "board": args.board,
        "rows": len(rows),
        "unparseable_lines": bad,
        "age_field": args.age_field,
        "age_hours": args.age_hours,
        "now": _ts_utc(now),
        "stalls": stalls,
    }
    if args.json:
        print(json.dumps(report))
    elif not args.quiet:
        sys.stdout.write(render(report))
    return 0 if not stalls else 1


if __name__ == "__main__":
    sys.exit(main())
