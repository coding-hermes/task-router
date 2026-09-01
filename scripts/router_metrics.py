#!/usr/bin/env python3
"""router_metrics.py — query CLI for per-hop resolve metrics (TR-021).

Reads an append-only JSONL metrics file produced by router_spawn.py and prints
usage counts per provider/model/pair over a time window.  Pure stdlib, mirrors
the argparse + JSON-purity conventions of the other scripts.

Metrics file location (in order):
  1. $TASK_ROUTER_HOME/metrics.jsonl if TASK_ROUTER_HOME is set
  2. <repo>/data/metrics.jsonl otherwise
The parent directory is created on writes by router_spawn.py; this query tool
reads only.

Rows have this shape (one per chain hop):
  {
    "ts": "2026-09-01T12:34:56+00:00",
    "project": "my-project",
    "profile": "P0_FORE",
    "provider": "ollama-cloud",
    "model": "deepseek-v4-flash:0731",
    "order": 1,
    "price_usd_per_m": 0.0,
    "outcome": "resolved",
    "exclusion_reason": null,
    "config_snapshot": {
      "registry_source": "registry.json",
      "gates_loaded": {...},
      "chain_length": 7,
      "routing_env_vars": ["ROUTING_REGISTRY", "ROUTER_STATE_DIR"]
    }
  }

outcome values: "resolved" | "excluded" | "error"
"""
import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)


def _metrics_path():
    home = os.environ.get("TASK_ROUTER_HOME")
    if home:
        return os.path.join(home, "metrics.jsonl")
    return os.path.join(_REPO, "data", "metrics.jsonl")


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_utc(ts):
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _parse_duration(s):
    """Convert a duration string like 24h, 7d, 30m into seconds."""
    s = str(s).strip()
    if not s:
        return None
    unit = s[-1].lower()
    try:
        num = float(s[:-1])
    except ValueError:
        raise ValueError(f"invalid duration {s!r}: expected <number><h|d|m>")
    if unit == "h":
        return int(num * 3600)
    if unit == "d":
        return int(num * 86400)
    if unit == "m":
        return int(num * 60)
    raise ValueError(f"invalid duration unit {unit!r}: expected h, d, or m")


def _read_rows(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    yield row
    except FileNotFoundError:
        return
    except Exception:
        return


def _filter_rows(path, since=None, profile=None):
    cutoff = None
    if since is not None:
        cutoff = _now_utc() - datetime.timedelta(seconds=since)
    for row in _read_rows(path):
        ts = row.get("ts")
        dt = _parse_utc(ts) if ts else None
        if cutoff is not None and dt is None:
            continue
        if cutoff is not None and dt < cutoff:
            continue
        if profile is not None and row.get("profile") != profile:
            continue
        yield row


def _aggregates(rows):
    providers, models, pairs = {}, {}, {}
    for row in rows:
        p = row.get("provider")
        m = row.get("model")
        pair = f"{p}/{m}" if p and m else None
        providers[p] = providers.get(p, 0) + 1
        models[m] = models.get(m, 0) + 1
        if pair:
            pairs[pair] = pairs.get(pair, 0) + 1
    return providers, models, pairs


def _top(mapping, n):
    items = sorted(mapping.items(), key=lambda kv: (-kv[1], kv[0]))
    if n is not None and n > 0:
        items = items[:n]
    return items


def _table(items, headers):
    if not items:
        print("no rows in window")
        return
    cols = [list(headers)] + [[str(x) for x in row] for row in items]
    widths = [max(len(str(c[i])) for c in cols) for i in range(len(headers))]
    for row in cols:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Resolve metrics query (TR-021)")
    ap.add_argument("--top-providers", nargs="?", const=10, type=int,
                    help="show top N providers by hop count (default 10)")
    ap.add_argument("--top-models", nargs="?", const=10, type=int,
                    help="show top N models by hop count (default 10)")
    ap.add_argument("--top-pairs", nargs="?", const=10, type=int,
                    help="show top N provider/model pairs by hop count (default 10)")
    ap.add_argument("--profile")
    ap.add_argument("--since", help="window like 24h, 7d, 30m")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    since = _parse_duration(args.since) if args.since else None
    path = _metrics_path()
    rows = list(_filter_rows(path, since=since, profile=args.profile))
    providers, models, pairs = _aggregates(rows)

    # Build the requested view.  If no specific top-* flag is given, default to
    # --top-pairs so a bare invocation is useful.
    if args.top_providers is None and args.top_models is None and args.top_pairs is None:
        args.top_pairs = 10

    data = {
        "window": args.since,
        "profile": args.profile,
        "total_hops": len(rows),
        "providers": {k: v for k, v in _top(providers, args.top_providers)},
        "models": {k: v for k, v in _top(models, args.top_models)},
        "pairs": {k: v for k, v in _top(pairs, args.top_pairs)},
    }
    # Invariant check used by tests.
    data["_invariant_check"] = sum(data["providers"].values()) == len(rows) and \
        sum(data["models"].values()) == len(rows) and \
        sum(data["pairs"].values()) == len(rows)

    if args.json:
        print(json.dumps(data, indent=1))
        return 0

    print(f"== metrics ({len(rows)} hops)")
    if args.since:
        print(f"window: last {args.since}")
    if args.profile:
        print(f"profile: {args.profile}")

    if args.top_providers is not None:
        print("\ntop providers:")
        _table([(p, n) for p, n in _top(providers, args.top_providers)],
               ("provider", "hops"))
    if args.top_models is not None:
        print("\ntop models:")
        _table([(m, n) for m, n in _top(models, args.top_models)],
               ("model", "hops"))
    if args.top_pairs is not None:
        print("\ntop pairs:")
        _table([(p, n) for p, n in _top(pairs, args.top_pairs)],
               ("pair", "hops"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
