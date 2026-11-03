#!/usr/bin/env python3
"""record_outcome.py — self-report a finished session into the task-router outcome store.

WHY
The outcome store (`data/state/outcomes.jsonl`) is the router's learning input:
it feeds the rolling averages that order chains by predicted cost per task.
Until now the only writer on this machine was the hourly CRON importer, which
means the router learned in arrears (up to 60 min) and only when a scheduler
job happened to be running. This script closes that gap the way it should be
closed: the session reports itself the moment it ends.

CONTRACT (the router's own published one, not a new one)
task-router owns `POST /api/v1/outcomes` (scripts/router_server.py, TR-049) and
keys rows `(source_system, session_id, model)` — a re-post never double-counts,
older duplicates than the tail window are accepted, and the rolling average is
decay-weighted so a re-post cannot dominate a bucket. This script posts into
that endpoint, so it inherits:
  * validation (normalize_row names every problem; we print it, we never guess)
  * dedupe (safe to run twice, safe to run after the cron already imported)
  * fail-open (a router or disk problem never blocks the session; we exit 0
    with a one-line warning)

It deliberately does NOT append the JSONL directly: the store is flock-
serialized but not schema-versioned, and going around the API would let two
writers drift.

USAGE (self-report at the end of a session)
  python3 scripts/record_outcome.py --session-id <id> --model <model> \
      --provider <provider> [--task-label ...] [--turns N] [--tokens-in N] \
      [--tokens-out N] [--tokens-reasoning N] [--cost-usd X] [--wall-time-s X] \
      [--success true|false] [--profile-id P1_CODING]

  # read the numbers straight out of a Hermes state.db instead of typing them
  python3 scripts/record_outcome.py --from-state-db <session_id> \
      [--state-db ~/.hermes/state.db]

EXIT CODES
  0  reported, or already reported, or the router is unreachable (fail-open,
     warned on stderr) — a session must never fail because of bookkeeping
  2  the invocation itself is wrong (missing/bad arguments)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("ROUTER_BASE_URL", "http://127.0.0.1:9092")
STATE_DB = os.path.expanduser(os.environ.get("HERMES_STATE_DB", "~/.hermes/state.db"))

# Same host->provider map as router_outcomes.import_hermes: billing_provider is
# re-stamped at gateway restarts, so the base URL host is the immutable truth.
HOST2PROVIDER = {
    "api.xkiro.com": "xkiro",
    "api.cline.bot": "clinepass",
    "openrouter.ai": "openrouter",
    "api.deepseek.com": "deepseek",
    "api.minimax.io": "minimax",
    "api.stepfun.com": "stepfun",
    "api.synthetic.new": "synthetic",
    "api.groq.com": "groq",
    "api.fireworks.ai": "fireworks-ai",
    "ollama.com": "ollama-cloud",
}


def _provider_of(base_url, stamped):
    for host, name in HOST2PROVIDER.items():
        if host in (base_url or ""):
            return name
    return stamped or "unknown"


def _warn(msg):
    print(f"record-outcome: {msg}", file=sys.stderr)


def rows_from_state_db(session_id, db_path=STATE_DB):
    """Aggregate one session from state.db's session_model_usage meter.

    Same source the hermes driver imports (router_outcomes.import_hermes), but
    scoped to ONE session: that is what makes this a self-report rather than a
    re-import. Returns one row per (session_id, model) — matching the store key.
    """
    db = sqlite3.connect(os.path.expanduser(db_path))
    try:
        cur = db.execute(
            """SELECT model, billing_provider, billing_base_url, api_call_count,
                      COALESCE(input_tokens,0), COALESCE(output_tokens,0),
                      COALESCE(reasoning_tokens,0), estimated_cost_usd,
                      first_seen, last_seen
               FROM session_model_usage
               WHERE session_id = ? AND billing_base_url IS NOT NULL
               ORDER BY first_seen""",
            (session_id,),
        )
        out = []
        for model, provider, base, calls, tin, tout, treason, cost, first, last in cur:
            if isinstance(model, bytes):
                model = model.decode("utf-8", "replace")
            if isinstance(provider, bytes):
                provider = provider.decode("utf-8", "replace")
            # billing_provider gets re-stamped to gateway defaults on restart;
            # billing_base_url is the immutable truth, so derive identity from
            # it exactly like router_outcomes.import_hermes does.
            provider = _provider_of(base, provider)
            wall = None
            try:
                if first and last:
                    wall = max(0.0, (float(last) - float(first)))
            except (TypeError, ValueError):
                wall = None
            out.append(
                {
                    "model": model,
                    "provider": provider,
                    "turns": calls,
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "tokens_reasoning": treason,
                    "cost_usd": cost,
                    "wall_time_s": wall,
                }
            )
        return out
    finally:
        db.close()


def post(base, payload):
    url = base.rstrip("/") + "/api/v1/outcomes"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Self-report a finished session to the task-router outcome store"
    )
    ap.add_argument("--session-id", help="session id written to the store")
    ap.add_argument(
        "--from-state-db",
        metavar="SESSION_ID",
        help="read turns/tokens/cost/wall-time out of a Hermes state.db for this session",
    )
    ap.add_argument("--state-db", default=STATE_DB)
    ap.add_argument("--source-system", default="hermes")
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--task-label")
    ap.add_argument("--profile-id")
    ap.add_argument("--turns", type=int)
    ap.add_argument("--tokens-in", type=int)
    ap.add_argument("--tokens-out", type=int)
    ap.add_argument("--tokens-reasoning", type=int)
    ap.add_argument("--cost-usd", type=float)
    ap.add_argument("--wall-time-s", type=float)
    ap.add_argument("--success", choices=["true", "false"])
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--dry-run", action="store_true", help="print the payload, post nothing")
    a = ap.parse_args(argv)

    sid = a.from_state_db or a.session_id
    if not sid:
        _warn("need --session-id or --from-state-db SESSION_ID")
        return 2

    if a.from_state_db:
        try:
            measured = rows_from_state_db(sid, a.state_db)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not break a session
            _warn(f"cannot read {a.state_db}: {type(exc).__name__}: {exc}")
            return 0
        if not measured:
            _warn(f"no metered rows for session {sid} (nothing to report)")
            return 0
    else:
        if not (a.model and a.provider):
            _warn("need --model and --provider (or use --from-state-db)")
            return 2
        measured = [
            {
                "model": a.model,
                "provider": a.provider,
                "turns": a.turns,
                "tokens_in": a.tokens_in,
                "tokens_out": a.tokens_out,
                "tokens_reasoning": a.tokens_reasoning,
                "cost_usd": a.cost_usd,
                "wall_time_s": a.wall_time_s,
            }
        ]

    posted, skipped, failed = 0, 0, 0
    for m in measured:
        payload = {
            "source_system": a.source_system,
            "session_id": sid,
            "provider": m["provider"],
            "model": m["model"],
            "task_label": a.task_label,
            "profile_id": a.profile_id,
            "turns": m["turns"],
            "tokens_in": m["tokens_in"],
            "tokens_out": m["tokens_out"],
            "tokens_reasoning": m["tokens_reasoning"],
            "cost_usd": m["cost_usd"],
            "wall_time_s": m["wall_time_s"],
            "success": None if a.success is None else a.success == "true",
        }
        if a.dry_run:
            print(json.dumps(payload, ensure_ascii=False))
            continue
        try:
            res = post(a.base_url, payload)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _warn(
                f"router unreachable at {a.base_url} ({type(exc).__name__}); "
                f"row kept out of the store — the hourly importer will catch it"
            )
            return 0
        if res.get("appended"):
            posted += 1
        elif "duplicate" in str(res.get("reason", "")):
            skipped += 1
        else:
            failed += 1
            _warn(f"not stored: {res.get('reason') or res.get('error')}")

    if not a.dry_run:
        print(
            f"record-outcome: session {sid} — posted {posted}, already present {skipped}, refused {failed}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
