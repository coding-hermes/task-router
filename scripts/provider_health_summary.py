#!/usr/bin/env python3
"""One-screen summary of the latest provider-health probe + dashboard link.

Reads the data of record (health.jsonl snapshots) — never scrapes the probe's
text report — and prints at most a few lines for cron delivery:

  provider-health 2026-10-03 17:00Z — 24/28 providers up, 1 down · 99 lanes up / 51 down (156 probed)
  flips (this run): xkiro OK→DOWN, …
  down lanes: xkiro 10, xkiro-2 10, commandcode 9, … (+4 more)
  dashboard: http://karahermes-mde-7840hs-2.tail448ac.ts.net:9095/

Exit 0 always (a delivery line is more useful than a cron error).
"""
import collections, datetime as dt, json, os, sys

MR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))
HEALTH_JSONL = f'{MR}/health.jsonl'
DASH = os.path.expanduser('~/.hermes/dashboards/provider-health/latest.json')
URL = os.environ.get('PROVIDER_HEALTH_URL',
                     'http://karahermes-mde-7840hs-2.tail448ac.ts.net:9095/')


def load_last_two(path):
    prev = cur = None
    if not os.path.exists(path):
        return None, None
    with open(path, errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            prev, cur = cur, d
    return prev, cur


def main():
    prev, cur = load_last_two(HEALTH_JSONL)
    try:
        dash = json.load(open(DASH)) if os.path.exists(DASH) else {}
    except Exception:
        dash = {}
    s = dash.get('summary') or {}

    if not cur:
        print('provider-health: no probe data at all (health.jsonl missing/empty)')
        print(URL)
        return 0

    ts = (cur.get('ts') or '?')[:16].replace('T', ' ')
    provs = cur.get('providers') or {}

    lines = []
    if s:
        lines.append(f"provider-health {ts}Z — {s.get('providers_up')}/{s.get('providers')} providers up, "
                     f"{s.get('providers_down')} down · {s.get('lanes_up')} lanes up / "
                     f"{s.get('lanes_down')} down (of {s.get('lanes_probed')} probed)")
    else:
        lines.append(f'provider-health {ts}Z — dashboard data unavailable')

    # transitions in THIS run = last snapshot vs previous (what the probe alerts on)
    flips = []
    if prev:
        pp = prev.get('providers') or {}
        for name in sorted(set(pp) | set(provs)):
            a = (pp.get(name) or {}).get('status')
            b = (provs.get(name) or {}).get('status')
            if a and b and a != b:
                flips.append(f'{name} {a}\u2192{b}')
    if flips:
        lines.append('flips (this run): ' + ', '.join(flips[:8]) +
                     (f' (+{len(flips)-8} more)' if len(flips) > 8 else ''))

    # down lanes grouped by provider (counts only — the page has the detail)
    down = collections.Counter()
    for name, pv in provs.items():
        for _m, mv in ((pv or {}).get('models') or {}).items():
            if (mv or {}).get('status') == 'DOWN':
                down[name] += 1
    if down:
        top = ', '.join(f'{p} {n}' for p, n in down.most_common(6))
        more = len(down) - 6
        lines.append(f'down lanes: {top}' + (f' (+{more} more providers)' if more > 0 else ''))

    disabled = [n for n, pv in provs.items() if (pv or {}).get('status') == 'DISABLED']
    if disabled:
        lines.append('disabled providers: ' + ', '.join(sorted(disabled)))

    stale_min = None
    try:
        t = dt.datetime.fromisoformat((cur.get('ts') or '').replace('Z', '+00:00'))
        stale_min = int((dt.datetime.now(dt.timezone.utc) - t).total_seconds() // 60)
    except Exception:
        pass
    if stale_min is not None and stale_min > 180:
        lines.append(f'!! probe data is {stale_min} min old — probe may be failing')

    lines.append(f'dashboard: {URL}')
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
