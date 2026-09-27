#!/usr/bin/env python3
"""proxy_e2e — ONE end-to-end proxied request, and a row that must explain itself.

TR-188. Drives a real request through the whole stack:

    caller -> proxy (:9391 POST /v1/chat/completions)
           -> outcomes ledger row (data/state/outcomes.jsonl)
           -> router API (:9092 GET /api/ui/flow?id=<session>)

and asserts, IN ORDER:
  1. the response is HTTP 200 or a STRUCTURED failure (JSON with an `error`
     key — the proxy's failure envelope). A transport error or an unparseable
     body fails here.
  2. a ledger row exists for that session.
  3. every row for the session is INTERPRETABLE: served-with-chain,
     failed-with-reason, or no-hops-with-reason — the outcome always names the
     evidence that explains it, never ambiguous. (A structured failure is an
     allowed step-1 result, so the `failed-with-reason` shape is interpretable
     too; a battery that only passes on HTTP 200 would contradict step 1.)
  4. no row is a FAKE ZERO: cost_usd == 0.0 exactly with no price_basis.
     cost None WITH a basis string is fine and reported as unknown-with-basis
     (unknown is reported as unknown, never as free). A None with NO basis is
     also a failure — an unexplained null on a live row is the same ambiguity
     the fake zero is.
  5. GET /api/ui/flow on the session id returns found=true with
     artefacts_available / artefacts_missing present (lists — a missing
     artefact is reported, not fatal).

ONE request per run — no retries: a battery that retries is measuring the
second hop, not the stack. `--timeout` bounds the request (default 600s).

Stdlib only. Never prints the upstream credential.

Usage:
    python3 scripts/proxy_e2e.py                     # live battery
    python3 scripts/proxy_e2e.py --json out.json     # also write the summary
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The header the proxy reads for the caller's session marker (SOURCE B/C:
#: x-router-session names the row; X-Hermes-Session-Key keeps the gateway
#: session continuous). Both carry the SAME tick so the ledger row, the
#: gateway session and the flow drill-down are one findable story.
SESSION_HEADER = "x-router-session"
GATEWAY_SESSION_HEADER = "X-Hermes-Session-Key"

CHECK_ORDER = [
    "response-ok-or-structured",
    "ledger-row-exists",
    "row-interpretable",
    "no-fake-zero",
    "flow-found-with-artefacts",
]


# --------------------------------------------------------------------------- parsing

def parse_ledger_rows(text):
    """The JSONL rows in a ledger dump, tolerantly: malformed lines are
    skipped (the store is append-only and a torn tail line must not take the
    battery down). Returns (rows, malformed_count)."""
    rows, malformed = [], 0
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(d, dict):
            rows.append(d)
    return rows, malformed


def rows_for_session(rows, session_id):
    """Every row that belongs to this session, in file order.

    Same match the flow drill-down uses: the row's own session_id
    (`router-proxy:<tick>`) or its parent_session_id (the bare tick).
    """
    sid = str(session_id)
    return [r for r in rows
            if str(r.get("session_id")) == sid or str(r.get("parent_session_id")) == sid]


def classify_cost(row):
    """The cost verdict for ONE row — the fake-zero guard, precisely.

    fake-zero:      cost_usd == 0.0 exactly AND no price_basis  -> FAIL
    zero-with-basis: 0.0 WITH a basis                            -> fine (a real free/plan row says so)
    unknown-with-basis: None WITH a basis                       -> fine, reported as such
    unpriced-no-basis:  None with NO basis                      -> FAIL (unexplained null)
    """
    cost = row.get("cost_usd")
    basis = row.get("price_basis")
    basis = basis.strip() if isinstance(basis, str) else basis
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost == 0.0:
        if basis:
            return {"verdict": "zero-with-basis", "ok": True,
                    "cost_usd": cost, "price_basis": basis}
        return {"verdict": "fake-zero", "ok": False,
                "cost_usd": cost, "price_basis": None,
                "reason": "cost_usd is exactly 0.0 with no price_basis — an unknown "
                          "price masquerading as free (the fake-zero class)"}
    if cost is None:
        if basis:
            return {"verdict": "unknown-with-basis", "ok": True,
                    "cost_usd": None, "price_basis": basis,
                    "reason": f"cost unknown, basis says why: {basis}"}
        return {"verdict": "unpriced-no-basis", "ok": False,
                "cost_usd": None, "price_basis": None,
                "reason": "cost_usd is None with no price_basis — unknown with no reason"}
    return {"verdict": "priced", "ok": True, "cost_usd": cost, "price_basis": basis}


def _has_chain_evidence(row):
    """Does the row say what the option chain was? (chain list, or its length.)"""
    chain = row.get("chain")
    if isinstance(chain, list) and chain:
        return True
    length = row.get("chain_length")
    return isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0


def _has_reason(row):
    """Any of the reason fields that explain why this outcome happened."""
    for k in ("failure_reason", "degrade_reason"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return True
    ce = row.get("chain_evidence")
    if isinstance(ce, dict) and (ce.get("exclusions") or ce.get("gate")):
        return True
    return False


def classify_row(row):
    """Is this ONE row interpretable — outcome + the evidence that explains it?

    interpretable shapes: served-with-chain, failed-with-reason,
    no-hops-with-reason. Everything else is ambiguous, and the verdict names
    WHICH fact is missing so the failure is actionable.
    """
    outcome = row.get("route_outcome")
    attempted = row.get("hops_attempted")
    attempted = attempted if isinstance(attempted, (int, float)) and not isinstance(attempted, bool) else None
    if outcome == "served":
        if row.get("served_by_hop") is None:
            return {"verdict": "ambiguous-served-without-hop", "ok": False,
                    "reason": "route_outcome=served but served_by_hop is missing"}
        if not _has_chain_evidence(row):
            return {"verdict": "ambiguous-served-without-chain", "ok": False,
                    "reason": "route_outcome=served but the row carries no chain evidence "
                              "(chain/chain_length absent or empty)"}
        return {"verdict": "served-with-chain", "ok": True}
    if outcome == "no-hops":
        if not _has_reason(row):
            return {"verdict": "ambiguous-no-hops-no-reason", "ok": False,
                    "reason": "route_outcome=no-hops and the row cannot say WHY nothing "
                              "was eligible (no failure/degrade reason, no exclusions)"}
        return {"verdict": "no-hops-with-reason", "ok": True}
    if outcome == "failed":
        if not _has_reason(row):
            return {"verdict": "ambiguous-failed-no-reason", "ok": False,
                    "reason": "route_outcome=failed but no failure_reason"}
        if attempted in (None, 0):
            return {"verdict": "ambiguous-failed-without-attempts", "ok": False,
                    "reason": "route_outcome=failed but hops_attempted is missing or 0"}
        return {"verdict": "failed-with-reason", "ok": True}
    return {"verdict": "ambiguous-outcome-missing", "ok": False,
            "reason": f"route_outcome is {outcome!r} — the row does not say which door it came out of"}


def parse_flow(flow_json):
    """Normalise the /api/ui/flow answer for assertion.

    Returns {found, present: bool, artefacts_available, artefacts_missing,
    hops_attempted, served_position, cost_usd, error}.
    `present` is the criterion-5 shape check: found=true AND both artefact
    lists exist (as lists). A found-but-malformed payload fails here.
    """
    if not isinstance(flow_json, dict):
        return {"found": False, "present": False, "artefacts_available": [],
                "artefacts_missing": [], "hops_attempted": None,
                "served_position": None, "cost_usd": None,
                "error": "flow response is not a JSON object"}
    found = flow_json.get("found") is True
    avail = flow_json.get("artefacts_available")
    missing = flow_json.get("artefacts_missing")
    present = found and isinstance(avail, list) and isinstance(missing, list)
    hops = (flow_json.get("hops") or {}) if isinstance(flow_json.get("hops"), dict) else {}
    request = (flow_json.get("request") or {}) if isinstance(flow_json.get("request"), dict) else {}
    return {
        "found": found,
        "present": present,
        "artefacts_available": avail if isinstance(avail, list) else [],
        "artefacts_missing": missing if isinstance(missing, list) else [],
        "hops_attempted": hops.get("attempted"),
        "served_position": hops.get("served_position"),
        "cost_usd": request.get("cost_usd"),
        "error": flow_json.get("error"),
    }


# --------------------------------------------------------------------------- evaluation

def evaluate(response, rows, flow_json, session_id):
    """The whole battery as an ordered check list. Pure: no I/O.

    response: {"status": int|None, "body": parsed-or-None, "error": str|None}
    rows:     the ledger rows matching this session
    flow_json: the parsed /api/ui/flow payload (or None)
    Returns the summary dict (see main).
    """
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    # 1 — HTTP 200 or a structured failure
    status = (response or {}).get("status")
    body = (response or {}).get("body")
    err = (response or {}).get("error")
    if status == 200:
        add("response-ok-or-structured", True, "HTTP 200")
    elif isinstance(status, int) and status >= 400 and isinstance(body, dict) and body.get("error"):
        add("response-ok-or-structured", True,
            f"HTTP {status} structured failure: {str(body.get('error'))[:160]}")
    elif err:
        add("response-ok-or-structured", False, f"transport error: {err}")
    else:
        add("response-ok-or-structured", False,
            f"HTTP {status} with no parseable error body — an unstructured failure")

    # 2 — a ledger row exists for the session
    add("ledger-row-exists", bool(rows),
        f"{len(rows)} ledger row(s) for session {session_id}" if rows
        else f"no ledger row for session {session_id}")

    # 3 — every row is interpretable
    classified = [{"session_row": _row_brief(r), **classify_row(r)} for r in rows]
    bad = [c for c in classified if not c["ok"]]
    parts = [f"row[{i}] {c['verdict']}" if c["ok"]
             else f"row[{i}] {c['verdict']}: {c.get('reason')}"
             for i, c in enumerate(classified)]
    add("row-interpretable", not bad and bool(rows), "; ".join(parts) or "no rows")

    # 4 — the fake-zero guard (and unexplained-null cost)
    cost_verdicts = [classify_cost(r) for r in rows]
    bad_cost = [c for c in cost_verdicts if not c["ok"]]
    cost_parts = [f"{c['verdict']}: {c.get('reason', c['verdict'])}" for c in bad_cost] or \
                 [f"row {r.get('provider')}/{r.get('model')}: ${c.get('cost_usd')} "
                  f"({c.get('price_basis') or c.get('verdict')})"
                  for r, c in zip(rows, cost_verdicts)]
    add("no-fake-zero", not bad_cost and bool(rows),
        "; ".join(cost_parts) or "no rows")

    # 5 — flow found with the artefact lists present
    flow = parse_flow(flow_json) if flow_json is not None else {
        "found": False, "present": False, "artefacts_available": [],
        "artefacts_missing": [], "hops_attempted": None,
        "served_position": None, "cost_usd": None,
        "error": "no flow response (request failed before the flow read)"}
    add("flow-found-with-artefacts", flow["present"],
        (f"found=true, available={flow['artefacts_available']}, "
         f"missing={flow['artefacts_missing']}")
        if flow["present"] else
        f"found={flow['found']}, artefact lists present="
        f"{isinstance(flow_json, dict) and 'artefacts_available' in (flow_json or {})} "
        f"— {(flow.get('error') or 'flow drill-down did not return the required shape')}")

    served = next((c for c in classified if c["verdict"] == "served-with-chain"), None)
    last_outcome = classified[-1]["session_row"].get("route_outcome") if classified else None
    outcome = ("served" if served
               else (last_outcome if last_outcome else "unknown"))
    best_cost = next((c for c in cost_verdicts if c["verdict"] == "priced"),
                     cost_verdicts[0] if cost_verdicts else None)
    summary = {
        "session_id": session_id,
        "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": all(c["ok"] for c in checks),
        "outcome": outcome,
        "cost": ({"usd": best_cost.get("cost_usd"), "basis": best_cost.get("price_basis"),
                  "verdict": best_cost.get("verdict"),
                  "reason": best_cost.get("reason")} if best_cost else None),
        "hops": {"attempted": flow["hops_attempted"],
                 "served_position": flow["served_position"]},
        "served_hop": served["session_row"].get("served_by_hop") if served else None,
        "artefacts_available": flow["artefacts_available"],
        "artefacts_missing": flow["artefacts_missing"],
        "flow_found": flow["found"],
        "rows": classified,
        "checks": checks,
    }
    return summary


def _row_brief(row):
    """The identity slice of a row, for the per-row classification report."""
    return {"session_id": row.get("session_id"), "provider": row.get("provider"),
            "model": row.get("model"), "route_outcome": row.get("route_outcome"),
            "served_by_hop": row.get("served_by_hop"),
            "cost_usd": row.get("cost_usd"), "price_basis": row.get("price_basis")}


def human_tail(summary):
    """The human-readable tail: the six facts the brief demands, one line each."""
    cost = summary.get("cost") or {}
    hops = summary.get("hops") or {}
    lines = [
        "",
        "proxy_e2e — " + ("PASS" if summary.get("ok") else "FAIL"),
        f"  session id       : {summary.get('session_id')}",
        f"  outcome          : {summary.get('outcome')}",
    ]
    if cost.get("verdict") in ("priced", "zero-with-basis"):
        lines.append(f"  cost             : ${cost.get('usd')} ({cost.get('basis')})")
    else:
        lines.append(f"  cost             : UNKNOWN — {cost.get('reason', cost.get('verdict', 'no rows'))}")
    lines.append(f"  hops attempted   : {hops.get('attempted')}")
    lines.append(f"  served hop       : {summary.get('served_hop') if summary.get('served_hop') is not None else 'none'}")
    lines.append(f"  artefacts missing: {summary.get('artefacts_missing') or '[]'}")
    for c in summary.get("checks", []):
        lines.append(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- live I/O

def upstream_key():
    """The credential for the gateway upstream, BY NAME from ~/.hermes/.env.

    The value is never printed and never logged; it only rides the
    Authorization header, exactly like proxy_smoke.
    """
    env = os.environ.get("ROUTER_E2E_KEY")
    if env:
        return env
    p = Path(os.path.expanduser("~/.hermes/.env"))
    if not p.exists():
        return ""
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def send_request(base, session_id, prompt, model, timeout, key):
    """ONE POST /v1/chat/completions through the proxy. Returns the response
    dict {status, body, error} — body is the parsed JSON when parseable."""
    url = base.rstrip("/") + "/v1/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    headers = {"Content-Type": "application/json",
               SESSION_HEADER: session_id,
               GATEWAY_SESSION_HEADER: session_id}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        status = e.code
    except Exception as e:  # noqa: BLE001 — the battery must report, not crash
        return {"status": None, "body": None, "error": f"{type(e).__name__}: {e}",
                "seconds": round(time.time() - t0, 3)}
    body = None
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        body = None
    return {"status": status, "body": body, "error": None,
            "seconds": round(time.time() - t0, 3)}


def read_flow(router_api, session_id, timeout):
    """GET /api/ui/flow?id=<session_id>. Returns the parsed JSON or None."""
    url = (router_api.rstrip("/") + "/api/ui/flow?"
           + urllib.parse.urlencode({"id": session_id}))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads((e.read() or b"").decode("utf-8", "replace"))
        except ValueError:
            return {"found": False, "error": f"HTTP {e.code} (unparseable body)"}
    except Exception as e:  # noqa: BLE001
        return {"found": False, "error": f"{type(e).__name__}: {e}"}


def await_rows(ledger_path, session_id, wait_s, poll_s=0.5):
    """Poll the ledger until at least one row for the session appears (the
    record path is best-effort and may land a beat after the HTTP answer)."""
    deadline = time.time() + max(0.0, wait_s)
    while True:
        rows, malformed = parse_ledger_rows(_read_text(ledger_path))
        matched = rows_for_session(rows, session_id)
        if matched:
            return matched, malformed
        if time.time() >= deadline:
            return [], malformed
        time.sleep(poll_s)


def _read_text(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def ledger_default():
    """Same resolution as router_outcomes.outcomes_path: env wins, then the
    repo this script ships in (so a live install reads the live store)."""
    return os.environ.get("ROUTING_OUTCOMES_FILE") or str(REPO / "data" / "state" / "outcomes.jsonl")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=os.environ.get("ROUTER_PROXY_URL", "http://127.0.0.1:9391"),
                    help="proxy base URL (default http://127.0.0.1:9391)")
    ap.add_argument("--router-api", default=os.environ.get("ROUTER_API_URL", "http://127.0.0.1:9092"),
                    help="router API base (default http://127.0.0.1:9092)")
    ap.add_argument("--ledger", default=ledger_default(),
                    help="outcomes ledger path (default $ROUTING_OUTCOMES_FILE or the repo store)")
    ap.add_argument("--session", default=None,
                    help="session tick (default router-e2e-<epoch>)")
    ap.add_argument("--model", default="router")
    ap.add_argument("--prompt", default="Reply with exactly: E2E-OK")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="request timeout in seconds (default 600)")
    ap.add_argument("--row-wait", type=float, default=30.0,
                    help="seconds to wait for the ledger row to land (default 30)")
    ap.add_argument("--json", default=None, help="also write the summary JSON here")
    args = ap.parse_args(argv)

    tick = args.session or f"router-e2e-{int(time.time())}"
    key = upstream_key()
    if not key:
        print("proxy_e2e: WARNING no API_SERVER_KEY found — sending without "
              "Authorization (the upstream may fail every hop)", file=sys.stderr)

    print(f"proxy_e2e: one request -> {args.base} /v1/chat/completions "
          f"(timeout {args.timeout:g}s, session {tick})")
    response = send_request(args.base, tick, args.prompt, args.model, args.timeout, key)
    print(f"  proxy answered in {response.get('seconds')}s: "
          f"{response.get('status') or response.get('error')}")

    rows, malformed = await_rows(args.ledger, tick, args.row_wait)
    if malformed:
        print(f"  ledger: skipped {malformed} malformed line(s)", file=sys.stderr)
    flow_json = read_flow(args.router_api, tick, min(args.timeout, 60.0))

    summary = evaluate(response, rows, flow_json, tick)
    text = json.dumps(summary, indent=2, default=str)
    print(text)
    print(human_tail(summary))
    if args.json:
        Path(args.json).write_text(text + "\n")
        print(f"  summary written: {args.json}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
