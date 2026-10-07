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
import argparse
import collections
import datetime
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


def _one(v):
    """First element of a list-valued query param, else the value itself."""
    if isinstance(v, list):
        v = v[0] if v else None
    return v


def _epoch(ts):
    """Rows carry epoch seconds (float); anything else is untimestampable."""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return float(ts)


def is_resolve(row):
    return row.get("route_outcome") in RESOLVE_OUTCOMES


def is_nohop(row):
    return row.get("route_outcome") == NO_HOPS


def top_blocking_category(rows):
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


def hourly_nohops_rate(rows, window_h=24.0, now_s=None):
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


def load_store_rows(store_path, scan_limit=200000):
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


def load_baseline(path=None):
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


def resolve_threshold(baseline=None):
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


def evaluate(rows, window_h=24.0, now_s=None, baseline=None):
    """rate + threshold -> the verdict and the exact alert payload."""
    series = hourly_nohops_rate(rows, window_h=window_h, now_s=now_s)
    baseline_doc, baseline_source = (baseline, "given") if baseline is not None \
        else load_baseline()
    threshold, threshold_source = resolve_threshold(baseline_doc)
    n = series["n"]
    rate = series["rate"]
    breached = None
    breach_reason = None
    if rate is None:
        breached = False
        breach_reason = series["rate_reason"] or "no rate measurable"
    elif n < min_n():
        breached = False
        breach_reason = (f"sample {n} below ROUTER_NOHOPS_MIN_N={min_n()}")
    else:
        breached = rate > threshold
        if not breached:
            breach_reason = f"rate {rate} <= threshold {threshold}"
    payload = {
        "kind": "nohops-alert",
        "alert": breached is True,
        "breach_reason": breach_reason,
        "rate": rate,
        "threshold": threshold,
        "threshold_source": threshold_source,
        "min_n": min_n(),
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


def alert_text(payload):
    """The human line a deliver thread sees."""
    cat = payload.get("top_blocking_category")
    cat_bit = (f"; top blocking category (TR-185): {cat}"
               f" ({payload.get('top_blocking_category_count')} no-hop"
               f" resolves require it)") if cat else ""
    return (f"NO-HOPS ALERT: rate {payload['rate']} over the last "
            f"{payload['window_h']}h crossed threshold "
            f"{payload['threshold']} ({payload['nohops']}/"
            f"{payload['resolves']} resolves with zero eligible hops"
            f"{cat_bit})")


def _append_event(path, row):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _post_webhook(url, payload, timeout=8):
    """Real POST. Only ever called with a configured URL in real mode."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "task-router-nohops/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def stub_post(url, payload):
    """The dry-run poster: no socket, same signature as _post_webhook."""
    target = url or "(no ROUTER_NOHOPS_WEBHOOK_URL configured)"
    print(f"[dry-run] STUB POST {target}")
    print("[dry-run] POST payload:")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 200


def deliver(result, dry_run=False, now_s=None, post_fn=None, events_path=None):
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


def cmd_check(args):
    store = router_outcomes.outcomes_path()
    rows, meta = load_store_rows(store, scan_limit=args.scan_limit)
    if meta.get("error"):
        print(f"router_nohops: {meta['error']}", file=sys.stderr)
        return 2
    result = evaluate(rows, window_h=args.window_h)
    p = result["payload"]
    if args.alert_dry_run:
        rec = deliver(result, dry_run=True)
        print(f"[dry-run] store: {meta['store']} rows_scanned="
              f"{meta['rows_scanned']} parse_failed={meta['parse_failed']}")
        print(f"[dry-run] delivered to: {'; '.join(rec['delivered'])}")
        return 1 if p["alert"] else 0
    rec = deliver(result, dry_run=False)
    s = result["series"]
    print(f"no-hops rate {p['rate']} over {p['window_h']}h "
          f"(n={p['n']}, threshold {p['threshold']} via "
          f"{p['threshold_source']}) breach_reason={p['breach_reason']}")
    print(f"window scanned {meta['rows_scanned']} store rows "
          f"({meta['parse_failed']} malformed skipped); "
          f"{s['non_resolves_excluded']} non-resolves, "
          f"{s['rejected_excluded']} rejected, "
          f"{s['untimestamped_excluded']} untimestamped excluded")
    if rec["delivered"] != ["nothing (no breach)"]:
        print(f"delivered: {'; '.join(rec['delivered'])}")
        return 1
    return 0


def cmd_status(args):
    store = router_outcomes.outcomes_path()
    rows, meta = load_store_rows(store, scan_limit=args.scan_limit)
    if meta.get("error"):
        print(f"router_nohops: {meta['error']}", file=sys.stderr)
        return 2
    s = evaluate(rows, window_h=args.window_h)["series"]
    out = dict(s, store=meta["store"],
               store_rows_scanned=meta["rows_scanned"],
               store_parse_failed=meta["parse_failed"])
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


def cmd_baseline(args):
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


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="TR-195 no-hops-rate baseline, threshold and alert")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
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
    return cmd_baseline(args)


if __name__ == "__main__":
    raise SystemExit(main())
