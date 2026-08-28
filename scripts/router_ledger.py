#!/usr/bin/env python3
"""router_ledger.py — spawn-ledger wiring tool (TR-007, AC3).

The scheduler calls this around every routed spawn; rows land in
~/.hermes/model-router/ledger.jsonl (env LEDGER_FILE overrides the path).
router_spawn.py derives per-(provider, model) in-flight counts from these rows:
a trace whose LAST row is outcome='started' is in flight, and 'started' rows
older than 30 minutes are stale (crash without `end`) and do not count.

Contract (STDLIB ONLY — json/os/sys/argparse/datetime/uuid; no duckdb):

  start --provider P --model M [--project X] [--profile R] [--hop N]
         [--requested-pair A/B] [--reason R] [--trace-id T]
      Append ONE 'started' row. Prints the trace_id on stdout — capture it for
      `end`. trace_id auto-generates ('tr-' + uuid4 hex[:8]) when not given.
      Note: --project/--profile/--requested-pair/--reason/-T values are used
      VERBATIM (treat them as data; they are never parsed back as flags).

  end --trace-id T --outcome success|failure|error [--latency-ms N] [--error-class E]
      [--tokens-in N] [--tokens-out N] [--reason R]
      Append ONE terminal row carrying the same trace_id + outcome + fields.
      Invalid --outcome → usage message on stderr + exit 2 (the only non-zero
      exit). Unknown trace_id (no prior 'started' row) → warning on stderr but
      still appends + exit 0 (fail-open).

  status [--provider P] [--json]
      Per (provider, model): in-flight count (last row 'started', stale >30min
      excluded) + last outcome. --json prints one parseable JSON object.

Fields written are a subset of ledger.schema.json v2's vocabulary; unknown
values are left ABSENT (never fabricated).

Failure mode: appends are open(...,'a') + single write; any append error exits
non-zero on ITS stderr but the CALLER (scheduler) must treat ledger failures as
non-fatal — it still spawns. Missing file/dir is fine for status/read paths.
"""
import argparse
import datetime
import json
import os
import sys
import uuid

SCHEMA_V2_FIELDS = (
    "ts", "project", "profile", "archetype", "provider", "model",
    "requested_pair", "served_pair", "chain_hop", "reason", "quota_before",
    "quota_after", "cache_hit", "tokens_in", "tokens_out", "reasoning_tokens",
    "latency_ms", "retries", "error_class", "cost_usd", "window", "outcome",
    "verifier_result", "trace_id",
)

OUTCOMES = ("success", "failure", "error")
# A 'started' row older than this is stale (crash without `end`) and does not
# count toward in-flight. Kept identical to router_spawn.STALE_MS deliberately;
# stdlib-only file, do not import the duckdb client here.
STALE_MS = 30 * 60 * 1000


def _ledger_path():
    env = os.environ.get("LEDGER_FILE")
    if env:
        return env
    return os.path.join(
        os.path.expanduser("~/.hermes/model-router"), "ledger.jsonl"
    )


def _now_iso():
    # ISO-8601 UTC seconds: what both tools parse losslessly.
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


def _parse_utc(ts):
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _append(row):
    """Atomic-enough single-line append (open+write+\n). Creates dirs/file."""
    path = _ledger_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


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
    except Exception as e:  # unreadable/corrupt — degrade to empty, warn
        print(f"router_ledger: warning: cannot read {path}: {e}",
              file=sys.stderr)


def _subset(fields):
    # also treat "" as unset — a CLI flag given as an empty string means
    # "not provided", never fabricate a field for it
    return {k: v for k, v in fields.items() if v is not None and v != ""}


def cmd_start(args):
    tid = args.trace_id or ("tr-" + uuid.uuid4().hex[:8])
    served_pair = f"{args.provider}/{args.model}"
    row = _subset({
        "ts": _now_iso(),
        "provider": args.provider,
        "model": args.model,
        "requested_pair": args.requested_pair or served_pair,
        "served_pair": served_pair,
        "chain_hop": args.hop,
        "reason": args.reason,
        "outcome": "started",
        "trace_id": tid,
        **({"project": args.project} if args.project else {}),
        **({"profile": args.profile} if args.profile else {}),
    })
    assert set(row) <= set(SCHEMA_V2_FIELDS), row.keys()
    _append(row)
    print(tid)  # scheduler captures this for `end`
    return 0


def cmd_end(args):
    if args.outcome not in OUTCOMES:
        print(f"router_ledger: invalid --outcome {args.outcome!r} "
              f"(must be one of {'|'.join(OUTCOMES)})", file=sys.stderr)
        return 2
    known = False
    for r in _read_rows(_ledger_path()):
        if r.get("trace_id") == args.trace_id:
            known = True
            break
    if not known:
        print(f"router_ledger: warning: trace {args.trace_id!r} has no "
              f"'started' row visible; appending terminal row anyway "
              f"(fail-open)", file=sys.stderr)
    row = _subset({
        "ts": _now_iso(),
        "trace_id": args.trace_id,
        "outcome": args.outcome,
        "latency_ms": args.latency_ms,
        "error_class": args.error_class,
        "tokens_in": args.tokens_in,
        "tokens_out": args.tokens_out,
        "reason": args.reason,
    })
    assert set(row) >= {"trace_id", "outcome"}, "end row must identify itself"
    _append(row)
    print(args.trace_id)
    return 0


def cmd_status(args):
    # Traces are reconstructed by trace_id across ALL rows (terminal rows carry
    # no provider/model): last outcome decides flight state, pair comes from
    # whichever row carries one. Mirrors router_spawn.ledger_in_flight exactly.
    last, pair_of = {}, {}
    path = _ledger_path()
    for r in _read_rows(path):
        tid = r.get("trace_id")
        if not tid:
            continue
        prev = last.get(tid)
        if prev is None:
            prev = {"outcome": None, "ts": None}
            last[tid] = prev
        if r.get("provider") and r.get("model"):
            pair_of[tid] = f"{r['provider']}/{r['model']}"
        if r.get("outcome") is not None:
            prev["outcome"] = r["outcome"]
        if r.get("ts") is not None:
            prev["ts"] = r["ts"]
    now = datetime.datetime.now(datetime.timezone.utc)
    inflight, last_outcome = {}, {}
    for tid, rec in last.items():
        pair = pair_of.get(tid)
        if not pair:
            continue  # terminal-only trace (unknown trace_id `end`): unattributable
        outcome, ts = rec["outcome"], rec["ts"]
        tsdt = _parse_utc(ts)
        fresh = bool(tsdt is not None and (now - tsdt).total_seconds() * 1000 <= STALE_MS)
        if outcome == "started" and fresh:
            inflight[pair] = inflight.get(pair, 0) + 1
        if outcome in OUTCOMES:
            last_outcome[pair] = outcome
    provider = args.provider
    pairs = sorted(set(inflight) | set(last_outcome))
    if provider:
        prefix = provider.rstrip("/") + "/"
        pairs = [p for p in pairs if p.startswith(prefix)]
    # TR-026 visible disable: zero traces = the ledger exists but start/end is
    # never called (scheduler not wired). wired=false tells any consumer that
    # concurrency accounting is NOT active — a missing data feed is a visible
    # gap, never a silent empty.
    n_traces = len(last)
    n_terminal = sum(
        1 for rec in last.values() if rec.get("outcome") in OUTCOMES
    )
    wired = n_traces > 0
    result = {
        "wired": wired,
        "rows": n_traces,
        "started_open": sum(
            1 for rec in last.values() if rec.get("outcome") == "started"
        ),
        "terminal": n_terminal,
        "stale_after_minutes": STALE_MS // 60000,
        "pairs": [
            {
                "pair": p,
                "in_flight": inflight.get(p, 0),
                "last_outcome": last_outcome.get(p),
            }
            for p in pairs
        ],
    }
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        if not wired:
            print("WARNING: ledger NOT WIRED — no trace rows found; the")
            print("  'model busy' concurrency gate cannot fire (TR-026).")
            print("  Until the scheduler calls router_ledger.py start/end")
            print("  around spawns, concurrency accounting is inactive.")
        print(f"in-flight (stale >{STALE_MS // 60000}m ignored):")
        if not pairs:
            print("  (no traces recorded)")
        for p in pairs:
            e = next(e for e in result["pairs"] if e["pair"] == p)
            lo = f" last={e['last_outcome']}" if e["last_outcome"] else ""
            n = e["in_flight"]
            mark = "*" if n else " "
            print(f" {mark} {p}: in_flight={n}{lo}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="task-router spawn ledger — start/end/status (TR-007)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_start = sub.add_parser("start", help="log a spawned call (outcome=started)")
    ap_start.add_argument("--provider", required=True)
    ap_start.add_argument("--model", required=True)
    ap_start.add_argument("--project")
    ap_start.add_argument("--profile")
    ap_start.add_argument("--hop", type=int)
    ap_start.add_argument("--requested-pair")
    ap_start.add_argument("--reason")
    ap_start.add_argument("--trace-id")

    ap_end = sub.add_parser("end", help="close a trace (success|failure|error)")
    ap_end.add_argument("--trace-id", required=True)
    ap_end.add_argument("--outcome", required=True,
                        choices=list(OUTCOMES))  # argparse rejects → exit 2
    ap_end.add_argument("--latency-ms", type=int)
    ap_end.add_argument("--error-class")
    ap_end.add_argument("--tokens-in", type=int)
    ap_end.add_argument("--tokens-out", type=int)
    ap_end.add_argument("--reason")

    ap_status = sub.add_parser("status", help="in-flight counts per (provider, model)")
    ap_status.add_argument("--provider")
    ap_status.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "start":
        sys.exit(cmd_start(args))
    elif args.cmd == "end":
        sys.exit(cmd_end(args))
    elif args.cmd == "status":
        sys.exit(cmd_status(args))


if __name__ == "__main__":
    main()
