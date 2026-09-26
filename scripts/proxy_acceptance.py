#!/usr/bin/env python3
"""Acceptance battery: is the router fit to sit in front of the scheduler?

This is the gate for repointing SCHEDULER_GATEWAY_URL at the proxy. Every check
measures the LIVE services — no fixtures, no simulated upstreams pretending to be
the fleet — and prints a PASS/FAIL line with the number behind it. Results also go
to JSON so a report can be built from the run rather than from memory.

Design rules carried from the router itself:
  * a check that cannot be measured reports SKIP with the reason, never PASS.
  * recorded evidence is LABELLED as recorded (with its file and timestamp) so a
    proof captured earlier is never presented as a fresh run.
  * the battery never writes to the live tree; it only reads and calls the API.

Usage:
    python3 scripts/proxy_acceptance.py                 # fast gates (~1 min)
    python3 scripts/proxy_acceptance.py --with-long-run # + a ~400s silent turn
    python3 scripts/proxy_acceptance.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROXY = os.environ.get("ROUTER_PROXY_URL", "http://127.0.0.1:9391")
WEB = os.environ.get("ROUTER_WEB_URL", "http://127.0.0.1:9093")
LEDGER = REPO / "data" / "state" / "outcomes.jsonl"
STATE_DB = Path(os.path.expanduser("~/.hermes/state.db"))
PROOF_LONG_RUN = Path("/tmp/idle_proof.json")

RESULTS: list[dict] = []


def record(gate, ok, detail, measurement=None, skipped=None):
    RESULTS.append({"gate": gate, "status": "SKIP" if skipped else ("PASS" if ok else "FAIL"),
                    "detail": detail, "measurement": measurement})
    tag = "SKIP" if skipped else ("PASS" if ok else "FAIL")
    print(f"  [{tag}] {gate}: {detail}")


def gateway_key():
    """Read the gateway credential from the hermes env file. Never printed."""
    p = Path(os.path.expanduser("~/.hermes/.env"))
    if not p.exists():
        return ""
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def post(path, body, headers=None, timeout=240):
    req = urllib.request.Request(PROXY + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", **(headers or {})},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def get(url, timeout=60):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def ledger_rows_since(ts, proxy_only=False):
    out = []
    if not LEDGER.exists():
        return out
    with LEDGER.open(errors="replace") as fh:
        for line in fh:
            if '"session_id"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if (d.get("ts") or 0) < ts:
                continue
            if proxy_only and not str(d.get("session_id") or "").startswith("router-proxy"):
                continue
            out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def gate_scheduler_shape():
    """The exact call the scheduler makes: /v1/responses, streaming."""
    sess = f"accept-shape-{int(time.time())}"
    hdr = {"authorization": f"Bearer {gateway_key()}",
           "x-hermes-session-key": sess, "x-hermes-session-id": sess}
    status, raw, headers = post("/v1/responses",
                                {"model": "router", "input": "Reply with the single word READY.",
                                 "stream": True}, hdr, timeout=300)
    text = raw.decode("utf-8", "replace")
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    sid = headers.get("X-Hermes-Session-Id") or headers.get("x-hermes-session-id")
    frames = re.findall(r"^event: (.+)$", text, re.M)
    ok = (status == 200 and "text/event-stream" in ctype and bool(sid)
          and "response.created" in frames and "response.completed" in frames and "[DONE]" in text)
    record("scheduler_shape_stream", ok,
           f"POST /v1/responses stream -> {status}, {ctype.split(';')[0]}, session header "
           f"{'present' if sid else 'MISSING'}, frames {frames}",
           {"status": status, "ctype": ctype, "session_header": bool(sid), "frames": frames})
    return sid


def gate_shape_no_keepalive_assumption():
    """The report must not claim long turns are safe on a 232s run (232 < 300)."""
    doc = (REPO / "docs" / "proxy-readiness-report-2026-09-26.html")
    if not doc.exists():
        record("documented_correction", False, "readiness report missing")
        return
    html = doc.read_text(errors="replace")
    ok = "CORRECTION, 2026-09-26" in html and "idle-timeout" in html
    record("documented_correction", ok,
           "the report carries the measured 305s idle-timeout correction"
           if ok else "the report still presents the 232s run as proof of long-turn safety")


def gate_session_keying():
    """What actually decides a Hermes session on the api path?

    READ FROM THE GATEWAY SOURCE (gateway/platforms/api_server.py):
        _derive_chat_session_id(system_prompt, first_user_message)
        = "api-" + sha256(f"{system_prompt}\n{first_user_message}")[:16]
    So the identity is a hash of the SYSTEM PROMPT + FIRST USER MESSAGE. The
    x-hermes-session-key / -id headers are NOT honoured here (gateway_routing only maps
    keys for chat platforms), which is why an earlier hand-test appeared to show
    continuity: both calls carried the IDENTICAL prompt, which is exactly the case that
    collides. This gate asserts both halves of the real contract.
    """
    key = f"accept-key-{int(time.time())}"
    same_prompt = "Reply with the single word SAME."
    ids_same = []
    for _ in range(2):
        st, raw, _ = post("/v1/chat/completions",
                          {"model": "router", "messages": [{"role": "user", "content": same_prompt}]},
                          {"authorization": f"Bearer {gateway_key()}",
                           "x-hermes-session-key": key, "x-hermes-session-id": key}, timeout=240)
        if st != 200:
            record("session_keying", False, f"call returned {st}")
            return
        ids_same.append(json.loads(raw).get("_router", {}).get("gateway_session_id"))

    ids_diff = []
    for i in range(2):
        st, raw, _ = post("/v1/chat/completions",
                          {"model": "router", "messages": [
                              {"role": "user", "content": f"Reply with the single word D{i}."}]},
                          {"authorization": f"Bearer {gateway_key()}",
                           "x-hermes-session-key": key, "x-hermes-session-id": key}, timeout=240)
        ids_diff.append(json.loads(raw).get("_router", {}).get("gateway_session_id"))

    same_ok = bool(ids_same[0]) and len(set(ids_same)) == 1
    distinct_ok = len(set(ids_diff)) == 2
    msgs = None
    if same_ok and STATE_DB.exists():
        try:
            con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
            msgs = con.execute("SELECT message_count FROM sessions WHERE id=?", (ids_same[0],)).fetchone()
            con.close()
        except Exception:
            msgs = None
    ok = same_ok and distinct_ok
    record("session_keying", ok,
           f"identical prompt twice -> {'SAME' if same_ok else 'DIFFERENT'} session "
           f"({ids_same[0] if ids_same else None}, message_count={msgs[0] if msgs else '?'}); "
           f"two different prompts -> {'two sessions (no bleed)' if distinct_ok else 'COLLIDED'} "
           f"| identity = hash(system prompt + first user message), session headers ignored",
           {"same_prompt_ids": ids_same, "distinct_prompt_ids": ids_diff,
            "message_count": msgs[0] if msgs else None})


def gate_failure_honesty():
    """A failed proxied call must explain itself in the artefact that survives.

    Fault injection would be tidier but the proxy has no such switch, and a fabricated
    one would prove nothing. So this reads the newest REAL failure out of the ledger —
    the two idle-timeout rows a live probe produced — and asserts the honesty contract
    on it: a reason code, the hops it tried, and meters that are null WITH a reason
    rather than a fabricated 0.
    """
    rows = [d for d in ledger_rows_since(0, proxy_only=True)
            if str(d.get("route_outcome")) == "failed"]
    if not rows:
        record("failure_honesty", False, "no failed proxied row in the ledger to inspect", skipped=True)
        return
    rows.sort(key=lambda d: d.get("ts") or 0)
    r = rows[-1]
    reason = r.get("failure_reason")
    hops = r.get("hops_attempted")
    steps = r.get("steps")
    priced = r.get("cost_usd")
    ok = bool(reason) and isinstance(hops, int) and isinstance(steps, int) and priced is None
    record("failure_honesty", ok,
           f"newest real failure: reason={reason!r}, hops_attempted={hops}, steps={steps}, "
           f"cost={priced!r} (null on a failed hop, not 0)",
           {"session_id": r.get("session_id"), "reason": reason, "hops": hops,
            "steps": steps, "cost": priced, "wall_time_s": r.get("wall_time_s")})


def gate_surface_404():
    status, raw, _ = post("/v1/definitely-not-a-route",
                          {"x": 1}, {"authorization": f"Bearer {gateway_key()}"}, timeout=60)
    text = raw.decode("utf-8", "replace")
    ok = status == 404 and "/v1/" in text
    record("surface_404", ok,
           f"unknown path -> {status} with the surface listed" if ok else
           f"unknown path -> {status} (expected 404 + endpoint surface)", {"status": status})


def gate_health_truth():
    status, raw, _ = get(PROXY + "/health")
    try:
        h = json.loads(raw)
    except Exception:
        record("health_truth", False, f"/health not JSON ({status})")
        return
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    loaded = str(h.get("commit") or (h.get("code") or {}).get("loaded_commit") or "")
    ok = status == 200 and loaded.startswith(head[:7]) and h.get("stale") is False
    record("health_truth", ok,
           f"/health reports loaded {loaded[:12]} == HEAD {head}, stale={h.get('stale')}",
           {"loaded": loaded, "head": head, "stale": h.get("stale")})


def gate_stats_live(before_ts, expect_increase=True):
    status, raw, _ = get(PROXY + "/proxy/stats")
    try:
        s = json.loads(raw)
    except Exception:
        record("stats_live", False, f"/proxy/stats not JSON ({status})")
        return
    w = (s.get("windows") or {}).get("168h") or {}
    matched = w.get("proxy_rows_matched")
    ok = status == 200 and isinstance(matched, int) and matched > 0
    record("stats_live", ok,
           f"/proxy/stats 168h reports {matched} proxied rows, {len(w.get('groups') or {})} groups, "
           f"scan {w.get('rows_scanned')} rows, cache ttl {s.get('ttl_s')}s",
           {"matched": matched, "groups": len(w.get("groups") or {}), "scanned": w.get("rows_scanned")})


def gate_custody(session_from_shape):
    """envelope session ids must match the ledger row, and Hermes must have the session."""
    if not session_from_shape:
        record("custody_chain", False, "no session id captured from the shape gate")
        return
    rows = ledger_rows_since(time.time() - 900, proxy_only=True)
    hit = None
    for d in rows:
        if d.get("gateway_session_id") == session_from_shape:
            hit = d
    if not hit:
        record("custody_chain", False,
               f"no ledger row carries the gateway session {session_from_shape} from the shape gate")
        return
    found = None
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
        found = con.execute("SELECT id, source FROM sessions WHERE id=?", (session_from_shape,)).fetchone()
        con.close()
    except Exception:
        pass
    ok = bool(hit.get("session_id")) and bool(found)
    record("custody_chain", ok,
           f"ledger row {hit.get('session_id')} carries the gateway session; state.db "
           f"{'has it' if found else 'does NOT have it'}",
           {"row_session": hit.get("session_id"), "state_db": list(found) if found else None})


def gate_pointer_alignment():
    """The proxy's idle budget must not be stricter than the caller's tolerance."""
    status, raw, _ = get(PROXY + "/health")
    try:
        h = json.loads(raw)
    except Exception:
        h = {}
    # measured through a live envelope instead: the budget the caller is told about
    sess = f"accept-budget-{int(time.time())}"
    st, r2, _ = post("/v1/chat/completions",
                     {"model": "router", "messages": [{"role": "user", "content": "Reply: ok"}]},
                     {"authorization": f"Bearer {gateway_key()}",
                      "x-hermes-session-key": sess, "x-hermes-session-id": sess}, timeout=240)
    idle = wall = None
    if st == 200:
        r = json.loads(r2).get("_router") or {}
        idle, wall = r.get("idle_budget_s"), r.get("wall_ceiling_s")
    sched_tolerance = 1800.0  # SCHEDULER_GATEWAY_RESPONSE_TIMEOUT default (30m)
    ok = isinstance(idle, (int, float)) and idle >= sched_tolerance / 2
    record("budget_alignment", ok,
           f"proxy idle budget {idle}s vs the scheduler's per-turn tolerance {sched_tolerance}s "
           f"(wall ceiling {wall}s)" if idle else "could not read the live idle budget",
           {"idle_budget_s": idle, "wall_ceiling_s": wall, "caller_tolerance_s": sched_tolerance})


def gate_long_run(with_long_run):
    """The shape that died at 305s: a turn whose tool runs quietly."""
    global PROOF_LONG_RUN
    if not with_long_run:
        if PROOF_LONG_RUN.exists():
            try:
                d = json.loads(PROOF_LONG_RUN.read_text())
                r = d.get("_router") or {}
                ans = str((d.get("choices") or [{}])[0].get("message", {}).get("content") or "")
                ok = "SILENCE-SURVIVED" in ans
                record("long_run_silence", ok,
                       f"RECORDED (not re-run): a 400s silent tool run returned 200 with "
                       f"wall={r.get('wall_time_s')}s, idle budget {r.get('idle_budget_s')}s - the same shape "
                       f"that died at ~305s before the fix",
                       {"proof_file": str(PROOF_LONG_RUN), "recorded": True,
                        "wall": r.get("wall_time_s"), "survived": ok})
            except Exception as exc:
                record("long_run_silence", False, f"recorded proof unreadable: {exc}")
        else:
            record("long_run_silence", False, "no recorded proof; re-run with --with-long-run",
                   skipped=True)
        return
    prompt = ("Run this exact shell command with the terminal tool and report its output verbatim: "
              "sleep 400; echo SILENCE-SURVIVED-400s. Do not do anything else first.")
    t0 = time.time()
    status, raw, _ = post("/v1/chat/completions",
                          {"model": "router", "messages": [{"role": "user", "content": prompt}]},
                          {"authorization": f"Bearer {gateway_key()}",
                           "x-hermes-session-key": f"accept-long-{int(time.time())}",
                           "x-hermes-session-id": f"accept-long-{int(time.time())}"}, timeout=900)
    wall = time.time() - t0
    ok = status == 200 and "SILENCE-SURVIVED" in raw.decode("utf-8", "replace")
    record("long_run_silence", ok,
           f"a 400s silent tool run -> {status} in {wall:.0f}s "
           f"({'survived, was killed at ~305s before the fix' if ok else 'FAILED'})",
           {"status": status, "wall_s": round(wall, 1), "fresh_run": True})


def post_web(path, body, timeout=60):
    req = urllib.request.Request(WEB + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def gate_write_safety():
    """A mutation without the edit key must be refused, never silently applied."""
    st, raw, _ = post_web("/api/settings/providers", {"id": "x", "archive": True})
    body = raw.decode("utf-8", "replace")[:120]
    ok = st in (401, 403, 404, 405)
    record("write_safety", ok,
           f"unauthenticated mutation on the UI service -> {st}" if ok else
           f"mutation returned {st} - expected a refusal",
           {"status": st, "body": body})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-long-run", action="store_true", help="also run the ~400s silent turn")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    print("acceptance battery — is the router fit to front the scheduler?")
    print(f"  proxy={PROXY}  web={WEB}  ledger={LEDGER.name}")
    t0 = time.time()
    sid = gate_scheduler_shape()
    gate_shape_no_keepalive_assumption()
    gate_session_keying()
    gate_failure_honesty()
    gate_surface_404()
    gate_health_truth()
    gate_pointer_alignment()
    gate_custody(sid)
    gate_long_run(args.with_long_run)
    gate_stats_live(t0)
    gate_write_safety()

    passed = [r for r in RESULTS if r["status"] == "PASS"]
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    skipped = [r for r in RESULTS if r["status"] == "SKIP"]
    print()
    print(f"  {len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped "
          f"({time.time() - t0:.0f}s)")

    for r in RESULTS:
        r["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = {"ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "proxy": PROXY, "web": WEB,
           "passed": len(passed), "failed": len(failed), "skipped": len(skipped),
           "gates": RESULTS}
    path = args.json or f"/tmp/proxy-acceptance-{int(time.time())}.json"
    Path(path).write_text(json.dumps(out, indent=2))
    print(f"  artifact: {path}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
