#!/usr/bin/env python3
"""dummy_scheduler — a stand-in scheduler that speaks the REAL call shape, so a repoint can
be tested without the live fleet on the other end.

Why this exists: repointing the live scheduler at the router put ~332 lanes' tick traffic
through the router, and the router's own rating step is a *gateway* model call carrying the
full Hermes context (~46k input tokens). That doubled upstream model calls per tick on top of
hop retries; the gateway and the providers saturated, provider-level circuits tripped
(api_down), and the fleet stopped answering. A test harness that can send 1..N of the exact
same requests — with concurrency and pacing under OUR control — measures that amplification
before a live flip, instead of discovering it in production.

Shape it reproduces (from internal/scheduler/gateway_stream.go SendResponseStream):
  POST {base}/v1/responses
  Authorization: Bearer <key>          <- never on the command line, never printed
  X-Hermes-Session-Key: <tick id>      <- the scheduler's session key
  body: {"model":..., "provider":..., "input":[...], "stream": true}
and it reads SSE frames until a terminal event, counting time-to-first-byte and wall time.

Every request writes ONE raw JSONL entry (the "raw entries" the operator wants to query
later), and the run ends with a summary: HTTP statuses, wall times, and — measured from the
router's own ledger — how many upstream model calls ONE proxied request actually caused.

Usage:
  dummy_scheduler.py --n 3
  dummy_scheduler.py --n 6 --concurrency 2 --prompt-chars 1200
  dummy_scheduler.py --n 1 --base http://127.0.0.1:9391     # through the router
  dummy_scheduler.py --n 1 --base http://127.0.0.1:8642     # direct (control) 
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

LEDGER = Path('/home/kara/task-router/data/state/outcomes.jsonl')
RAW_OUT = Path('/home/kara/task-router/data/state/dummy-scheduler-runs.jsonl')

# A foreman-ish prompt: real ticks are thousands of chars. Kept configurable so the harness can
# reproduce both a trivial probe and a realistic tick without me guessing.
DEFAULT_PROMPT = (
    "You are the dummy-scheduler harness ticking project <proj>. Load skills dummy-foreman, "
    "read the board, pick the highest-priority open row, implement it, run the guard, commit.\n"
)


def read_secret(name):
    """Read a credential from ~/.hermes/.env by NAME. The value is never printed."""
    env_local = Path.home() / '.hermes' / '.env'
    if not env_local.exists():
        return ''
    for line in env_local.read_text(errors='replace').splitlines():
        line = line.strip()
        if line.startswith(name + '='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def ledger_count():
    if not LEDGER.exists():
        return 0
    n = 0
    with LEDGER.open(errors='replace') as f:
        for line in f:
            if '"session_id": "router-proxy' in line:
                n += 1
    return n


def one_request(idx, base, key, prompt, model, provider, timeout, results, lock, stop):
    if stop.is_set():
        return
    tick = f'dummy-scheduler-{int(time.time())}-{idx}'
    body = json.dumps({
        'model': model,
        'provider': provider,
        'input': [{'role': 'user', 'content': prompt}],
        'stream': True,
    }).encode()
    req = urllib.request.Request(
        base.rstrip('/') + '/v1/responses', data=body, method='POST',
        headers={'content-type': 'application/json', 'authorization': f'Bearer {key}',
                 'X-Hermes-Session-Key': tick, 'accept': 'text/event-stream'})
    rec = {'ts': time.time(), 'idx': idx, 'tick': tick, 'base': base, 'model': model,
           'provider': provider, 'prompt_chars': len(prompt)}
    t0 = time.time()
    frames, first_byte = [], None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rec['status'] = r.status
            rec['content_type'] = r.headers.get('content-type')
            rec['session_id_echo'] = r.headers.get('x-hermes-session-id')
            buf = b''
            while True:
                chunk = r.read(256)
                if not chunk:
                    break
                if first_byte is None:
                    first_byte = time.time() - t0
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    s = line.decode('utf-8', 'replace').strip()
                    if s.startswith('event:'):
                        frames.append(s.split(':', 1)[1].strip())
                    elif s.startswith('data:') and len(frames) < 60 and s[5:].strip() not in ('[DONE]', ''):
                        try:
                            d = json.loads(s[5:])
                            if d.get('type') == 'response.completed':
                                resp = d.get('response') or {}
                                rec['output_text'] = str(resp.get('output_text') or '')[:400]
                                rec['usage'] = resp.get('usage')
                        except Exception:
                            pass
    except urllib.error.HTTPError as e:
        rec['status'] = e.code
        rec['error'] = e.read()[:300].decode('utf-8', 'replace')
    except Exception as e:
        rec['status'] = None
        rec['error'] = str(e)[:200]
    rec['wall_s'] = round(time.time() - t0, 3)
    rec['ttfb_s'] = round(first_byte, 3) if first_byte is not None else None
    rec['frames'] = frames[:12]
    rec['frame_count'] = len(frames)
    with lock:
        results.append(rec)
        RAW_OUT.parent.mkdir(parents=True, exist_ok=True)
        with RAW_OUT.open('a') as f:          # one raw entry per request, queryable later
            f.write(json.dumps(rec) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=3, help='how many requests to send')
    ap.add_argument('--concurrency', type=int, default=1)
    ap.add_argument('--base', default='http://127.0.0.1:9391')
    ap.add_argument('--model', default='z-ai/glm-5.3-flash')
    ap.add_argument('--provider', default='xkiro')
    ap.add_argument('--prompt-chars', type=int, default=400)
    ap.add_argument('--timeout', type=float, default=300.0)
    ap.add_argument('--label', default='')
    args = ap.parse_args()

    key = read_secret('API_SERVER_KEY') or os.environ.get('HERMES_API_KEY', '')
    if not key:
        print('no API key found by name (API_SERVER_KEY) — refuse to guess'); return 2

    prompt = DEFAULT_PROMPT
    while len(prompt) < args.prompt_chars:
        prompt += f'board row DUMMY-{len(prompt)}: fix it.\n'

    before = ledger_count()
    load_before = open('/proc/loadavg').read().split()[:3]
    print(f'dummy scheduler -> {args.base} | n={args.n} concurrency={args.concurrency} '
          f'| prompt {len(prompt)} chars | model {args.model} provider {args.provider}')
    print(f'  ledger proxy rows before: {before} | loadavg {load_before}')

    results, lock, stop = [], threading.Lock(), threading.Event()
    t0 = time.time()
    threads = []
    for i in range(args.n):
        while sum(1 for t in threads if t.is_alive()) >= max(1, args.concurrency):
            time.sleep(0.2)
        t = threading.Thread(target=one_request, args=(i + 1, args.base, key, prompt, args.model,
                                                       args.provider, args.timeout, results, lock, stop))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    wall = time.time() - t0

    after = ledger_count()
    print()
    print(f'  completed {len(results)} requests in {wall:.1f}s')
    ok = [r for r in results if r.get('status') == 200]
    print(f'  HTTP 200: {len(ok)}/{len(results)} | statuses: '
          f'{sorted({str(r.get("status")) for r in results})}')
    if ok:
        print(f'  wall time  min {min(r["wall_s"] for r in ok):.1f}s  '
              f'median {statistics.median(r["wall_s"] for r in ok):.1f}s  '
              f'max {max(r["wall_s"] for r in ok):.1f}s')
        ttfb = [r['ttfb_s'] for r in ok if r.get('ttfb_s') is not None]
        if ttfb:
            print(f'  first byte median {statistics.median(ttfb):.3f}s')
    for r in results:
        if r.get('status') != 200:
            print(f'    FAIL {r["idx"]}: status={r.get("status")} {str(r.get("error"))[:150]}')
    print()
    print(f'  ledger proxy rows after : {after}  (delta {after - before})')
    print(f'  AMPLIFICATION: {args.base.rstrip("/")} caused {after - before} ledger row(s) '
          f'for {len(results)} request(s) -> {(after - before) / max(1, len(results)):.2f} per request')
    print(f'  raw entries appended to: {RAW_OUT}')
    load_after = open('/proc/loadavg').read().split()[:3]
    print(f'  loadavg after: {load_after}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
