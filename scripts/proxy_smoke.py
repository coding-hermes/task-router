#!/usr/bin/env python3
"""proxy_smoke — small fake-traffic harness for the router proxy.

Purpose: generate controllable dummy traffic so the proxy can be tested BEFORE the live
scheduler is pointed at it. Zero deps (stdlib), two wire shapes:

  --shape chat       POST /v1/chat/completions   (JSON back, simplest)
  --shape responses  POST /v1/responses          (SSE — the shape the scheduler actually sends)

Every request writes one JSONL row to data/state/proxy-smoke.jsonl and the run prints a
summary: HTTP status histogram, latency, the distinct error bodies, and the router-ledger
delta proving whether a proxied row was written for each request.

  proxy_smoke.py --n 4                      # 4 requests, one at a time, through the proxy
  proxy_smoke.py --n 6 --concurrency 3      # concurrency probe
  proxy_smoke.py --n 2 --base http://127.0.0.1:8642   # control: straight to the gateway
"""
import argparse, json, os, statistics, threading, time, urllib.error, urllib.request
from pathlib import Path

OUT = Path('/home/kara/task-router/data/state/proxy-smoke.jsonl')
LEDGER = Path('/home/kara/task-router/data/state/outcomes.jsonl')
PROMPT = "Reply with exactly: SMOKE-OK"
_lock = threading.Lock()


def secret(name):
    """Read a credential from ~/.hermes/.env BY NAME; the value is never printed."""
    p = Path.home() / '.hermes' / '.env'
    if not p.exists():
        return ''
    for line in p.read_text(errors='replace').splitlines():
        if line.strip().startswith(name + '='):
            return line.strip().split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def ledger_rows():
    if not LEDGER.exists():
        return 0
    n = 0
    with LEDGER.open(errors='replace') as f:
        for line in f:
            if '"session_id": "router-proxy' in line:
                n += 1
    return n


def one(idx, args, key, out):
    tick = f"proxy-smoke-{args.label}-{int(time.time())}-{idx}"
    if args.shape == 'chat':
        url = args.base.rstrip('/') + '/v1/chat/completions'
        body = {"model": args.model, "messages": [{"role": "user", "content": args.prompt}]}
    else:
        url = args.base.rstrip('/') + '/v1/responses'
        body = {"model": args.model, "input": [{"role": "user", "content": args.prompt}],
                "stream": True}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                 "X-Hermes-Session-Key": tick, "accept": "text/event-stream"},
        method="POST")
    t0 = time.time()
    status, err, chars = None, None, 0
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            status = r.status
            if args.shape == 'responses':
                for raw in r:                      # SSE: read to the terminal event
                    chars += len(raw)
                    if b'response.completed' in raw or b'"status": "failed"' in raw:
                        break
            else:
                chars = len(r.read())
    except urllib.error.HTTPError as e:
        status = e.code
        err = (e.read() or b'')[:300].decode('utf-8', 'replace')
    except Exception as e:                          # noqa: BLE001 — a harness must not crash
        err = f'{type(e).__name__}: {e}'
    dt = round(time.time() - t0, 3)
    row = {"label": args.label, "tick": tick, "shape": args.shape, "base": args.base,
           "status": status, "seconds": dt, "body_chars": chars, "error": err}
    with _lock:
        out.append(row)
        print(f"  [{idx}] {status} {dt:6.2f}s {chars:6d}B  {tick}"
              + (f"  ERR: {err[:120]}" if err else ""))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=4)
    ap.add_argument('--concurrency', type=int, default=1)
    ap.add_argument('--base', default='http://127.0.0.1:9391')
    ap.add_argument('--shape', choices=['chat', 'responses'], default='chat')
    ap.add_argument('--model', default='router')
    ap.add_argument('--prompt', default=PROMPT)
    ap.add_argument('--timeout', type=float, default=180)
    ap.add_argument('--label', default='smoke')
    args = ap.parse_args()

    key = secret('API_SERVER_KEY') or secret('ROUTER_EDIT_API_KEY')
    if not key:
        print('no key found in ~/.hermes/.env (API_SERVER_KEY) — refusing to send unauthenticated')
        return 2
    before = ledger_rows()
    print(f"proxy_smoke: {args.n} x {args.shape} -> {args.base} (concurrency {args.concurrency})")
    out, t0 = [], time.time()
    threads = []
    for i in range(args.n):
        t = threading.Thread(target=one, args=(i + 1, args, key, out))
        t.start()
        threads.append(t)
        if len(threads) == args.concurrency:
            for t2 in threads:
                t2.join()
            threads = []
    for t in threads:
        t.join()
    wall = round(time.time() - t0, 1)

    ok = [r for r in out if r['status'] == 200]
    lat = sorted(r['seconds'] for r in out)
    print(f"\n  statuses: " + ", ".join(f"{s}x{n}" for s, n in
          sorted({r['status']: sum(1 for x in out if x['status'] == r['status']) for r in out}.items(),
                 key=lambda kv: str(kv[0]))))
    print(f"  served {len(ok)}/{len(out)} | wall {wall}s | latency min/med/max "
          f"{lat[0]}/{statistics.median(lat)}/{lat[-1]}s")
    errs = {}
    for r in out:
        if r['error']:
            errs[r['error'][:160]] = errs.get(r['error'][:160], 0) + 1
    for e, c in errs.items():
        print(f"  error x{c}: {e}")
    print(f"  ledger rows written by this run: {ledger_rows() - before} "
          f"(a served request should leave exactly one)")
    with OUT.open('a') as f:
        for r in out:
            f.write(json.dumps(r) + '\n')
    print(f"  raw entries appended: {OUT}")
    return 0 if len(ok) == len(out) else 1


if __name__ == '__main__':
    raise SystemExit(main())
