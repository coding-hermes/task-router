#!/usr/bin/env python3
"""router_store_sync.py — the tanque de nafta: keep the outcome store and the
rolling averages current WITHOUT a cron that has to remember to run.

WHY THIS EXISTS (Bane, 2026-11-03)
The importer and the averages refresh were wired as hourly cron jobs. That was
the wrong owner: a router that learns its own costs should not depend on a
foreign scheduler firing on time. Measured cost of the wrong owner: the store
froze for 12.9h once and the averages were recomputed hourly over a frozen
input while nothing paged (TR-116).

CONTRACT
  * Any router entry point calls `sync_if_due()` on its way in (a resolve, a
    spawn, an outcome POST). If the last sync is older than the TTL (default
    30 min) it imports and recomputes; otherwise it returns immediately — one
    stat() call on a hot path.
  * If the host is busy or nothing calls it, an optional slow heartbeat can
    still tick it; nothing depends on that heartbeat being alive.
  * `import_now()` / `refresh_now()` force a step (the CLI verbs use these).
  * A single flock on data/state/.store-sync.lock makes concurrent callers
    cheap rather than duplicated: whoever loses the lock returns 'busy'.

Exit codes are the CLI's own; the library never raises for a store problem
(fail-open, like the rest of the router).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

STATE_DIR = os.path.join(REPO, "data", "state")
STAMP = os.path.join(STATE_DIR, "store-sync.json")
LOCK = os.path.join(STATE_DIR, ".store-sync.lock")
DEFAULT_TTL_S = int(os.environ.get("ROUTER_STORE_SYNC_TTL_S", "1800"))
#: At most one background sync launch per this many seconds, no matter how many
#: resolves or outcome POSTs arrive in the window. Cheap check, no lock needed.
LAUNCH_GAP_S = int(os.environ.get("ROUTER_STORE_SYNC_GAP_S", "60"))


def _read_stamp():
    try:
        with open(STAMP, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def age_s():
    """Seconds since the last COMPLETED sync (inf when never synced).

    Deliberately keyed on `last_sync_epoch`, which only a finished sync writes.
    A launch marker alone must not make the store look fresh, or asking for a
    sync would be the way to cancel it.
    """
    st = _read_stamp()
    last = st.get("last_sync_epoch")
    if not last:
        return float("inf")
    return max(0.0, time.time() - float(last))


def _write_stamp(mark_synced=True, **_kw):
    """Persist the stamp. `mark_synced=False` records a launch without claiming
    a completed sync."""
    st = _read_stamp()
    if mark_synced:
        st["last_sync_epoch"] = time.time()
        st["last_sync_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    else:
        st["last_launch_epoch"] = time.time()
        st["last_launch_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    st.update(_kw)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STAMP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, STAMP)
    except OSError:
        pass
    return st


def import_now(verbose=False):
    """Run every available driver import. Returns (ok, summary).

    Each driver is invoked through `router_outcomes.py <verb>` rather than as an
    in-process function call, because the CLI verb is the path that actually
    APPENDS to the store (the importers return rows; the verb writes them). Same
    contract the old shell importer used, without a shell.
    """
    import subprocess

    py = sys.executable or "python3"
    cli = os.path.join(REPO, "scripts", "router_outcomes.py")
    before = _store_rows()
    results = {}
    for verb in ("import-hermes", "import-pi", "import-opencode", "import-openclaw"):
        proc = subprocess.run([py, cli, verb], cwd=REPO, capture_output=True, text=True)
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        if proc.returncode == 0:
            results[verb] = tail[0][:120]
        else:
            blob = ((proc.stdout or "") + (proc.stderr or "")).lower()
            # a source that is not installed on this host is not a failure
            if "no such file" in blob or "not found" in blob or "does not exist" in blob:
                results[verb] = None
            else:
                results[verb] = "error: " + tail[0][:120]
    after = _store_rows()
    ok = not any(isinstance(v, str) and v.startswith("error:") for v in results.values())
    return ok, {
        "rows_before": before,
        "rows_after": after,
        "added": (after - before) if (before is not None and after is not None) else None,
        "drivers": results,
    }


def refresh_now():
    """Recompute the rolling averages from the store.

    outcomes_averages.main() writes its full JSON report to stdout (3 MB on a
    451k-row store), so its output is captured here rather than leaking into a
    resolve's stdout — that would corrupt `--format json`.
    """
    import contextlib
    import io

    import outcomes_averages as oa

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = oa.main([])
    return rc == 0, {"averages_bytes": len(buf.getvalue())}


def _store_rows():
    try:
        import router_outcomes as ro

        path = ro.outcomes_path()
        n = 0
        with open(path, "rb") as f:
            for _ in f:
                n += 1
        return n
    except OSError:
        return None


def _launch_marker_recent(min_gap_s):
    st = _read_stamp()
    last = st.get("last_launch_epoch")
    if not last:
        return False
    return (time.time() - float(last)) < min_gap_s


def sync_if_due(ttl_s=None, force=False, background=True, verbose=False):
    """Refresh the store if it is older than ttl_s — WITHOUT ever blocking the caller.

    Two separate hazards shaped this signature:

    1. A caller on a hot path (a resolve, an outcome POST) must not pay for a cold
       import. Measured: the first sync after the TTL expires took 70 s across the
       four drivers. On the resolve path that is a 70 s resolve, and under the test
       suite it parked an ingest handler long enough for the NEXT assertions in the
       same file to fail. So `background=True` (the default) launches the work in a
       detached child and returns immediately; the caller never waits.
    2. Retried callers must not stampede. The child takes a flock, and the parent
       records `last_launch_epoch` so a burst of resolves inside one minute spawns
       at most one child. A caller that finds the work fresh, throttled, or already
       locked out gets a plain dict back and carries on.

    `background=False` runs the work inline (CLI verbs and tests use it to get a
    real answer). Never raises: bookkeeping must not break a resolve.
    """
    ttl = DEFAULT_TTL_S if ttl_s is None else int(ttl_s)
    if not force and age_s() < ttl:
        return {"ran": False, "reason": "fresh", "age_s": round(age_s(), 1), "ttl_s": ttl}

    if background and not force:
        if _launch_marker_recent(LAUNCH_GAP_S):
            return {
                "ran": False,
                "reason": "launch-throttled",
                "gap_s": LAUNCH_GAP_S,
                "age_s": round(age_s(), 1),
            }
        _write_stamp(mark_synced=False)
        try:
            subprocess.Popen(
                [sys.executable or "python3", os.path.abspath(__file__), "sync-now", "--quiet"],
                cwd=REPO,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            return {"ran": False, "reason": "launched-background", "age_s": round(age_s(), 1)}
        except OSError as exc:
            # fall through to the inline path rather than leave the store stale
            if verbose:
                print(f"spawn failed ({exc}); syncing inline", file=sys.stderr)

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        lockf = open(LOCK, "a+")
    except OSError as exc:
        return {"ran": False, "reason": f"lock unavailable: {exc}"}
    try:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"ran": False, "reason": "busy (another process is syncing)"}
        if not force and age_s() < ttl:
            return {"ran": False, "reason": "fresh", "age_s": round(age_s(), 1), "ttl_s": ttl}
        ok_i, imp = import_now(verbose=verbose)
        ok_a, avg = refresh_now()
        detail = {"ran": True, "import_ok": ok_i, "averages_ok": ok_a, **imp}
        _write_stamp(
            ok=bool(ok_i and ok_a), detail={k: v for k, v in imp.items() if k != "drivers"}
        )
        if verbose:
            print(json.dumps(detail, indent=1, default=str))
        return detail
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Keep the task-router outcome store and rolling averages current"
    )
    ap.add_argument(
        "verb",
        choices=["sync", "sync-now", "import", "refresh", "status"],
        help="sync = refresh when due (backgrounded); sync-now = the same work inline, "
        "under the lock — what the detached child runs; import/refresh force one "
        "step; status = print the stamp",
    )
    ap.add_argument("--ttl-s", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.verb == "status":
        st = _read_stamp()
        print(json.dumps({**st, "age_s": round(age_s(), 1)}, indent=1, default=str))
        return 0
    if a.verb == "import":
        ok, imp = import_now(verbose=not a.quiet)
        _write_stamp(ok=ok, detail={"forced": "import"})
    elif a.verb == "refresh":
        ok, _ = refresh_now()
        _write_stamp(ok=ok, detail={"forced": "refresh"})
    elif a.verb == "sync-now":
        # the detached child's verb: do the work here, under the lock, and be
        # honest about the result. `force` skips the TTL re-check so a child
        # launched moments ago cannot decide it is not needed.
        res = sync_if_due(ttl_s=a.ttl_s, force=True, background=False, verbose=not a.quiet)
        ok = bool(res.get("ran")) and res.get("import_ok", True) and res.get("averages_ok", True)
        if not a.quiet:
            print(json.dumps(res, indent=1, default=str))
    else:
        res = sync_if_due(ttl_s=a.ttl_s, force=a.force, background=True, verbose=not a.quiet)
        # a backgrounded sync is a request, not a result: report success for the
        # request itself, and never fail a caller because the child is still busy
        ok = res.get("reason") in (
            "launched-background",
            "fresh",
            "launch-throttled",
            "busy (another process is syncing)",
        ) or bool(res.get("ran"))
        if not a.quiet:
            print(json.dumps(res, indent=1, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
