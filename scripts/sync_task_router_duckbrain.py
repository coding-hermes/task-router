#!/usr/bin/env python3
"""sync_task_router_duckbrain.py — deterministic write path for the task-router-sync lane (TR-204).

Why this exists: the 2026-09-27 tick (task-router-sync-2026-09-27-01-35-14) failed 2
writes against POST /api/memories' 102400-byte JSON body cap (Express default,
duckbrain src/cli/http.ts — plain express.json()), reported them with EMPTY reasons
(the old ad-hoc lane logged only a subprocess' stderr while the HTTP error went to
stdout), and then still appended a /sync/task-router/last-success marker whose content
claimed SUCCESS with an unsubstituted window placeholder (the literal 'window ...').
TR-204 makes this class of failure impossible:

  1. Diagnosability — every HTTP call captures BOTH the status and the response body
     (urllib raises HTTPError carrying the body; there is no stdout/stderr split to
     lose). Any rejected write is reported as:
         FAIL <key>: HTTP <status> <body>
     and recorded in the run report JSON with both fields.
  2. No false success marker — the marker is written LAST and ONLY when the run has
     zero failed writes/verifications. A failed run writes NO new row under
     /sync/task-router/last-success and exits 1. Additionally the marker content is
     passed through a placeholder guard before posting: content containing a
     templated token ('...', '{name}', '%s', '<ts>') is refused loudly instead of
     being written.
  3. Over-cap snapshot path (DECISION, option b of the board row) — registry table
     snapshots do NOT go through POST /api/memories at all. They are appended, in
     row-chunks, to DECLARED DuckBrain tables via the existing 1mb
     application/x-ndjson mount:
         POST /api/ns/task-router/tables/snapshot_<table>   (Content-Type: application/x-ndjson)
     The lane writes the declarations (tables/snapshot_<t>.table.json) idempotently
     and appends to tables/snapshots/<t>.jsonl — a lane-owned subtree; it never
     touches the registry mirror files themselves (AGENTS.md: generated tables are
     rebuilt via router_seed.py, not hand-edited). Chunks are capped at 512 KiB /
     4000 rows, so a growing registry can never re-hit a body cap; run metadata
     (snap_ts, sha8 of the source file) rides on every row so a run's rows are
     queryable and the read-back is provable (Prefer: count=exact + sampled content
     equality). Unchanged tables (same sha8 already present) are skipped with a
     named dedupe note instead of re-appending megabytes.
  4. Size preflight, no silent skip — every /api/memories fact payload is serialized
     and measured BEFORE posting; anything over the body cap is named (key, size,
     cap) in both the report and the marker and never sent, instead of failing at
     the first over-cap table with an empty reason.

Usage (canonical source: task-router repo scripts/; live install:
~/.hermes/scripts/sync_task_router_duckbrain.py via scripts/sync_runtime.sh):

  python3 sync_task_router_duckbrain.py run \
      --window-start 2026-09-30T04:20Z --window-end 2026-09-30T10:05Z \
      --facts-file facts-<run>.json [--git-head <sha>] [--note "..."] [--dry-run]

  python3 sync_task_router_duckbrain.py probe-overcap        # live AC1 proof: force an
                                                             # over-cap write, require the
                                                             # failure line to name status+body

  python3 sync_task_router_duckbrain.py run ... --force-fail # live AC2 proof: simulate
                                                             # every write failing; assert no
                                                             # marker row is written (zero HTTP writes)

Exit codes: 0 = success (or PARTIAL with named skips) and marker written;
1 = at least one failed write/verification -> NO marker; 2 = environment/preflight
abort (no marker); 3 = write-test failed (no marker); 4 = usage.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = os.environ.get("DUCKBRAIN_URL", "http://localhost:3000")
DEFAULT_NS_DIR = os.environ.get(
    "DUCKBRAIN_NAMESPACES", str(Path.home() / "duckbrain" / "namespaces")
)
# The task-router ns authenticates with schedulerd-sync.token (lane recipe rule 8:
# the first-*.token glob can pick a valid-but-different token and every write 403s).
DEFAULT_TOKEN_FILE = str(Path.home() / ".duckbrain" / "schedulerd-sync.token")
NAMESPACE = "task-router"
MARKER_KEY = "/sync/task-router/last-success"
DOMAINS = ("person", "event", "concept", "message", "config", "raw_note")

# POST /api/memories parses with plain express.json() (duckbrain src/cli/http.ts,
# DB-SUPA-3 rework block) -> 100 KiB default body cap. Over-cap bodies answer
# HTTP 500 INTERNAL_ERROR and write nothing (TR-204 repro 2026-09-27: 101567B
# parsed, 102467B rejected).
MEMORIES_BODY_CAP = 100 * 1024

# The tables NDJSON mount parses with express.text({ type: 'application/x-ndjson',
# limit: '1mb' }). Chunks stay an order of magnitude below it so a growing
# registry never re-hits the cap.
NDJSON_MOUNT_CAP = 1024 * 1024
CHUNK_BODY_BYTES = 512 * 1024
CHUNK_ROWS = 4000

# The 5 over-cap routing tables named in the board row (sizes 2026-09-27:
# models 797786B, model_catalog 409534B, model_tier 241325B, model_perf 233983B,
# model_notes 114152B).
SNAPSHOT_TABLES = (
    "models",
    "model_catalog",
    "model_tier",
    "model_perf",
    "model_notes",
)
SNAPSHOT_COLUMNS = (
    {"name": "snap_ts", "type": "varchar"},
    {"name": "sha8", "type": "varchar"},
    {"name": "row", "type": "json"},
)

# A marker must never carry an unsubstituted template token (the bad 09-27
# marker contained the literal 'window ...'). Refuse ellipses, {name} slots,
# printf slots and <slot> angle tokens anywhere in the content.
MARKER_PLACEHOLDER_RE = re.compile(r"\.\.\.|\{[a-zA-Z_]+\}|%[sdf]|<[a-zA-Z_][a-zA-Z0-9_]*>")


# ---------------------------------------------------------------------------
# Transport — every call returns (status, body_text) and never raises, so a
# rejection can never be lost to an exception path or a stream-split.
# ---------------------------------------------------------------------------
def http_request(method, url, token, data=None, content_type="application/json",
                 extra_headers=None, timeout=90):
    headers = {"Accept": "application/json"}
    if token:
        headers["X-API-Key"] = token
    if data is not None:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers or {})
    except urllib.error.URLError as exc:
        return 0, "NETWORK-ERROR " + str(exc.reason), {}
    except OSError as exc:
        return 0, "NETWORK-ERROR " + str(exc), {}


def jload(text):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 — body may be HTML/plain text; caller handles None
        return None


def utcnow():
    return datetime.now(timezone.utc)


def run_ts(dt=None):
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Marker guard (AC 2b)
# ---------------------------------------------------------------------------
def marker_content_is_substituted(content):
    """True when the marker content carries no unsubstituted template token."""
    return MARKER_PLACEHOLDER_RE.search(content) is None


# ---------------------------------------------------------------------------
# Facts (POST /api/memories) with size preflight and full failure capture
# ---------------------------------------------------------------------------
def post_memory(base, ns, token, key, domain, content, attributes=None):
    body = {"key": key, "domain": domain, "content": content}
    body["attributes"] = attributes or {"source_type": "sync_cron",
                                        "certainty": "confirmed"}
    payload = json.dumps(body, separators=(",", ":")).encode()
    url = f"{base}/api/memories?namespace={urllib.parse.quote(ns)}"
    status, text, _ = http_request("POST", url, token, data=payload)
    return status, text, len(payload)


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def verify_memory(base, ns, token, key, uid, since_floor, limit=5):
    """Prefix-GET with widen-once; a write is proven when any returned entry
    carries the key, this run's uuid, and a timestamp at/after the floor."""
    floor = _parse_ts(since_floor)
    q_limit = limit
    for _ in range(2):
        q = urllib.parse.urlencode({"namespace": ns, "prefix": key,
                                    "limit": str(q_limit)})
        status, text, _ = http_request(
            "GET", f"{base}/api/memories?{q}", token)
        payload = jload(text) or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        items = items or (payload if isinstance(payload, list) else [])
        for it in items:
            if it.get("key") != key:
                continue
            if uid and it.get("id") != uid:
                continue
            ts = str(it.get("timestamp") or "")
            parsed = _parse_ts(ts)
            fresh = (parsed >= floor) if (parsed and floor) else ts >= since_floor
            if fresh:
                return True, status, ts
        if q_limit >= 40:
            break
        q_limit = 40  # shared hot prefixes paginate: widen once before a miss
    return False, status, None


def run_facts(base, ns, token, facts, since_floor, between_posts, report,
              dry_run=False, fail_pattern=None):
    """Returns (written, failures, skips). Over-cap facts are named skips —
    never silent, never sent. Failed posts are named failures with status+body.
    A key matching fail_pattern fails WITHOUT any HTTP call (simulated)."""
    written, failures, skips = 0, [], []
    for i, fact in enumerate(facts):
        key, domain = fact.get("key", ""), fact.get("domain", "")
        content = fact.get("content", "")
        if fail_pattern and re.search(fail_pattern, key):
            failures.append({"key": key, "status": 500,
                             "body": '{"error":"INTERNAL_ERROR"} (simulated)'})
            report(f"FAIL {key}: HTTP 500 " + '{"error":"INTERNAL_ERROR"} (simulated)')
            continue
        if domain not in DOMAINS:
            failures.append({"key": key, "status": 0,
                             "body": f"invalid domain '{domain}'"})
            report(f"FAIL {key}: invalid domain '{domain}'")
            continue
        size = len(json.dumps(
            {"key": key, "domain": domain, "content": content,
             "attributes": fact.get("attributes")
             or {"source_type": "sync_cron", "certainty": "confirmed"}},
            separators=(",", ":")).encode())
        if size > MEMORIES_BODY_CAP:
            skips.append({"key": key, "size": size, "cap": MEMORIES_BODY_CAP})
            report(f"SKIP (over body cap) {key}: size={size} cap={MEMORIES_BODY_CAP}")
            continue
        if dry_run:
            report(f"would write {key} [{domain}] {len(content)} chars (body {size}B)")
            written += 1
            continue
        status, text, _ = post_memory(base, ns, token, key, domain, content,
                                      fact.get("attributes"))
        if status != 201:
            failures.append({"key": key, "status": status, "body": text[:400]})
            report(f"FAIL {key}: HTTP {status} {text[:200]}")
            continue
        payload = jload(text) or {}
        uid = payload.get("id")
        if not uid:
            failures.append({"key": key, "status": status,
                             "body": "201 without id; cannot prove"})
            report(f"FAIL {key}: HTTP 201 without id (unprovable)")
            continue
        ok, gstatus, ts = verify_memory(base, ns, token, key, uid, since_floor)
        if not ok:
            failures.append({"key": key, "status": status,
                             "body": f"posted id={uid} but read-back unproven "
                                     f"(get HTTP {gstatus})"})
            report(f"FAIL {key}: posted id={uid[:8]} but read-back unproven")
            continue
        written += 1
        report(f"OK {key}: HTTP 201 id={uid[:8]} ts={ts}")
        if i < len(facts) - 1 and not dry_run:
            time.sleep(between_posts)
    return written, failures, skips


def plan_fact_sizes(facts):
    """Serialize every fact payload and return (ok, skipped) — AC 4: nothing
    over the body cap is silently dropped; the caller names each skip."""
    ok, skipped = [], []
    for fact in facts:
        body = {"key": fact.get("key", ""), "domain": fact.get("domain", ""),
                "content": fact.get("content", ""),
                "attributes": fact.get("attributes")
                or {"source_type": "sync_cron", "certainty": "confirmed"}}
        size = len(json.dumps(body, separators=(",", ":")).encode())
        if size > MEMORIES_BODY_CAP:
            skipped.append({"key": fact.get("key", ""), "size": size,
                            "cap": MEMORIES_BODY_CAP})
        else:
            ok.append(fact)
    return ok, skipped


# ---------------------------------------------------------------------------
# Snapshot tables (over-cap path, DECISION b): declared NDJSON tables
# ---------------------------------------------------------------------------
def declaration_path(ns_dir, table):
    return Path(ns_dir) / NAMESPACE / "tables" / f"snapshot_{table}.table.json"


def declaration_doc(table):
    return {
        "name": f"snapshot_{table}",
        "format": "jsonl-objects",
        "glob": f"tables/snapshots/{table}.jsonl",
        "columns": [dict(c) for c in SNAPSHOT_COLUMNS],
    }


def ensure_declarations(ns_dir, report, dry_run=False):
    """Idempotently write the .table.json declarations. Legacy declarations are
    picked up by the server's mtime/size staleness check with no restart."""
    written = 0
    for table in SNAPSHOT_TABLES:
        path = declaration_path(ns_dir, table)
        doc = json.dumps(declaration_doc(table), indent=1) + "\n"
        if path.exists() and path.read_text() == doc:
            continue
        if dry_run:
            report(f"would write declaration {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc)
        written += 1
        report(f"declaration written: {path}")
    return written


def table_source_path(ns_dir, table):
    return Path(ns_dir) / NAMESPACE / "tables" / f"{table}.jsonl"


def build_chunks(table, lines, snap_ts, sha8):
    """ NDJSON row chunks: {"snap_ts","sha8","row"} per source line, each chunk
    <= CHUNK_BODY_BYTES and <= CHUNK_ROWS, and always < NDJSON_MOUNT_CAP."""
    chunks, cur, cur_bytes = [], [], 0
    for line in lines:
        obj = {"snap_ts": snap_ts, "sha8": sha8, "row": json.loads(line)}
        enc = json.dumps(obj, separators=(",", ":")).encode()
        if cur and (cur_bytes + len(enc) + 1 > CHUNK_BODY_BYTES
                    or len(cur) >= CHUNK_ROWS):
            chunks.append(b"\n".join(cur) + b"\n")
            cur, cur_bytes = [], 0
        cur.append(enc)
        cur_bytes += len(enc) + 1
    if cur:
        chunks.append(b"\n".join(cur) + b"\n")
    for c in chunks:
        if len(c) >= NDJSON_MOUNT_CAP:
            raise ValueError(
                f"chunk {len(c)}B exceeds NDJSON mount cap {NDJSON_MOUNT_CAP} "
                f"(table {table}) — reduce CHUNK_BODY_BYTES")
    return chunks


def tables_url(base, table):
    return (f"{base}/api/ns/{NAMESPACE}/tables/snapshot_{table}")


def _norm_json(value):
    """Canonical comparable HASHABLE form: sorted keys, integral floats
    normalized (1.0 == 1 — DuckDB JSON round-trips can rewrite integral
    floats), lists as tuples so rows can live in a set."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        f = float(value)
        return int(f) if f.is_integer() else f
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return tuple(_norm_json(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _norm_json(v)) for k, v in value.items()))
    return str(value)


def readback_count(base, token, table, filters):
    # count=exact rides BOTH the query and the Prefer header: the server's
    # wantsExactCount accepts either, and an explicit query param keeps the
    # count independent of header-stripping proxies.
    q = filters + "&limit=1&count=exact"
    status, text, headers = http_request(
        "GET", tables_url(base, table) + "?" + q, token,
        extra_headers={"Prefer": "count=exact"})
    total = headers.get("X-Total-Count")
    return status, text, (int(total) if total and total.isdigit() else None)


def snapshot_table(base, token, ns_dir, table, snap_ts, between_posts, report,
                   dry_run=False, simulate_fail=False):
    """Snapshot one registry table through the NDJSON tables mount.
    Returns a result dict; 'verified' False + note means a failed write."""
    src = table_source_path(ns_dir, table)
    if not src.exists():
        return {"table": table, "status": "missing-source",
                "note": f"{src} not found", "verified": False}
    raw = src.read_bytes()
    sha8 = hashlib.sha256(raw).hexdigest()[:8]
    lines = [l for l in raw.decode().splitlines() if l.strip()]
    for l in lines:
        try:
            json.loads(l)
        except json.JSONDecodeError as exc:
            return {"table": table, "status": "bad-source-line",
                    "note": f"{src}: {exc}", "verified": False, "sha8": sha8}

    # Dedupe: is this exact source already snapshotted?
    status, text, total = readback_count(base, token, table,
                                         f"sha8=eq.{urllib.parse.quote(sha8)}")
    if status == 200 and total:
        report(f"TABLE {table}: unchanged (sha8={sha8}, {total} rows on file) — dedupe skip")
        return {"table": table, "status": "unchanged", "sha8": sha8,
                "size": len(raw), "rows": len(lines), "verified": True}
    if status not in (0, 200, 404):
        # A broken read path is a failure, not a silent skip.
        return {"table": table, "status": "dedupe-read-failed",
                "note": f"HTTP {status} {text[:200]}", "sha8": sha8,
                "verified": False}

    if dry_run:
        report(f"TABLE {table}: would post {len(lines)} rows "
               f"(sha8={sha8}, {len(raw)}B) to snapshot_{table}")
        return {"table": table, "status": "dry-run", "sha8": sha8,
                "size": len(raw), "rows": len(lines), "verified": True}
    if simulate_fail:
        return {"table": table, "status": "failed (simulated)",
                "note": "500 INTERNAL_ERROR (simulated)", "sha8": sha8,
                "size": len(raw), "rows": len(lines), "verified": False}

    try:
        chunks = build_chunks(table, lines, snap_ts, sha8)
    except ValueError as exc:
        return {"table": table, "status": "chunking-failed", "note": str(exc),
                "sha8": sha8, "verified": False}
    posted = 0
    for ci, chunk in enumerate(chunks, 1):
        status, text, _ = http_request(
            "POST", tables_url(base, table), token, data=chunk,
            content_type="application/x-ndjson")
        if status != 201:
            return {"table": table, "status": "post-failed",
                    "note": f"chunk {ci}/{len(chunks)}: HTTP {status} {text[:200]}",
                    "sha8": sha8, "size": len(raw), "rows": len(lines),
                    "posted": posted, "verified": False}
        payload = jload(text) or {}
        posted += int(payload.get("inserted") or 0)
        report(f"TABLE {table}: chunk {ci}/{len(chunks)} HTTP 201 "
               f"inserted={payload.get('inserted')}")
        if ci < len(chunks):
            time.sleep(between_posts)

    # Read-back proof: exact row count for this run + sampled content equality.
    rstatus, rtext, rtotal = readback_count(
        base, token, table, "snap_ts=eq." + urllib.parse.quote(snap_ts))
    if rstatus != 200 or rtotal != len(lines):
        return {"table": table, "status": "readback-failed",
                "note": f"count read-back HTTP {rstatus} "
                        f"total={rtotal} expected={len(lines)}",
                "sha8": sha8, "size": len(raw), "rows": len(lines),
                "posted": posted, "verified": False}
    q = urllib.parse.urlencode({"snap_ts": snap_ts, "limit": "3"})
    sstatus, stext, _ = http_request("GET", tables_url(base, table) + "?" + q, token)
    sampled = jload(stext) or []
    src_norm = {_norm_json(json.loads(l)) for l in lines}
    mismatch = 0
    for r in sampled[:3]:
        if _norm_json(r.get("row")) not in src_norm:
            mismatch += 1
    if mismatch:
        return {"table": table, "status": "readback-mismatch",
                "note": f"{mismatch} sampled rows do not match source",
                "sha8": sha8, "size": len(raw), "rows": len(lines),
                "posted": posted, "verified": False}
    report(f"TABLE {table}: verified rows={rtotal} sha8={sha8} "
           f"chunks={len(chunks)} readback=OK")
    return {"table": table, "status": "posted", "sha8": sha8,
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
            "rows": len(lines), "posted": posted, "chunks": len(chunks),
            "readback": rtotal, "verified": True}


# ---------------------------------------------------------------------------
# Run report / marker
# ---------------------------------------------------------------------------
class Reporter:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)
        print(line, flush=True)


def build_marker_content(outcome, window_start, window_end, written, n_facts,
                         table_results, skipped, extra=None):
    posted = [r for r in table_results if r.get("status") == "posted"]
    unchanged = [r for r in table_results if r.get("status") == "unchanged"]
    parts = [f"task-router-sync {outcome} {window_end}",
             f"window {window_start}..{window_end}",
             f"{written} keys written ({n_facts} facts + 1 write-test + "
             f"{sum(r.get('posted', 0) for r in posted)} snapshot rows)",
             "preflight write-test OK",
             "tables: " + (", ".join(
                 f"{r['table']}={r.get('rows')}@{r.get('sha8')} posted"
                 for r in posted)
                 + ("; " if posted and unchanged else "")
                 + ", ".join(f"{r['table']}={r.get('rows')}@{r.get('sha8')} unchanged"
                             for r in unchanged) or "none"),
             "skipped: " + (", ".join(
                 f"{s['key']} ({s['size']}B > cap {s['cap']}B)" for s in skipped)
                 or "none")]
    if extra:
        parts.append(extra)
    return " — ".join(parts[:4]) + ". " + "; ".join(parts[4:])


def post_marker(base, ns, token, content, since_floor, report):
    status, text, _ = post_memory(base, ns, token, MARKER_KEY, "config", content)
    if status != 201:
        report(f"MARKER: WRITE FAILED HTTP {status} {text[:200]}")
        return None
    uid = (jload(text) or {}).get("id")
    ok, gstatus, ts = verify_memory(base, ns, token, MARKER_KEY, uid, since_floor)
    if not ok:
        report(f"MARKER: posted id={uid} but read-back unproven (HTTP {gstatus})")
        return None
    report(f"MARKER: WRITTEN id={uid[:8]} ts={ts}")
    return {"written": True, "id": uid, "ts": ts}


def newest_marker_timestamp(base, token):
    """Newest row under the marker key, for before/after assertions."""
    q = urllib.parse.urlencode({"namespace": NAMESPACE,
                                "prefix": MARKER_KEY, "limit": "5"})
    status, text, _ = http_request("GET", f"{base}/api/memories?{q}", token)
    payload = jload(text) or {}
    items = payload.get("items") if isinstance(payload, dict) else None
    items = items or []
    return max((str(i.get("timestamp") or "") for i in items), default=None)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def load_token(explicit):
    if explicit:
        return Path(explicit).read_text().strip()
    env = os.environ.get("DUCKBRAIN_TOKEN_FILE")
    if env:
        return Path(env).read_text().strip()
    cands = sorted(Path.home().joinpath(".duckbrain").glob("*.token"))
    if not cands:
        sys.exit("no DuckBrain token: pass --token-file or set DUCKBRAIN_TOKEN_FILE")
    return cands[0].read_text().strip()


def preflight(base, report):
    status, text, _ = http_request("GET", f"{base}/health", token=None)
    ok = status == 200 and "healthy" in text
    report(f"preflight /health: HTTP {status} {'OK' if ok else text[:120]}")
    return ok


def cmd_run(args):
    report = Reporter()
    base, ns_dir = args.base_url, args.ns_dir
    token = load_token(args.token_file)
    if not args.window_start or not args.window_end:
        print("usage: --window-start/--window-end required (ISO UTC)", file=sys.stderr)
        return 4
    run_started = utcnow()
    snap_ts = run_ts(run_started)
    out = {"cmd": "run", "run_ts": snap_ts,
           "window": [args.window_start, args.window_end],
           "dry_run": args.dry_run, "force_fail": args.force_fail}
    report(f"task-router-sync lane run {snap_ts} window="
           f"{args.window_start}..{args.window_end} dry_run={args.dry_run} "
           f"force_fail={args.force_fail}")

    if args.force_fail:
        # AC2 proof: simulate every write failing. ZERO HTTP writes happen —
        # the before/after marker timestamps must be identical.
        marker_before = newest_marker_timestamp(base, token)
        facts = json.loads(Path(args.facts_file).read_text()) if args.facts_file else []
        written, failures, skips = run_facts(
            base, NAMESPACE, token, facts, snap_ts, 0, report,
            fail_pattern=".")
        marker_after = newest_marker_timestamp(base, token)
        no_new_row = (marker_before == marker_after)
        out.update(outcome="FAILED", failures=failures, skips=skips,
                   marker_before=marker_before, marker_after=marker_after,
                   marker={"written": False,
                           "reason": "failed run writes no marker (TR-204 AC2)",
                           "no_new_row_asserted": no_new_row})
        report(f"MARKER: NOT WRITTEN — {len(failures)} failed write(s); "
               f"outcome=FAILED; newest-marker-before={marker_before} "
               f"after={marker_after} "
               f"({'UNCHANGED, no new row' if no_new_row else 'CHANGED — VIOLATION'})")
        if not no_new_row:
            return 1  # the no-marker invariant itself failed — loud, never silent
        write_report(out, args, snap_ts, report)
        return 1

    if not preflight(base, report):
        out.update(outcome="ABORT-PREFLIGHT")
        report("MARKER: NOT WRITTEN — preflight failed")
        write_report(out, args, snap_ts, report)
        return 2

    facts = []
    if args.facts_file:
        facts = json.loads(Path(args.facts_file).read_text())
        if not isinstance(facts, list):
            print(f"facts file must hold a JSON list: {args.facts_file}",
                  file=sys.stderr)
            return 4
    facts, size_skips = plan_fact_sizes(facts)
    for s in size_skips:
        report(f"SKIP (over body cap) {s['key']}: size={s['size']} "
               f"cap={s['cap']} — named, not written")

    marker_before = newest_marker_timestamp(base, token)

    # write-test (freshness floor) — skipped in dry-run: no writes at all
    wt_ts = snap_ts
    if not args.dry_run:
        wt_key = f"/sync/write-test-{args.window_end[:10]}"
        wt_status, wt_text, _ = post_memory(
            base, NAMESPACE, token, wt_key, "config",
            f"task-router-sync write-test {snap_ts}")
        if wt_status != 201:
            report(f"write-test FAIL {wt_key}: HTTP {wt_status} {wt_text[:200]}")
            out.update(outcome="ABORT-WRITE-TEST")
            report("MARKER: NOT WRITTEN — write-test failed")
            write_report(out, args, snap_ts, report)
            return 3
        wt_ok, _, wt_ts = verify_memory(base, NAMESPACE, token, wt_key, None,
                                        snap_ts)
        if not wt_ok:
            report("write-test FAIL: posted but read-back unproven")
            out.update(outcome="ABORT-WRITE-TEST")
            report("MARKER: NOT WRITTEN — write-test unproven")
            write_report(out, args, snap_ts, report)
            return 3
        report(f"write-test OK {wt_key} ts={wt_ts}")

    decls = ensure_declarations(ns_dir, report, dry_run=args.dry_run)

    # facts (already preflighted for size; verification floor = write-test ts)
    written, failures, skips = run_facts(
        base, NAMESPACE, token, facts, wt_ts, args.between_posts, report,
        dry_run=args.dry_run, fail_pattern=args.fail_pattern)
    skips = size_skips + skips

    # snapshot tables through the NDJSON mount
    table_results = []
    for table in SNAPSHOT_TABLES:
        table_results.append(snapshot_table(
            base, token, ns_dir, table, snap_ts, args.between_posts, report,
            dry_run=args.dry_run))
    table_failures = [r for r in table_results if not r.get("verified")]

    failures.extend({"key": f"table:{r['table']}", "status": 0,
                     "body": r.get("note", r.get("status"))}
                    for r in table_failures)

    outcome = ("FAILED" if failures
               else "PARTIAL" if (skips or not written and facts) else "SUCCESS")
    out.update(outcome=outcome, written=written, facts_planned=len(facts),
               failures=failures, skips=skips, tables=table_results,
               declarations_written=decls, marker_before=marker_before)

    if failures:
        report(f"MARKER: NOT WRITTEN — {len(failures)} failed write(s); "
               f"outcome=FAILED")
        out["marker"] = {"written": False,
                         "reason": f"{len(failures)} failed write(s)"}
        write_report(out, args, snap_ts, report)
        return 1

    content = build_marker_content(
        outcome, args.window_start, args.window_end, written + 1, len(facts),
        table_results, skips, extra="; ".join(filter(None, [args.note])))
    if not marker_content_is_substituted(content):
        bad = MARKER_PLACEHOLDER_RE.search(content).group(0)
        report(f"MARKER: REFUSED — unsubstituted template token {bad!r} in content")
        out["marker"] = {"written": False,
                         "reason": f"template token {bad!r} in marker content"}
        out["marker_content_refused"] = content
        write_report(out, args, snap_ts, report)
        return 1
    if args.dry_run:
        report(f"MARKER (dry-run, not written): {content}")
        out["marker"] = {"written": False, "reason": "dry-run",
                         "content": content}
        write_report(out, args, snap_ts, report)
        return 0
    marker = post_marker(base, NAMESPACE, token, content, snap_ts, report)
    out["marker"] = marker or {"written": False, "reason": "marker write failed"}
    out["marker_content"] = content
    write_report(out, args, snap_ts, report)
    if marker is None:
        return 1
    return 0


def write_report(out, args, snap_ts, report):
    if args.no_report:
        return
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    path = wd / f"run-{snap_ts.replace(':', '')}.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    report(f"REPORT: {path}")


def cmd_probe_overcap(args):
    """AC1 proof: force an over-cap POST /api/memories write and require the
    failure line to name the HTTP status + body. Verifies nothing landed."""
    report = Reporter()
    base, token = args.base_url, load_token(args.token_file)
    target = args.size  # serialized JSON body size to attempt (default: 102467, the TR-204 repro size)
    probe_key = f"/sync/task-router/overcap-probe-{run_ts()}"
    overhead = len(json.dumps({
        "key": probe_key, "domain": "raw_note", "content": "",
        "attributes": {"source_type": "sync_cron", "certainty": "confirmed"}},
        separators=(",", ":")).encode())
    pad = target - overhead
    if pad <= 0:
        print(f"target size {target} below overhead {overhead}", file=sys.stderr)
        return 4
    status, text, size = post_memory(
        base, NAMESPACE, token, probe_key, "raw_note", "x" * pad,
        {"source_type": "overcap-probe", "certainty": "confirmed"})
    report(f"OVERCAP-PROBE body_bytes={size} -> HTTP {status} body={text[:200]!r}")
    named = status >= 400 and text.strip() != ""
    if not named:
        report("OVERCAP-PROBE FAILED: rejection did not surface status+body")
        return 1
    ok, _, _ = verify_memory(base, NAMESPACE, token, probe_key, None,
                             "1970-01-01T00:00:00Z", limit=15)
    if ok:
        report("OVERCAP-PROBE FAILED: an over-cap probe row LANDED — unexpected")
        return 1
    report("OVERCAP-PROBE OK: failure named status+body; nothing landed")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["run", "probe-overcap"])
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--ns-dir", default=DEFAULT_NS_DIR)
    ap.add_argument("--workdir",
                    default=str(Path.home() / ".hermes" / "sync-workdirs"
                                / "task-router-sync"))
    ap.add_argument("--facts-file", default=None)
    ap.add_argument("--window-start", default=None)
    ap.add_argument("--window-end", default=None)
    ap.add_argument("--between-posts", type=float, default=2.0)
    ap.add_argument("--git-head", default=None, help="recorded verbatim in --note slot")
    ap.add_argument("--note", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-fail", action="store_true",
                    help="simulate every write failing; zero HTTP writes (AC2 proof)")
    ap.add_argument("--fail-pattern", default=None,
                    help="simulate failure only for fact keys matching this regex")
    ap.add_argument("--size", type=int, default=102467,
                    help="probe-overcap: serialized body size to attempt "
                         "(default 102467B = the TR-204 repro size)")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args(argv)
    if args.git_head and not args.note:
        args.note = f"HEAD={args.git_head}"
    if args.command == "probe-overcap":
        return cmd_probe_overcap(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
