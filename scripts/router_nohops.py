#!/usr/bin/env python3
"""router_nohops.py — the no-hops-rate baseline, threshold and alert (TR-195).

The 2026-10 incident: the proxy's hourly resolve rate collapsed to zero hops
delivered and NOTHING alarmed, because no threshold existed. This module is
the single computation everyone reads: the UI's /api/ui/series and the alert
both call hourly_nohops_rate() — one function, so the two can never disagree.

WHAT A "RESOLVE" IS (and what is deliberately excluded)
The outcome store (data/state/outcomes.jsonl) carries every proxied request;
rows written by the driver importers (source Hermes/opencode/...) have
route_outcome=null: they never faced the hop ladder, so they are neither a
hop nor a resolve — they are counted as `non_resolves` and excluded WITH that
reason, never silently. A resolve is a row whose route_outcome names a
ladder decision:

    served    the ladder attempted and delivered
    failed    the ladder attempted and every attempt failed
    no-hops   the ladder had ZERO eligible hops to attempt (the incident shape)

`rejected` rows are admission sheds (overloaded / refusing callers) that
never reached a ladder; they are reported as `rejected_excluded` and are in
NEITHER side of the rate — a shed is a different failure class, and mixing it
into the denominator would dilute exactly the signal the alarm exists for.

THE RATE
    no-hops rate = |no-hops resolves| / |resolves|   over a sliding window,
    bucketed per hour — every bucket carries its own sample count `n`
    (a 2-row hour is not a trend), and the whole window carries one overall
    rate + the window's total sample size.

THE BASELINE + THRESHOLD
    data/nohops-baseline.json (in-repo, measured from the live store) records
    the rate under normal operation. The default alert threshold is
    baseline.rate + headroom (headroom default 0.05). ROUTER_NOHOPS_THRESHOLD
    overrides the whole ladder; ROUTER_NOHOPS_HEADROOM overrides the margin.
    A breach ALSO requires a minimum sample (ROUTER_NOHOPS_MIN_N, default 10):
    an alarm on n=3 would be noise, and a rate suppressed by a small sample is
    reported with `breach_reason: 'sample below min_n'` — a null with a
    reason, never a silent zero.

THE ALERT
    router_nohops.py check                 — real mode
    router_nohops.py check --alert-dry-run — prints the EXACT POST payload and
        hands it to a stub poster; no webhook is contacted, no live state is
        written. Forcing a breach in a dry run is done through the real knob
        (ROUTER_NOHOPS_THRESHOLD below the observed rate) — the threshold path
        that production runs is the path the dry run exercises.

TWO BREACH ARMS (TR-344)
    The rate arm alone could NOT see the 2026-10-20 10:20-10:54 incident: 190
    distinct scheduler lanes failed with 503 'no eligible model for this
    rating' in 34 minutes, and because the proxy also serves hundreds of
    normal resolves the RATE stayed under the baseline threshold while the
    fleet starved. Volume is the missing signal, so a second arm counts
    ABSOLUTE no-hop resolves inside a short window:

        burst no-hops >= ROUTER_NOHOPS_BURST_MIN (default 50)  within
        ROUTER_NOHOPS_BURST_WINDOW_H (default 1.0h, clamped to the window
        being evaluated)  =>  ALERT

    A burst is its own evidence (190 unservable ratings in 34 minutes is not a
    sample-size question), so the burst arm is not gated by MIN_N — burst_min
    IS its volume floor. The payload names which arm fired (`breach_kind`), and
    `check` prints one `verdict=ALERT|OK` line so an operator (or a cron) reads
    the verdict without parsing the header prose.

THE UNSERVABLE-RATING CENSUS (TR-344)
    Every unservable rating the RESOLVER dead-ends (router_spawn.resolve ->
    'no chain — no eligible model and no fallback lane could serve') is
    appended, once, to data/state/unservable-counters.jsonl (append-only, one
    row per rating: ts, matrix_hash, lane_class, required_categories). The
    counter answers 'which rating, on which lane family, how often' WITHOUT
    grepping logs — GET /api/ui/unservable (unservable_view) aggregates it into
    hourly buckets and a top-n. lane_class ∈ qa | releng | docs | readme |
    review | foreman | primary, derived from the lane/project name; 'primary'
    means "no satellite family named", and every row also carries
    lane_class_source so a default is never mistaken for a declaration.
    Fail-open: an unreadable/unwritable counter file never breaks a resolve.

    Delivery (real mode): ALWAYS appends one JSONL row to the events log
    (data/state/router-events.jsonl, ROUTER_NOHOPS_EVENTS_FILE overrides);
    POSTs the same payload to the deliver-thread webhook ONLY when
    ROUTER_NOHOPS_WEBHOOK_URL is configured. No webhook configured is a
    documented, visible state — the events log is the delivery, and the
    payload names which channel(s) took it.

    The payload names the TOP BLOCKING CATEGORY (TR-185 tie-in): the category
    most often required by the window's no-hops rows, computed from the SAME
    ledger rows the rate came from — never hardcoded.

Exit codes (mirrors router_outcomes_freshness / router_stale_watchdog):
    0 healthy, 1 breach (the actionable signal), 2 the inputs themselves
    could not be read. --alert-dry-run keeps the honest verdict code.

Stdlib only, like every runtime tool in this repo.
"""
from __future__ import annotations
import argparse
import collections
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sibling tools

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: One store resolution for the whole repo: the same path /api/ui/series reads.
import router_outcomes  # noqa: E402
from typing import Any

BASELINE_PATH = os.environ.get(
    "ROUTER_NOHOPS_BASELINE",
    os.path.join(REPO, "data", "nohops-baseline.json"))
EVENTS_PATH = os.environ.get(
    "ROUTER_NOHOPS_EVENTS_FILE",
    os.path.join(REPO, "data", "state", "router-events.jsonl"))
WEBHOOK_URL = os.environ.get("ROUTER_NOHOPS_WEBHOOK_URL", "")

#: Ladder decisions only (see module docstring for the rejected exclusion).
RESOLVE_OUTCOMES = ("served", "failed", "no-hops")
NO_HOPS = "no-hops"

DEFAULT_HEADROOM = 0.05
DEFAULT_MIN_N = 10
#: Only when there is no baseline file AND no env override — loud in the payload.
DEFAULT_THRESHOLD = 0.10

#: TR-344: the absolute-volume arm. 50 no-hop resolves inside an hour is a
#: fleet-wide starvation event (2026-10-20: 190 in 34 minutes), not noise.
DEFAULT_BURST_MIN = 50
DEFAULT_BURST_WINDOW_H = 1.0

#: TR-344: the per-unservable-rating census (append-only JSONL).
_DEFAULT_UNSERVABLE = os.path.join(REPO, "data", "state",
                                   "unservable-counters.jsonl")

#: The rated lane families (a satellite lane of a fleet project). 'primary' is
#: the catch-all for a resolve that named no satellite family.
LANE_CLASSES = ("qa", "releng", "docs", "readme", "review", "foreman",
                "primary")
SATELLITE_CLASSES = LANE_CLASSES[:-1]


def unservable_path():
    """The counter store (ROUTER_UNSERVABLE_FILE wins — hermetic tests and a
    per-user data home both need it). Resolved at CALL time, like the outcome
    store, so a long-lived server follows an env change."""
    return os.environ.get("ROUTER_UNSERVABLE_FILE") or _DEFAULT_UNSERVABLE


def _one(v: Any):
    """First element of a list-valued query param, else the value itself."""
    if isinstance(v, list):
        v = v[0] if v else None
    return v


def _epoch(ts: Any) -> float | None:
    """Rows carry epoch seconds (float); anything else is untimestampable."""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return float(ts)


def is_resolve(row: Any) -> bool:
    return row.get("route_outcome") in RESOLVE_OUTCOMES


def is_nohop(row: Any) -> bool:
    return row.get("route_outcome") == NO_HOPS


def top_blocking_category(rows: Any) -> tuple:
    """TR-185 tie-in: the category most often required by no-hops rows.

    The demand side, straight off the same rows the rate came from: count the
    required_categories keys across the window's no-hops resolves. Returns
    (category, count) or (None, reason) when nothing can be named — a null
    always carries its reason.
    """
    counts = collections.Counter()
    n_nohop = 0
    for r in rows:
        if not is_nohop(r):
            continue
        n_nohop += 1
        req = r.get("required_categories")
        if isinstance(req, dict):
            counts.update(k for k in req if isinstance(k, str))
    if not n_nohop:
        return None, "no no-hops rows in the window"
    if not counts:
        return None, ("no-hops rows carry no required_categories rating "
                      "evidence")
    cat, n = counts.most_common(1)[0]
    return cat, n


def hourly_nohops_rate(rows: Any, window_h: float=24.0, now_s: Any=None) -> dict:
    """THE one computation. `rows` = parsed store dicts (already a window from
    a caller that scanned the store; every row outside it is unknowable here
    and the response says how many rows it was given).

    Returns the overall rate with its sample size, per-hour buckets each with
    their own n, and the exclusion census with reasons.
    """
    try:
        window_h = max(0.1, float(window_h))
    except (TypeError, ValueError):
        window_h = 24.0
    now = float(now_s) if now_s is not None else time.time()
    cutoff = now - window_h * 3600.0

    scanned = len(rows)
    non_resolves = 0
    untimestamped = 0
    rejected = 0
    resolve_rows = []
    for r in rows:
        if not is_resolve(r):
            if r.get("route_outcome") == "rejected":
                rejected += 1
            else:
                non_resolves += 1
            continue
        if _epoch(r.get("ts")) is None:
            untimestamped += 1
            continue
        resolve_rows.append(r)

    width = 3600.0
    first = int(cutoff // width) * width
    buckets = {}
    nohops_n = 0
    for r in resolve_rows:
        ts = _epoch(r.get("ts"))
        if ts < cutoff:
            continue  # the caller's window is a courtesy; this one is exact
        b = int(ts // width) * width
        g = buckets.setdefault(b, {"resolves": 0, "nohops": 0})
        g["resolves"] += 1
        if is_nohop(r):
            g["nohops"] += 1
            nohops_n += 1

    hours = []
    b = first
    last = int(now // width) * width
    while b <= last:
        g = buckets.get(b)
        if not g:
            hours.append({"start_ts": b,
                          "iso": datetime.datetime.fromtimestamp(
                              b, datetime.timezone.utc).strftime(
                              "%Y-%m-%dT%H:%M:%SZ"),
                          "resolves": 0, "nohops": 0, "n": 0,
                          "rate": None, "rate_reason": "no resolves in this "
                          "hour"})
        else:
            hours.append({"start_ts": b,
                          "iso": datetime.datetime.fromtimestamp(
                              b, datetime.timezone.utc).strftime(
                              "%Y-%m-%dT%H:%M:%SZ"),
                          "resolves": g["resolves"], "nohops": g["nohops"],
                          "n": g["resolves"],
                          "rate": (round(g["nohops"] / g["resolves"], 6)
                                   if g["resolves"] else None)})
        b += int(width)

    total = sum(h["resolves"] for h in hours)
    total_nohops = sum(h["nohops"] for h in hours)
    cat, cat_n = top_blocking_category(
        [r for r in resolve_rows if _epoch(r.get("ts")) >= cutoff])
    return {
        "rate": (round(total_nohops / total, 6) if total else None),
        "rate_reason": (None if total else "no resolves in this window"),
        "nohops": total_nohops,
        "resolves": total,
        "n": total,
        "window_h": window_h,
        "window_start_ts": cutoff,
        "window_end_ts": now,
        "hours": hours,
        "hour_count": len(hours),
        "top_blocking_category": cat,
        "top_blocking_category_count": cat_n if cat else None,
        "top_blocking_category_reason": (None if cat else cat_n),
        # the exclusion census — every number the rate did NOT use says why
        "rows_scanned": scanned,
        "non_resolves_excluded": non_resolves,
        "rejected_excluded": rejected,
        "untimestamped_excluded": untimestamped,
    }


def load_store_rows(store_path: Any, scan_limit: int=200000) -> tuple:
    """Tolerant TAIL-scan of the outcome store (same file + same newest-N
    semantics the UI series uses — a store bigger than the scan limit must
    yield its NEWEST rows, never its oldest).

    Malformed lines are skipped and counted, never raised. Returns
    (rows, {"rows_scanned": N, "parse_failed": M, "store": path}) where
    rows_scanned is every non-empty line seen and parse_failed counts the
    malformed ones WITHIN the kept window.
    """
    import collections
    tail = collections.deque(maxlen=max(1, scan_limit))
    scanned = 0
    try:
        with open(store_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                scanned += 1
                tail.append(line)
    except OSError as e:
        return [], {"rows_scanned": 0, "parse_failed": 0, "store": store_path,
                    "error": f"store unreadable: {e}"}
    rows = []
    parse_failed = 0
    for line in tail:
        try:
            d = json.loads(line)
        except ValueError:
            parse_failed += 1
            continue
        if isinstance(d, dict):
            rows.append(d)
    return rows, {"rows_scanned": scanned, "parse_failed": parse_failed,
                  "store": store_path}


def load_baseline(path: Any=None) -> tuple:
    """The in-repo baseline. Returns (dict, source-note); missing file is a
    reported state, not an exception."""
    path = path or BASELINE_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            b = json.load(fh)
        if isinstance(b, dict) and isinstance(b.get("rate"), (int, float)):
            return b, f"baseline file {path}"
        return None, f"baseline file {path} has no numeric rate"
    except FileNotFoundError:
        return None, f"no baseline file at {path}"
    except Exception as e:  # noqa: BLE001 — a torn baseline is a reported state
        return None, f"baseline file {path} unreadable: {e}"


def resolve_threshold(baseline: Any=None) -> tuple:
    """Env override > baseline.threshold > baseline.rate + headroom > default.

    Returns (threshold, source) — the source rides in every payload so a
    threshold can never look more principled than it is.
    """
    env = os.environ.get("ROUTER_NOHOPS_THRESHOLD")
    if env:
        try:
            return max(0.0, float(env)), "env ROUTER_NOHOPS_THRESHOLD"
        except ValueError:
            pass  # a malformed override falls through, loudly, to the ladder
    if isinstance(baseline, dict):
        t = baseline.get("threshold")
        if isinstance(t, (int, float)):
            return float(t), "baseline file threshold"
        rate = baseline.get("rate")
        if isinstance(rate, (int, float)):
            headroom = DEFAULT_HEADROOM
            env_h = os.environ.get("ROUTER_NOHOPS_HEADROOM")
            if env_h:
                try:
                    headroom = max(0.0, float(env_h))
                except ValueError:
                    pass
            return float(rate) + headroom, "baseline rate + headroom"
    return DEFAULT_THRESHOLD, "default (no baseline, no override)"


def min_n():
    env = os.environ.get("ROUTER_NOHOPS_MIN_N")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return DEFAULT_MIN_N


# ---------------------------------------------------------------------------
# TR-344: lane class + the per-unservable-rating census
# ---------------------------------------------------------------------------

def lane_class(name: Any=None) -> str:
    """The rated lane family a lane/project NAME belongs to (TR-344).

    Fleet satellite lanes are named `<project>-<family>` (`lore-qa`,
    `task-router-releng`, `…-releng-local`), so the RIGHTMOST token that names
    a known family wins: a trailing qualifier (`-local`, `-2`) does not hide
    the family it qualifies. Nothing recognised -> 'primary' (the documented
    catch-all: "no satellite family named"), never a guess — callers that must
    distinguish a declaration from a default use lane_class_source().
    """
    if not isinstance(name, str) or not name.strip():
        return "primary"
    tokens = [t for t in name.strip().lower().replace('_', '-')
              .replace('.', '-').replace('/', '-').replace(':', '-').split('-')
              if t]
    for tok in reversed(tokens):
        if tok in SATELLITE_CLASSES:
            return tok
    return "primary"


def lane_class_source(name: Any=None, declared: Any=None) -> str:
    """Why THIS lane_class is what it is: declared | derived-from-name |
    default-no-lane-hint. A default is never silently presented as a fact."""
    if isinstance(declared, str) and declared.strip():
        return "declared"
    if isinstance(name, str) and name.strip() \
            and lane_class(name) != "primary":
        return "derived-from-name"
    return "default-no-lane-hint"


def matrix_hash(matrix: Any) -> str:
    """Stable fingerprint of a rating's required_categories (TR-344).

    Canonical form = sorted (category, level) pairs, JSON-encoded: two
    spellings of the same rating (dict order, tuple list) hash identically, and
    the counter and GET /api/ui/unservable agree by construction. 16 hex chars
    is enough to key a counter; the full matrix rides on the row.
    """
    pairs = []
    if isinstance(matrix, dict):
        pairs = [(str(k), matrix[k]) for k in matrix]
    elif isinstance(matrix, (list, tuple)):
        for item in matrix:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]), item[1]))
            elif isinstance(item, dict):
                pairs.extend((str(k), item[k]) for k in item)
    if not pairs:
        return "empty"
    canon = json.dumps(sorted(pairs), separators=(',', ':'), sort_keys=False)
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()[:16]


def record_unservable(matrix: Any=None, lane: Any=None, project: Any=None,
                      profile_id: Any=None, source: Any=None,
                      reason: Any=None, path: Any=None, ts: Any=None) -> Any:
    """Append ONE row for an unservable rating (TR-344). Returns the row, or
    None when the write failed — NEVER raises: fail-open is the resolver's
    contract and a broken counter must not break a 503.

    `lane` is the lane identity as the caller declared it (a scheduler lane
    name, an x-router-lane header, or the resolver's project slot). `project`
    is recorded verbatim as provenance; lane_class is derived from the lane
    first, then the project.
    """
    try:
        name = lane if isinstance(lane, str) and lane.strip() else project
        lclass = lane_class(name)
        row = {
            # one row per rating, at the moment the resolver dead-ended
            'ts': float(ts) if ts is not None else time.time(),
            'matrix_hash': matrix_hash(matrix),
            'lane_class': lclass,
            'lane_class_source': lane_class_source(name, lane),
            'required_categories': (dict(matrix) if isinstance(matrix, dict)
                                    else matrix),
            'lane': (name if isinstance(name, str) and name.strip() else None),
            'project': project if isinstance(project, str) else None,
            'profile_id': profile_id if isinstance(profile_id, str) else None,
            'source': source if isinstance(source, str) else None,
            'reason': reason,
            'task': 'TR-344',
        }
        target = path or unservable_path()
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(target, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
    except Exception:  # noqa: BLE001 — the census never breaks a resolve
        return None


def load_unservable_rows(path: Any=None, scan_limit: int=200000) -> tuple:
    """Tolerant TAIL-scan of the counter store (same newest-N semantics and
    the same malformed-line accounting as the outcome store reader)."""
    return load_store_rows(path or unservable_path(), scan_limit=scan_limit)


def unservable_view(window_h: Any=24.0, top_n: Any=10, now_s: Any=None,
                    path: Any=None, scan_limit: Any=200000,
                    bucket_h: Any=1.0) -> dict:
    """GET /api/ui/unservable: the unservable-rating census over a window.

    Shape: {window_h, buckets: [{ts, iso, matrix_hash, lane_class, count}],
    top_n, top: [{matrix_hash, lane_class, count}], …}. Every count is a row
    count from the append-only file — never a log grep. An unreadable store is
    a REPORTED state (error + empty buckets), never an exception.
    """
    try:
        window_h = max(0.1, float(window_h))
    except (TypeError, ValueError):
        window_h = 24.0
    try:
        top_n = max(1, int(top_n))
    except (TypeError, ValueError):
        top_n = 10
    try:
        bucket_h = max(1.0 / 60.0, float(bucket_h))
    except (TypeError, ValueError):
        bucket_h = 1.0
    now = float(now_s) if now_s is not None else time.time()
    cutoff = now - window_h * 3600.0
    try:
        scan_limit = max(1, int(scan_limit))
    except (TypeError, ValueError):
        scan_limit = 200000
    rows, meta = load_unservable_rows(path, scan_limit=scan_limit)
    width = bucket_h * 3600.0
    buckets = {}
    per_key = collections.Counter()
    total = 0
    untimestamped = 0
    for r in rows:
        ts = _epoch(r.get('ts'))
        if ts is None:
            untimestamped += 1
            continue
        if ts < cutoff:
            continue
        total += 1
        mhash = r.get('matrix_hash') or matrix_hash(r.get('required_categories'))
        lclass = r.get('lane_class') or lane_class(r.get('lane'))
        b = int(ts // width) * width
        key = (b, mhash, lclass)
        buckets[key] = buckets.get(key, 0) + 1
        per_key[(mhash, lclass)] += 1
    out_buckets = [
        {'ts': b,
         'iso': datetime.datetime.fromtimestamp(
             b, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
         'matrix_hash': mh, 'lane_class': lc, 'count': n}
        for (b, mh, lc), n in sorted(buckets.items())
    ]
    top = [{'matrix_hash': mh, 'lane_class': lc, 'count': n}
           for (mh, lc), n in per_key.most_common(top_n)]
    by_class = collections.Counter()
    for (_mh, lc), n in per_key.items():
        by_class[lc] += n
    return {
        'window_h': window_h,
        'buckets': out_buckets,
        'bucket_h': bucket_h,
        'top_n': top_n,
        'top': top,
        'total': total,
        'lane_classes': list(LANE_CLASSES),
        'by_lane_class': dict(sorted(by_class.items())),
        'distinct_matrices': len({mh for (mh, _lc) in per_key}),
        'rows_scanned': meta.get('rows_scanned', 0),
        'parse_failed': meta.get('parse_failed', 0),
        'untimestamped_excluded': untimestamped,
        'scan_limit': scan_limit,
        'store': meta.get('store'),
        'error': meta.get('error'),
    }


# ---------------------------------------------------------------------------
# TR-344: the absolute-volume (burst) arm
# ---------------------------------------------------------------------------

def burst_min():
    env = os.environ.get('ROUTER_NOHOPS_BURST_MIN')
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return DEFAULT_BURST_MIN


def burst_window_h():
    env = os.environ.get('ROUTER_NOHOPS_BURST_WINDOW_H')
    if env:
        try:
            return max(1.0 / 60.0, float(env))
        except ValueError:
            pass
    return DEFAULT_BURST_WINDOW_H


def nohops_burst(rows: Any, now_s: Any=None, window_h: Any=None) -> dict:
    """Count no-hop resolves inside the BURST window (TR-344).

    The window is the burst window, clamped to the window the caller is
    evaluating (asking for 24h must not count an hour's burst over a day, and
    asking for 30m must not reach outside it). Rows without a usable ts cannot
    be counted and are reported, never silently dropped.
    """
    now = float(now_s) if now_s is not None else time.time()
    w = burst_window_h()
    if window_h is not None:
        try:
            w = min(w, max(1.0 / 60.0, float(window_h)))
        except (TypeError, ValueError):
            pass
    cutoff = now - w * 3600.0
    n = 0
    untimestamped = 0
    for r in rows:
        if not is_nohop(r):
            continue
        ts = _epoch(r.get('ts'))
        if ts is None:
            untimestamped += 1
            continue
        if ts >= cutoff:
            n += 1
    floor = burst_min()
    return {'window_h': w, 'nohops': n, 'min': floor,
            'breached': n >= floor,
            'untimestamped_excluded': untimestamped,
            'reason': (f'burst {n} no-hops in {w:g}h '
                       f'{">=" if n >= floor else "<"} '
                       f'ROUTER_NOHOPS_BURST_MIN={floor}')}



def evaluate(rows: Any, window_h: float=24.0, now_s: Any=None, baseline: Any=None) -> dict:
    """rate + threshold -> the verdict and the exact alert payload.

    TR-344: TWO arms. The rate arm (baseline + headroom over the window) and
    the absolute-volume arm (burst_min no-hop resolves inside burst_window_h).
    ALERT when EITHER arm breaches; the payload names which one(s) with
    `breach_kind`, and both arms' numbers ride along so a reader never has to
    infer why the verdict is what it is.
    """
    series = hourly_nohops_rate(rows, window_h=window_h, now_s=now_s)
    baseline_doc, baseline_source = (baseline, "given") if baseline is not None \
        else load_baseline()
    threshold, threshold_source = resolve_threshold(baseline_doc)
    n = series["n"]
    rate = series["rate"]
    burst = nohops_burst(rows, now_s=now_s, window_h=series["window_h"])
    rate_breached = None
    rate_reason = None
    if rate is None:
        rate_breached = False
        rate_reason = series["rate_reason"] or "no rate measurable"
    elif n < min_n():
        rate_breached = False
        rate_reason = (f"sample {n} below ROUTER_NOHOPS_MIN_N={min_n()}")
    else:
        rate_breached = rate > threshold
        if not rate_breached:
            rate_reason = f"rate {rate} <= threshold {threshold}"
    alert = bool(rate_breached or burst["breached"])
    rate_bit = (f"rate {rate} > threshold {threshold}" if rate_breached
                else rate_reason)
    if rate_breached and burst["breached"]:
        breach_kind = "rate+burst"
        breach_reason = f"{rate_bit}; {burst['reason']}"
    elif rate_breached:
        breach_kind = "rate"
        breach_reason = rate_bit
    elif burst["breached"]:
        breach_kind = "burst"
        breach_reason = f"{burst['reason']} ({rate_bit})"
    else:
        breach_kind = None
        breach_reason = f"{rate_bit}; {burst['reason']}"
    payload = {
        "kind": "nohops-alert",
        "alert": alert,
        "breach_kind": breach_kind,
        "breach_reason": breach_reason,
        "rate": rate,
        "rate_breached": rate_breached,
        "threshold": threshold,
        "threshold_source": threshold_source,
        "min_n": min_n(),
        "burst": burst,
        "nohops": series["nohops"],
        "resolves": series["resolves"],
        "n": n,
        "window_h": series["window_h"],
        "top_blocking_category": series["top_blocking_category"],
        "top_blocking_category_count": series["top_blocking_category_count"],
        "text": None,  # filled by alert_text() when delivered
    }
    return {"series": series, "payload": payload, "baseline_source":
            baseline_source}


def alert_text(payload: Any) -> str:
    """The human line a deliver thread sees."""
    cat = payload.get("top_blocking_category")
    cat_bit = (f"; top blocking category (TR-185): {cat}"
               f" ({payload.get('top_blocking_category_count')} no-hop"
               f" resolves require it)") if cat else ""
    burst = payload.get("burst") or {}
    burst_bit = ""
    if payload.get("breach_kind") in ("burst", "rate+burst"):
        # name the arm that caught it — a volume alarm must read as one
        burst_bit = (f"; BURST: {burst.get('nohops')} no-hop resolves in "
                     f"{burst.get('window_h')}h (floor "
                     f"{burst.get('min')}) — routing starvation, not a sample")
    return (f"NO-HOPS ALERT: rate {payload['rate']} over the last "
            f"{payload['window_h']}h crossed threshold "
            f"{payload['threshold']} ({payload['nohops']}/"
            f"{payload['resolves']} resolves with zero eligible hops"
            f"{cat_bit}{burst_bit})")


def _append_event(path: Any, row: Any) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _post_webhook(url: Any, payload: Any, timeout: int=8):
    """Real POST. Only ever called with a configured URL in real mode."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "task-router-nohops/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def stub_post(url: Any, payload: Any) -> int:
    """The dry-run poster: no socket, same signature as _post_webhook."""
    target = url or "(no ROUTER_NOHOPS_WEBHOOK_URL configured)"
    print(f"[dry-run] STUB POST {target}")
    print("[dry-run] POST payload:")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 200


def deliver(result: Any, dry_run: bool=False, now_s: Any=None, post_fn: Any=None, events_path: Any=None) -> dict:
    """Real: events log always + webhook when configured. Dry-run: stub only.

    Returns the delivery record appended to the log / printed.
    """
    payload = dict(result["payload"])
    payload["text"] = alert_text(payload)
    post_fn = post_fn or (_post_webhook if not dry_run else stub_post)
    events_path = events_path or EVENTS_PATH
    delivered = []
    if payload["alert"]:
        if dry_run:
            post_fn(WEBHOOK_URL, payload)
            delivered.append("stub-post (dry-run)")
        else:
            if WEBHOOK_URL:
                status = post_fn(WEBHOOK_URL, payload)
                delivered.append(f"webhook {WEBHOOK_URL} -> HTTP {status}")
            delivered.append(f"events log {events_path}")
            _append_event(events_path, dict(payload, delivered=delivered,
                                            dry_run=False))
    else:
        print(f"no alert: {payload['breach_reason']}")
    if dry_run and not payload["alert"]:
        print("[dry-run] would NOT alert "
              f"({payload['breach_reason']}); no POST, no events row")
    return {"payload": payload, "delivered": delivered or
            ["nothing (no breach)"]}


def cmd_check(args: Any) -> int:
    store = router_outcomes.outcomes_path()
    rows, meta = load_store_rows(store, scan_limit=args.scan_limit)
    if meta.get("error"):
        print(f"router_nohops: {meta['error']}", file=sys.stderr)
        return 2
    result = evaluate(rows, window_h=args.window_h)
    p = result["payload"]
    verdict = "ALERT" if p["alert"] else "OK"
    if args.alert_dry_run:
        rec = deliver(result, dry_run=True)
        print(f"[dry-run] verdict={verdict} kind={p.get('breach_kind') or '-'} "
              f"({p['breach_reason']})")
        print(f"[dry-run] store: {meta['store']} rows_scanned="
              f"{meta['rows_scanned']} parse_failed={meta['parse_failed']}")
        print(f"[dry-run] delivered to: {'; '.join(rec['delivered'])}")
        return 1 if p["alert"] else 0
    rec = deliver(result, dry_run=False)
    s = result["series"]
    # TR-344: one machine-readable verdict line — an operator (or a cron) reads
    # the verdict without parsing prose, and the arm that fired is named.
    print(f"verdict={verdict} kind={p.get('breach_kind') or '-'}")
    print(f"no-hops rate {p['rate']} over {p['window_h']}h "
          f"(n={p['n']}, threshold {p['threshold']} via "
          f"{p['threshold_source']}) breach_reason={p['breach_reason']}")
    print(f"burst: {p['burst']['nohops']} no-hops in "
          f"{p['burst']['window_h']}h (floor {p['burst']['min']}, "
          f"{'BREACHED' if p['burst']['breached'] else 'below floor'})")
    print(f"window scanned {meta['rows_scanned']} store rows "
          f"({meta['parse_failed']} malformed skipped); "
          f"{s['non_resolves_excluded']} non-resolves, "
          f"{s['rejected_excluded']} rejected, "
          f"{s['untimestamped_excluded']} untimestamped excluded")
    if rec["delivered"] != ["nothing (no breach)"]:
        print(f"delivered: {'; '.join(rec['delivered'])}")
        return 1
    return 0


def cmd_status(args: Any) -> int:
    store = router_outcomes.outcomes_path()
    rows, meta = load_store_rows(store, scan_limit=args.scan_limit)
    if meta.get("error"):
        print(f"router_nohops: {meta['error']}", file=sys.stderr)
        return 2
    _ev = evaluate(rows, window_h=args.window_h)
    s = _ev["series"]
    ev = _ev["payload"]
    out = dict(s, store=meta["store"],
               store_rows_scanned=meta["rows_scanned"],
               store_parse_failed=meta["parse_failed"],
               burst=ev["burst"], breach_kind=ev["breach_kind"],
               breached=ev["alert"])
    out["threshold"], out["threshold_source"] = resolve_threshold(
        load_baseline()[0])
    if args.json:
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    print(f"store       : {out['store']} "
          f"({out['store_rows_scanned']} rows scanned)")
    print(f"window      : {s['window_h']}h -> rate {s['rate']} "
          f"(no-hops {s['nohops']}/{s['resolves']}, n={s['n']})")
    print(f"threshold   : {out['threshold']} ({out['threshold_source']})")
    b = ev["burst"]
    print(f"burst       : {b['nohops']} no-hops in {b['window_h']}h "
          f"(floor {b['min']}, "
          f"{'BREACHED' if b['breached'] else 'below floor'})")
    print(f"verdict     : {'ALERT' if ev['alert'] else 'OK'} "
          f"(kind={ev['breach_kind'] or '-'})")
    cat = s["top_blocking_category"]
    if cat:
        print(f"blocking    : {cat} ({s['top_blocking_category_count']} "
              f"no-hop resolves)")
    else:
        print(f"blocking    : none named "
              f"({s['top_blocking_category_reason']})")
    print("per hour (rate, n):")
    for h in s["hours"]:
        mark = " *" if (h["rate"] or 0) > out["threshold"] and h["n"] else ""
        print(f"  {h['iso']}  {str(h['rate']):>8}  n={h['n']:<5}{mark}")
    return 0


def cmd_baseline(args: Any) -> int:
    """Measure the live store and print (or --write, record) the baseline."""
    store = router_outcomes.outcomes_path()
    rows, meta = load_store_rows(store, scan_limit=args.scan_limit)
    if meta.get("error"):
        print(f"router_nohops: {meta['error']}", file=sys.stderr)
        return 2
    s = evaluate(rows, window_h=args.window_h)["series"]
    if s["rate"] is None:
        print(f"router_nohops: cannot baseline an empty window "
              f"({s['rate_reason']})", file=sys.stderr)
        return 2
    threshold, tsrc = resolve_threshold({"rate": s["rate"]})
    doc = {
        "task": "TR-195",
        "kind": "nohops-baseline",
        "computed": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "store": store,
        "store_rows_scanned": meta["rows_scanned"],
        "window_h": s["window_h"],
        "rate": s["rate"],
        "nohops": s["nohops"],
        "resolves": s["resolves"],
        "headroom": DEFAULT_HEADROOM,
        "threshold": threshold,
        "threshold_note": f"rate + headroom ({tsrc})",
        "excluded": {"non_resolves": s["non_resolves_excluded"],
                     "rejected": s["rejected_excluded"],
                     "untimestamped": s["untimestamped_excluded"]},
        "how_to_recompute": ("ROUTING_OUTCOMES_FILE=<live store> "
                             "python3 scripts/router_nohops.py baseline "
                             f"--window-h {args.window_h} --write"),
    }
    if args.write:
        path = args.write if isinstance(args.write, str) else BASELINE_PATH
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"baseline written: {path}")
    print(json.dumps(doc, indent=1, sort_keys=True))
    return 0


def cmd_unservable(args: Any) -> int:
    """TR-344: the unservable-rating census, the same document the GET route
    serves (one function — the CLI and /api/ui/unservable cannot disagree)."""
    view = unservable_view(window_h=args.window_h, top_n=args.top_n,
                           scan_limit=args.scan_limit)
    if view.get("error"):
        print(f"router_nohops: {view['error']}", file=sys.stderr)
    if args.json:
        print(json.dumps(view, indent=1, sort_keys=True))
        return 0
    print(f"store   : {view['store']} ({view['rows_scanned']} rows scanned)")
    print(f"window  : {view['window_h']}h -> {view['total']} unservable "
          f"ratings, {view['distinct_matrices']} distinct matrices")
    print(f"classes : {view['by_lane_class'] or '{}'}")
    print("top:")
    for t in view["top"]:
        print(f"  {t['count']:>5}  {t['lane_class']:<8} {t['matrix_hash']}")
    print("buckets (hour, matrix, class, count):")
    for b in view["buckets"]:
        print(f"  {b['iso']}  {b['matrix_hash']}  {b['lane_class']:<8} "
              f"{b['count']}")
    return 0


def main(argv: Any=None) -> int:
    ap = argparse.ArgumentParser(
        description="TR-195/TR-344 no-hops-rate baseline, threshold, alert "
                    "and the unservable-rating census")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: Any) -> None:
        p.add_argument("--window-h", type=float, default=24.0)
        p.add_argument("--scan-limit", type=int, default=200000)

    ap_status = sub.add_parser("status", help="rate + hourly buckets + threshold")
    common(ap_status)
    ap_status.add_argument("--json", action="store_true")

    ap_check = sub.add_parser("check", help="evaluate + deliver the alert")
    common(ap_check)
    ap_check.add_argument("--alert-dry-run", action="store_true",
                          help="print the exact POST payload; stub poster; "
                               "no webhook, no live state")

    ap_uns = sub.add_parser("unservable",
                            help="the per-unservable-rating census "
                                 "(TR-344; same document as /api/ui/unservable)")
    common(ap_uns)
    ap_uns.add_argument("--top-n", type=int, default=10)
    ap_uns.add_argument("--json", action="store_true")

    ap_base = sub.add_parser("baseline", help="measure the live store")
    common(ap_base)
    ap_base.add_argument("--write", nargs="?", const=True, default=None,
                         metavar="PATH",
                         help="record the baseline (default data/"
                              "nohops-baseline.json)")

    args = ap.parse_args(argv)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "unservable":
        return cmd_unservable(args)
    return cmd_baseline(args)


if __name__ == "__main__":
    raise SystemExit(main())
