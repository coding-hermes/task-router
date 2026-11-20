#!/usr/bin/env python3
"""cos_view_freshness.py — prove the COS `initiatives` read view is live, or name why not.

The fleet COS reasons from the DuckBrain `cos` namespace, and its `initiatives`
table is the attribution plane: every kanban card is one row, and the `project`
column is derived from the card's `[tag]` title prefix. When that view goes
stale, cards that exist on a board are invisible to attribution queries — a
card then looks like it "failed to sync" when the write was fine and only the
read is old. That false negative is fleet-wide, not per-project.

WHY A STALE VIEW IS POSSIBLE AT ALL (measured 2026-11-20, cos/initiatives):

  * the table is declared with a SINGLE-FILE storage path
    (``tables/initiatives/current.jsonl``, no wildcard);
  * DuckBrain's JSONL writer rotates: once a segment passes its bound, writes
    go to the newest NUMERIC segment and ``current.jsonl`` is never appended
    again (``src/storage/jsonl.ts`` — "Writers continue to pass current.jsonl
    after the first rotation ... always [resolve to] the newest numeric
    segment"); segment consolidation explicitly leaves ``current.jsonl`` alone;
  * the table read path resolves the declaration's path to a file list
    (``resolveTableFiles``) and does NOT dedupe by primary key.

  So a rotated table's read view freezes at the pre-rotation row count while
  the partitions keep growing. On 2026-11-20 the cos/initiatives directory held
  22 files / 12,044 lines / 969 distinct initiative ids, and the API served
  619 rows whose newest ``created_at`` was 2026-11-16T04:56:10Z.

  A naive "just glob the segments" repair is WRONG for this table: upserts
  append a new version of a row, so the segments hold 12,044 lines for 969
  ids. The read needs newest-version-wins dedupe by the declared key columns,
  which the table read path does not implement. That repair belongs in
  DuckBrain, not in the sync that writes the rows.

WHAT THIS SCRIPT DOES. It runs the three gate arms the card asks for and
reports each one honestly, so the defect is caught on every run instead of
being rediscovered by hand:

  arm 1  freshness   newest ``created_at`` in ``current.jsonl`` is within
                     ``--max-age-hours`` of ``--now``
  arm 2  visibility  a card minted inside the window reads back from the
                     PostgREST table endpoint as exactly one row, and its
                     ``project`` matches the card's ``[tag]`` prefix
  arm 3  coverage    the endpoint's exact row count is at least the sum of
                     per-board card counts

It also reports the rotation state (numeric segments present, distinct ids vs
lines) so a failure names its own cause instead of just saying "stale".

Exit codes: 0 every arm passed; 1 at least one arm failed; 2 the check could
not run (no token file, endpoint unreachable). A check that cannot look never
reports PASS.

The token is read from ``~/.duckbrain/cos-sync.token`` and is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_NS_DIR = Path(os.path.expanduser("~/duckbrain/namespaces/cos"))
DEFAULT_TOKEN_FILE = Path(os.path.expanduser("~/.duckbrain/cos-sync.token"))
DEFAULT_BASE = "http://127.0.0.1:3000/api/ns/cos/tables"
DEFAULT_BOARDS = Path(os.path.expanduser("~/.hermes/kanban/boards"))

PASS = "pass"
FAIL = "fail"
SKIP = "skip"


# --------------------------------------------------------------------------
# pure helpers (unit-tested without a network or a live store)
# --------------------------------------------------------------------------
def parse_iso(value: str) -> datetime | None:
    """Parse the ISO-8601 spellings DuckBrain writes; None when unparseable."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def newest_created_at(rows: list[dict]) -> datetime | None:
    """Newest parseable ``created_at`` across rows (None when none parse)."""
    newest = None
    for row in rows:
        stamp = parse_iso(row.get("created_at") or "")
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    return newest


def project_from_title(title: str) -> str | None:
    """The COS project tag: the leading ``[tag]`` of a card title, else None."""
    text = title or ""
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    return None


def segment_state(table_dir: Path) -> dict:
    """Rotation state of one table directory: segments, lines, distinct ids."""
    files = sorted(p for p in table_dir.glob("*.jsonl") if p.is_file())
    numeric = [p for p in files if re.fullmatch(r"\d+", p.stem)]
    lines = 0
    ids: set[str] = set()
    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    lines += 1
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        ident = row.get("initiative_id")
                        if isinstance(ident, str) and ident:
                            ids.add(ident)
        except OSError:
            continue
    return {
        "files": len(files),
        "numeric_segments": len(numeric),
        "rotated": bool(numeric),
        "lines": lines,
        "distinct_ids": len(ids),
        "duplicate_versions": max(0, lines - len(ids)),
    }


def evaluate_arms(
    *,
    newest: datetime | None,
    now: datetime,
    max_age_hours: float,
    visible_rows: list[dict] | None,
    expected_project: str | None,
    api_total: int | None,
    board_total: int | None,
) -> list[dict]:
    """Score the three gate arms. Pure: every input is already measured."""
    arms: list[dict] = []

    if newest is None:
        arms.append({"arm": "freshness", "status": FAIL,
                     "detail": "no parseable created_at in current.jsonl"})
    else:
        age_hours = (now - newest).total_seconds() / 3600.0
        ok = age_hours <= max_age_hours
        arms.append({
            "arm": "freshness",
            "status": PASS if ok else FAIL,
            "detail": (f"newest created_at {newest.isoformat()} is "
                       f"{age_hours:.1f}h old (limit {max_age_hours:g}h)"),
            "age_hours": round(age_hours, 2),
        })

    if visible_rows is None:
        arms.append({"arm": "visibility", "status": SKIP,
                     "detail": "no fresh card id supplied (--card)"})
    elif len(visible_rows) != 1:
        arms.append({"arm": "visibility", "status": FAIL,
                     "detail": f"expected exactly 1 row, got {len(visible_rows)}"})
    else:
        got = visible_rows[0].get("project")
        ok = expected_project is None or got == expected_project
        arms.append({
            "arm": "visibility",
            "status": PASS if ok else FAIL,
            "detail": (f"1 row; project={got!r} "
                       f"(expected {expected_project!r})"),
        })

    if api_total is None or board_total is None:
        arms.append({"arm": "coverage", "status": SKIP,
                     "detail": "row count or board total unavailable"})
    else:
        ok = api_total >= board_total
        arms.append({
            "arm": "coverage",
            "status": PASS if ok else FAIL,
            "detail": f"api rows {api_total} vs board cards {board_total}",
        })

    return arms


def overall(arms: list[dict]) -> str:
    """FAIL if any arm failed, else PASS (a skipped arm never upgrades to PASS)."""
    if any(a["status"] == FAIL for a in arms):
        return FAIL
    if any(a["status"] == SKIP for a in arms):
        return SKIP
    return PASS


# --------------------------------------------------------------------------
# measurement (I/O)
# --------------------------------------------------------------------------
def read_current_rows(table_dir: Path) -> list[dict]:
    path = table_dir / "current.jsonl"
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def board_card_total(boards_dir: Path) -> int:
    """Sum of card counts across every per-board kanban DB."""
    total = 0
    for db_path in sorted(boards_dir.glob("*/kanban.db")):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                total += int(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            continue
    return total


def newest_card(boards_dir: Path) -> dict | None:
    """The most recently created card across every board (id + title)."""
    best: dict | None = None
    for db_path in sorted(boards_dir.glob("*/kanban.db")):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT id, title, created_at FROM tasks "
                    "ORDER BY created_at DESC LIMIT 1").fetchone()
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            continue
        if row and (best is None or int(row[2] or 0) > int(best["created_at"] or 0)):
            best = {"id": row[0], "title": row[1], "created_at": int(row[2] or 0)}
    return best


def _request(url: str, token: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"X-API-Key": token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else None), resp.headers.get("X-Total-Count")


def probe_api(base: str, token: str, card_id: str | None) -> tuple[list[dict] | None, int | None]:
    """(rows for card_id, exact table row count). Raises on transport failure."""
    rows = None
    if card_id:
        status, data, _ = _request(f"{base}/initiatives?initiative_id=eq.{card_id}", token)
        if status == 200 and isinstance(data, list):
            rows = data
    status, _, total = _request(f"{base}/initiatives?count=exact&limit=1", token)
    api_total = int(total) if total is not None and str(total).isdigit() else None
    return rows, api_total


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prove the COS initiatives read view is live (or name why not).")
    parser.add_argument("--ns-dir", default=str(DEFAULT_NS_DIR),
                        help="cos namespace directory (default: ~/duckbrain/namespaces/cos)")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE),
                        help="DuckBrain API key file (never printed)")
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="tables endpoint base URL")
    parser.add_argument("--boards-dir", default=str(DEFAULT_BOARDS),
                        help="kanban boards directory")
    parser.add_argument("--card", default=None,
                        help="card id for the visibility arm (default: newest card)")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="freshness limit in hours (default: 24)")
    parser.add_argument("--now", default=None,
                        help="ISO-8601 reference time (default: wall clock)")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)

    now = parse_iso(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        print(f"cos_view_freshness: unparseable --now {args.now!r}", file=sys.stderr)
        return 2

    table_dir = Path(args.ns_dir) / "tables" / "initiatives"
    if not table_dir.is_dir():
        print(f"cos_view_freshness: no initiatives table at {table_dir}", file=sys.stderr)
        return 2

    rows = read_current_rows(table_dir)
    newest = newest_created_at(rows)
    segments = segment_state(table_dir)

    card = {"id": args.card} if args.card else newest_card(Path(args.boards_dir))
    card_id = card.get("id") if card else None
    expected_project = project_from_title(card.get("title") or "") if card else None

    visible_rows = None
    api_total = None
    transport_error = None
    token_file = Path(args.token_file)
    if not token_file.is_file():
        transport_error = f"token file missing: {token_file}"
    else:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
            visible_rows, api_total = probe_api(args.base, token, card_id)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            transport_error = f"{type(exc).__name__}: {exc}"

    board_total = board_card_total(Path(args.boards_dir))

    arms = evaluate_arms(
        newest=newest, now=now, max_age_hours=args.max_age_hours,
        visible_rows=visible_rows, expected_project=expected_project,
        api_total=api_total, board_total=board_total,
    )
    verdict = overall(arms)

    report = {
        "verdict": verdict,
        "checked_at": now.isoformat(),
        "table_dir": str(table_dir),
        "current_rows": len(rows),
        "segments": segments,
        "card": card_id,
        "expected_project": expected_project,
        "api_total": api_total,
        "board_total": board_total,
        "transport_error": transport_error,
        "arms": arms,
    }
    if transport_error and verdict != FAIL:
        report["verdict"] = "error"

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"COS initiatives view: {report['verdict'].upper()}")
        print(f"  current.jsonl: {len(rows)} rows, newest {newest.isoformat() if newest else 'n/a'}")
        print(f"  segments: {segments['files']} files, {segments['numeric_segments']} numeric, "
              f"{segments['lines']} lines / {segments['distinct_ids']} distinct ids")
        for arm in arms:
            print(f"  [{arm['status']}] {arm['arm']}: {arm['detail']}")
        if transport_error:
            print(f"  transport: {transport_error}")
        if segments["rotated"] and verdict != PASS:
            print("  cause: the table has rotated past current.jsonl; the declared "
                  "single-file storage path cannot see the numeric segments "
                  "(repair belongs in DuckBrain's table read path, with "
                  "newest-version-wins dedupe by key columns)")

    if report["verdict"] == "error":
        return 2
    return 0 if report["verdict"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
