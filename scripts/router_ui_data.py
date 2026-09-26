#!/usr/bin/env python3
"""TR-150..TR-156 — the DATA layer behind the router web UI.

The UI is a data command center: search the ledger, chart the traffic, follow one
request end to end, browse the board, and (edit-gated) change the registry. Every
function here answers one question over live files on disk. Nothing is cached
unless it says so, nothing is invented, and every answer states the window it
actually read.

Design rules carried from the router itself (TR-120/136/137/142/144):
  * A number that was not measured is None WITH A REASON — never 0.
  * An answer states how much of the source it scanned, so a truncated search can
    never look complete.
  * Priced averages disclose how many samples were priced.
  * A half-written trailing line in a live JSONL must not break a query.
  * Missing source file => explicit empty result with a reason, not an exception.

Stdlib only, like the rest of the runtime.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "data" / "state" / "outcomes.jsonl"
BOARD = REPO / ".coding-hermes" / "board" / "tasks.jsonl"
EVENTS = REPO / ".coding-hermes" / "board" / "events.jsonl"
STATE_DB = Path(os.path.expanduser("~/.hermes/state.db"))

# Scanned rows are bounded so a UI request can never run away on a 324k-row file.
MAX_SCAN = 400_000
PAGE_CEILING = 500
DEFAULT_LIMIT = 50


def _iter_jsonl(path: Path, max_scan: int = MAX_SCAN):
    """Yield (lineno, dict) for each complete JSON line.

    A live append can leave a torn final line; that line is skipped and counted,
    never allowed to raise. Returns the count via the generator's .sent value
    pattern is overkill here — callers use scan_stats() when they need totals.
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if i > max_scan:
                return
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # torn tail or corrupt row: skip, the scan counter still reports it
                yield i, None


def _row_ts(d: dict) -> float:
    for k in ("ts", "timestamp", "created_at", "completed_at"):
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _match(d: dict, q: str | None, flt: dict) -> bool:
    if not d:
        return False
    for key, want in flt.items():
        if want in (None, ""):
            continue
        v = d.get(key)
        if isinstance(v, list):
            v = ",".join(str(x) for x in v)
        if str(v).lower() != str(want).lower():
            return False
    if q:
        # free text across the whole row — the point of a data terminal
        if q.lower() not in json.dumps(d, default=str).lower():
            return False
    return True


def ledger_search(q=None, outcome=None, source=None, provider=None, model=None,
                  band=None, since=None, until=None, sort="ts", desc=True,
                  limit=DEFAULT_LIMIT, offset=0, path: Path = LEDGER) -> dict:
    """Search the outcome ledger. Honest about the window it read."""
    try:
        limit = max(1, min(int(limit), PAGE_CEILING))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        limit, offset = DEFAULT_LIMIT, 0
    since = float(since) if since else None
    until = float(until) if until else None

    flt = {"route_outcome": outcome, "complexity_source": source,
           "provider": provider, "model": model, "band": band}

    rows, scanned, malformed, matched = [], 0, 0, 0
    for _, d in _iter_jsonl(path):
        scanned += 1
        if d is None:
            malformed += 1
            continue
        ts = _row_ts(d)
        if since is not None and ts and ts < since:
            continue
        if until is not None and ts and ts > until:
            continue
        if not _match(d, q, flt):
            continue
        matched += 1
        rows.append(d)

    if sort:
        def key(d):
            v = d.get(sort)
            if v is None:
                # None sorts last regardless of direction, so a missing value can
                # never be mistaken for "smallest"
                return (1, 0)
            if isinstance(v, (int, float)):
                return (0, -float(v) if desc else float(v))
            return (0, str(v))
        rows.sort(key=key)

    page = rows[offset:offset + limit]
    return {
        "rows": page,
        "total_matched": matched,
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "rows_scanned": scanned,
        "rows_malformed": malformed,
        "source": str(path),
        "source_exists": path.exists(),
        "window": {"since": since, "until": until},
        "truncated": offset + len(page) < matched,
        "scan_limit": MAX_SCAN,
        "note": (None if matched else "no rows matched this query in the scanned window"),
    }


def _cost_stats(rows: list[dict]) -> dict:
    priced = [r.get("cost_usd") for r in rows if isinstance(r.get("cost_usd"), (int, float))]
    out = {"cost_samples": len(priced)}
    if priced:
        out["cost_usd_total"] = round(sum(priced), 6)
        out["cost_usd_per_task"] = round(sum(priced) / len(priced), 6)
        out["cost_reason"] = None
    else:
        out["cost_usd_total"] = None
        out["cost_usd_per_task"] = None
        out["cost_reason"] = "no priced samples in window"
    return out


def series(hours=24, bucket="hour", path: Path = LEDGER, now=None) -> dict:
    """Bucketed traffic: requests, unique lanes, tokens, cost, success rate, hops.

    Buckets with no traffic are omitted (never a fabricated zero line), and each
    bucket carries its own sample count so a 1-request spike cannot look like a trend.
    """
    try:
        hours = max(1, min(int(hours), 24 * 30))
    except (TypeError, ValueError):
        hours = 24
    step = 3600 if bucket == "hour" else 86400
    now = float(now if now is not None else time.time())
    start = now - hours * 3600

    scan = _scan_proxy_rows(path, start, now)
    rows = scan["rows"]
    buckets: dict[int, list[dict]] = {}
    for r in rows:
        b = int(_row_ts(r) // step) * step
        buckets.setdefault(b, []).append(r)

    out = []
    for b in sorted(buckets):
        rs = buckets[b]
        lanes = {f"{r.get('provider')}/{r.get('model')}" for r in rs if r.get("provider")}
        served = [r for r in rs if str(r.get("route_outcome") or r.get("success")).lower() in ("served", "true", "1", "ok")]
        hops = [len(r.get("ladder") or []) or r.get("steps") or r.get("hops_attempted") or 1 for r in rs]
        entry = {
            "bucket_start": b,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(b)),
            "requests": len(rs),
            "unique_lanes": len(lanes),
            "success_rate": round(len(served) / len(rs), 4),
            "tokens_in": sum(int(r.get("tokens_in") or 0) for r in rs),
            "tokens_out": sum(int(r.get("tokens_out") or 0) for r in rs),
            "one_hop": sum(1 for h in hops if h <= 1),
            "fell_back": sum(1 for h in hops if h > 1),
        }
        entry.update(_cost_stats(rs))
        entry["samples"] = len(rs)
        out.append(entry)

    return {
        "bucket": bucket,
        "hours": hours,
        "series": out,
        "requests_total": len(rows),
        "rows_scanned": scan["rows_scanned"],
        "proxy_rows_matched": scan["proxy_rows_matched"],
        "unique_lanes_total": len({f"{r.get('provider')}/{r.get('model')}" for r in rows if r.get("provider")}),
        "window": {"since": start, "until": now},
        "note": None if rows else "no proxied traffic in this window",
    }


def _scan_proxy_rows(path: Path, since: float | None = None, until: float | None = None) -> dict:
    """Proxy rows only (session_id starts with router-proxy), with honest counts."""
    rows = []
    scanned = malformed = 0
    for _, d in _iter_jsonl(path):
        scanned += 1
        if d is None:
            malformed += 1
            continue
        if not str(d.get("session_id") or "").startswith("router-proxy"):
            continue
        ts = _row_ts(d)
        if since is not None and ts and ts < since:
            continue
        if until is not None and ts and ts > until:
            continue
        rows.append(d)
    return {"rows": rows, "rows_scanned": scanned, "rows_malformed": malformed,
            "proxy_rows_matched": len(rows)}


def flow(key, path: Path = LEDGER) -> dict:
    """The whole story of ONE request: rating -> chain -> hops -> served lane -> cost.

    Accepts a session_id (`router-proxy-…`) or a gateway session uuid. Unknown key
    is a 404 for the caller, not an empty 200 (TR-153).
    """
    if not key:
        return {"error": "missing id", "searched_for": key, "status": 400}
    rows = []
    scanned = 0
    for _, d in _iter_jsonl(path):
        scanned += 1
        if d is None:
            continue
        if key in json.dumps(d, default=str):
            rows.append(d)
    if not rows:
        return {"error": "no request found for that id", "searched_for": key,
                "rows_scanned": scanned, "status": 404}
    row = rows[-1]
    ladder = row.get("ladder") or []
    served_hop = row.get("served_by_hop")
    steps = row.get("steps")
    if steps is None:
        steps = len(ladder)
    outcome = {
        "id": key,
        "session_id": row.get("session_id"),
        "gateway_session_id": row.get("gateway_session_id"),
        "parent_session_id": row.get("parent_session_id"),
        "when": _iso(_row_ts(row)),
        "route_outcome": row.get("route_outcome"),
        "failure_reason": row.get("failure_reason"),
        "degrade_reason": row.get("degrade_reason"),
        "rating": {
            "source": row.get("complexity_source"),
            "profile_id": row.get("profile_id"),
            "complexity_sig": row.get("complexity_sig"),
            "band": row.get("band"),
            "required_categories": row.get("required_categories"),
            "problems": row.get("complexity_problems"),
        },
        "chain": {"max_hops": row.get("max_hops"), "hops_attempted": row.get("hops_attempted"),
                  "served_by_hop": served_hop, "steps": steps},
        "served_by": {"provider": row.get("provider"), "model": row.get("model")},
        "tokens": {"in": row.get("tokens_in"), "out": row.get("tokens_out"),
                   "cache_read": row.get("cache_read_tokens"),
                   "cache_write": row.get("cache_write_tokens"),
                   "reasoning": row.get("tokens_reasoning")},
        "cost": {"usd": row.get("cost_usd"), "basis": row.get("price_basis"),
                 "reason": None if isinstance(row.get("cost_usd"), (int, float)) else "not priced"},
        "wall_time_s": row.get("wall_time_s"),
        "ladder": ladder,
        # TR-153/TR-145: the third artefact of the chain of custody, resolved for the
        # person looking at the screen. A UI that cannot say whether Hermes has a record
        # of the request cannot be used to debug one.
        "hermes_session": hermes_session(row.get("gateway_session_id")),
        "raw": row,
    }
    return outcome


def _iso(ts: float) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def hermes_session(gateway_session_id: str, db: Path = STATE_DB) -> dict:
    """Does the gateway session the router recorded actually exist in Hermes?

    This is the third artefact of the chain of custody (TR-145). A missing row is
    reported as missing — the UI must never imply a request ran when Hermes has no
    record of it.
    """
    if not gateway_session_id:
        return {"found": None, "reason": "no gateway session id recorded on this request"}
    if not db.exists():
        return {"found": None, "reason": f"state.db not present at {db}"}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.execute("SELECT id, source, model FROM sessions WHERE id = ?", (gateway_session_id,))
            r = cur.fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — a UI read must not raise
        return {"found": None, "reason": f"state.db read failed: {str(exc)[:160]}"}
    if not r:
        return {"found": False, "reason": "no such session in state.db"}
    return {"found": True, "session_id": r[0], "source": r[1], "model": r[2]}


def board(search=None, status=None, priority=None, owner=None, limit=100, offset=0,
          path: Path = BOARD) -> dict:
    """The router board: find tasks, their times, their rows. Read-only."""
    try:
        limit = max(1, min(int(limit), PAGE_CEILING))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        limit, offset = 100, 0

    rows, scanned, malformed = [], 0, 0
    for _, d in _iter_jsonl(path):
        scanned += 1
        if d is None:
            malformed += 1
            continue
        rows.append(d)

    ids = [r.get("id") for r in rows]
    dups = {}
    for i in ids:
        dups[i] = dups.get(i, 0) + 1
    duplicates = {k: v for k, v in dups.items() if v > 1 and k}

    sel = [r for r in rows if _match(r, search, {"status": status, "priority": priority, "owner": owner})]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1

    return {
        "rows": sel[offset:offset + limit],
        "total_matched": len(sel),
        "board_rows": len(rows),
        "duplicate_ids": duplicates,
        "by_status": by_status,
        "rows_scanned": scanned,
        "rows_malformed": malformed,
        "source": str(path),
        "note": "board id census included: a duplicate id means two rows claim the same work",
    }
