#!/usr/bin/env python3
"""Provider-health dashboard builder.

Reads the hourly probe's data of record and emits a dense, self-contained,
mobile-first dark dashboard (no CDN, no JS deps) plus CSV/JSON:

  in : ~/.hermes/model-router/health.jsonl        (append-only snapshot history)
       ~/.hermes/model-router/health-state.json   (latest snapshot, rich)
       ~/task-router/data/tables/models.jsonl     (registry: price/context/disabled)
       ~/task-router/data/state/outcomes-averages.jsonl (token usage windows)
  out: ~/.hermes/dashboards/provider-health/
         index.html   (self-contained, filterable)
         latest.csv   (flattened latest snapshot, all lanes)
         latest.json  (machine-readable summary)

Null doctrine: every missing value carries a stated reason
('not probed', 'no sample in 24h window', 'disabled'), never a blank/zero.
Never fabricates: absent inputs reduce the page, they do not invent numbers.

Usage: provider_health_dashboard.py [--out DIR] [--quiet]
"""
import argparse, csv, datetime as dt, json, os, statistics, sys, html

HOME = os.path.expanduser('~')
MR = os.environ.get('ROUTER_STATE_DIR', f'{HOME}/.hermes/model-router')
HEALTH_JSONL = f'{MR}/health.jsonl'
HEALTH_STATE = f'{MR}/health-state.json'
TR = f'{HOME}/task-router'
REGISTRY = f'{TR}/data/tables/models.jsonl'
AVERAGES = f'{TR}/data/state/outcomes-averages.jsonl'
DEFAULT_OUT = f'{HOME}/.hermes/dashboards/provider-health'

WINDOWS = (24, 72, 168)


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def load_history(path):
    """-> (snapshots, parse_errors). One snapshot per hourly run."""
    snaps, bad = [], 0
    if not os.path.exists(path):
        return snaps, 1
    with open(path, errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                bad += 1
                continue
            ts = parse_ts(d.get('ts'))
            if ts is None:
                bad += 1
                continue
            snaps.append({'ts': ts, 'providers': d.get('providers') or {}})
    snaps.sort(key=lambda s: s['ts'])
    return snaps, bad


def provider_status(snap_providers, name):
    v = snap_providers.get(name) or {}
    return (v.get('status') or 'UNPROBED'), v


def history_rollup(snaps, names):
    """Per provider: uptime over windows, last transitions, latency series."""
    now = snaps[-1]['ts'] if snaps else None
    roll = {}
    for n in names:
        rec = {'n': 0, 'up': 0, 'down': 0, 'other': 0, 'transitions': [],
               'lat_series': [], 'windows': {}}
        prev = None
        for s in snaps:
            st, v = provider_status(s['providers'], n)
            rec['n'] += 1
            if st == 'OK':
                rec['up'] += 1
            elif st == 'DOWN':
                rec['down'] += 1
            else:
                rec['other'] += 1
            if prev is not None and st != prev:
                rec['transitions'].append({'ts': s['ts'], 'from': prev, 'to': st})
            prev = st
            lm = v.get('latency_ms')
            if isinstance(lm, (int, float)):
                rec['lat_series'].append({'ts': s['ts'], 'ms': lm})
        if now:
            for w in WINDOWS:
                cut = now - dt.timedelta(hours=w)
                sub = [s for s in snaps if s['ts'] >= cut]
                up = sum(1 for s in sub if provider_status(s['providers'], n)[0] == 'OK')
                rec['windows'][w] = {'n': len(sub), 'up': up,
                                     'pct': round(100.0 * up / len(sub), 1) if sub else None}
        roll[n] = rec
    return roll


def load_registry():
    """(provider, model) -> registry row. Registry-only lanes are kept."""
    reg = {}
    for r in read_jsonl(REGISTRY):
        key = (r.get('provider'), r.get('model'))
        if key[0] and key[1]:
            reg[key] = r
    return reg


def load_usage():
    """(provider, model) -> {window: row} from outcomes-averages."""
    use = {}
    for r in read_jsonl(AVERAGES):
        key = (r.get('provider'), r.get('model'))
        if not (key[0] and key[1]):
            continue
        # prefer the hermes source when duplicates exist
        cur = use.setdefault(key, {})
        if r.get('source_system') == 'hermes' or not cur:
            cur.update(r)
    return use


def usd(v):
    if not isinstance(v, (int, float)):
        return None
    return f'${v:,.2f}' if v >= 1 else f'${v:,.4f}'


def tok(v):
    if not isinstance(v, (int, float)):
        return None
    if v >= 1e9:
        return f'{v/1e9:.2f}B'
    if v >= 1e6:
        return f'{v/1e6:.2f}M'
    if v >= 1e3:
        return f'{v/1e3:.1f}k'
    return f'{v:,.0f}'


def esc(s):
    return html.escape(str(s), quote=True)


def spark(vals, width=120, height=18):
    """Inline SVG sparkline; None when no data."""
    if len(vals) < 2:
        return ''
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = width / (len(vals) - 1)
    pts = ' '.join(f'{i*step:.1f},{height - (v-lo)/span*height:.1f}'
                   for i, v in enumerate(vals))
    return (f'<svg class="sp" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}"><polyline points="{pts}"/></svg>')


def bar(pct, cls=''):
    if pct is None:
        return '<span class="na" title="no probe samples in window">n/a</span>'
    return (f'<span class="barwrap" title="{pct}% up">'
            f'<span class="bar {cls}" style="width:{max(2, pct):.0f}%"></span></span>'
            f'<span class="pct">{pct}%</span>')


def build(out_dir, quiet=False):
    snaps, bad = load_history(HEALTH_JSONL)
    state = {}
    if os.path.exists(HEALTH_STATE):
        try:
            state = json.load(open(HEALTH_STATE, errors='replace'))
        except Exception:
            state = {}
    latest_provs = state.get('providers') or {}
    if not latest_provs and snaps:
        latest_provs = snaps[-1]['providers']
    reg = load_registry()
    usage = load_usage()

    now_ts = parse_ts(state.get('ts')) or (snaps[-1]['ts'] if snaps else None)
    gen_ts = dt.datetime.now(dt.timezone.utc)

    prov_names = sorted(set(list(latest_provs.keys()) + list(reg.keys() and
                              {p for p, _ in reg.keys()})))
    roll = history_rollup(snaps, prov_names) if snaps else {}

    # ---- lane rows: every registry lane + every probed model entry
    lanes = {}
    for (p, m), r in reg.items():
        lanes[(p, m)] = {'provider': p, 'model': m, 'reg': r}
    for p, pv in latest_provs.items():
        for m, mv in (pv.get('models') or {}).items():
            row = lanes.setdefault((p, m), {'provider': p, 'model': m, 'reg': None})
            row['health'] = mv
            row['prov_latency'] = pv.get('latency_ms')
            row['prov_status'] = pv.get('status')

    lane_rows = []
    for (p, m), row in lanes.items():
        h = row.get('health') or {}
        st = h.get('status') or row.get('prov_status') or 'UNPROBED'
        lat = h.get('latency_ms')
        lat_src = 'model' if isinstance(lat, (int, float)) else None
        if lat_src is None and isinstance(row.get('prov_latency'), (int, float)):
            lat, lat_src = row['prov_latency'], 'provider'
        r = row.get('reg') or {}
        u = usage.get((p, m)) or {}
        reason = None
        if h:
            pass
        elif r:
            reason = 'not probed (registry lane)'
        else:
            reason = 'no data'
        if r.get('disabled'):
            reason = 'disabled: ' + str(r.get('disabled_reason') or 'unspecified')
        tok24 = u.get('avg_tokens_total_24h')
        lane_rows.append({
            'provider': p, 'model': m, 'status': st,
            'latency_ms': lat if isinstance(lat, (int, float)) else None,
            'latency_src': lat_src,
            'error': (h.get('error') or '')[:200],
            'note': h.get('note') or '',
            'in_per_m': r.get('public_in_per_m'), 'out_per_m': r.get('public_out_per_m'),
            'context_limit': r.get('context_limit'), 'thinking': bool(r.get('thinking')),
            'disabled': bool(r.get('disabled')),
            'tokens_24h': tok24 if isinstance(tok24, (int, float)) else None,
            'samples_24h': u.get('samples_24h') or u.get('n_24h'),
            'cost_24h': u.get('avg_cost_task_24h'),
            'reason': reason,
        })

    # ---- provider summary rows
    prov_rows = []
    for p in prov_names:
        pv = latest_provs.get(p) or {}
        models = pv.get('models') or {}
        up = sum(1 for v in models.values() if (v or {}).get('status') == 'OK')
        down = sum(1 for v in models.values() if (v or {}).get('status') == 'DOWN')
        rl = roll.get(p) or {}
        trs = rl.get('transitions') or []
        prov_rows.append({
            'provider': p, 'status': pv.get('status') or 'UNPROBED',
            'latency_ms': pv.get('latency_ms'), 'models': len(models),
            'up': up, 'down': down, 'error': (pv.get('error') or '')[:300],
            'w24': (rl.get('windows') or {}).get(24), 'n_snaps': rl.get('n'),
            'last_transition': trs[-1] if trs else None,
            'transitions_24h': [t for t in trs
                                if now_ts and t['ts'] >= now_ts - dt.timedelta(hours=24)],
            'lat_series': [x['ms'] for x in (rl.get('lat_series') or [])][-24:],
        })

    # ---- summary
    def count_status(rows, st):
        return sum(1 for r in rows if r['status'] == st)

    transitions_24h = []
    for pr in prov_rows:
        for t in pr['transitions_24h']:
            transitions_24h.append({'provider': pr['provider'], **t})
    transitions_24h.sort(key=lambda t: t['ts'])

    summary = {
        'generated_at': gen_ts.isoformat(timespec='seconds'),
        'probe_ts': now_ts.isoformat(timespec='seconds') if now_ts else None,
        'probe_version': state.get('probe_version'),
        'snapshots': len(snaps), 'snapshot_parse_errors': bad,
        # 'providers' = providers actually covered by the probe run (the report's own set);
        # registry-only providers are counted separately, never silently mixed in.
        'providers': len([pr for pr in prov_rows if pr['provider'] in latest_provs]),
        'providers_registry_only': len([pr for pr in prov_rows
                                        if pr['provider'] not in latest_provs]),
        'providers_up': count_status([pr for pr in prov_rows
                                      if pr['provider'] in latest_provs], 'OK'),
        'providers_down': count_status([pr for pr in prov_rows
                                        if pr['provider'] in latest_provs], 'DOWN'),
        'providers_disabled': sum(1 for pr in prov_rows
                                  if pr['provider'] in latest_provs
                                  and pr['status'] == 'DISABLED'),
        'lanes_total': len(lane_rows),
        'lanes_probed': sum(1 for r in lane_rows if r['status'] != 'UNPROBED'),
        'lanes_up': count_status(lane_rows, 'OK'),
        'lanes_down': count_status(lane_rows, 'DOWN'),
        'lanes_slow': sum(1 for r in lane_rows if r['status'] == 'SLOW'),
        'transitions_24h': len(transitions_24h),
        'history_from': snaps[0]['ts'].isoformat(timespec='seconds') if snaps else None,
        'history_to': snaps[-1]['ts'].isoformat(timespec='seconds') if snaps else None,
    }

    os.makedirs(out_dir, exist_ok=True)

    def _j(o):
        return o.isoformat(timespec='seconds') if isinstance(o, dt.datetime) else str(o)

    with open(os.path.join(out_dir, 'latest.json'), 'w') as fh:
        json.dump({'summary': summary, 'providers': prov_rows,
                   'lanes': lane_rows, 'transitions_24h': transitions_24h}, fh,
                  indent=1, default=_j)

    with open(os.path.join(out_dir, 'latest.csv'), 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['provider', 'model', 'status', 'latency_ms', 'latency_src',
                    'in_per_m', 'out_per_m', 'context_limit', 'thinking', 'disabled',
                    'tokens_24h', 'cost_24h', 'reason', 'error'])
        for r in sorted(lane_rows, key=lambda x: (x['provider'], x['model'])):
            w.writerow([r['provider'], r['model'], r['status'], r['latency_ms'] or '',
                        r['latency_src'] or '', r['in_per_m'] if r['in_per_m'] is not None else '',
                        r['out_per_m'] if r['out_per_m'] is not None else '',
                        r['context_limit'] if r['context_limit'] is not None else '',
                        int(r['thinking']), int(r['disabled']),
                        int(r['tokens_24h']) if r['tokens_24h'] else '',
                        round(r['cost_24h'], 4) if isinstance(r['cost_24h'], (int, float)) else '',
                        r['reason'] or '', r['error']])

    html_doc = render(summary, prov_rows, lane_rows, transitions_24h)
    path = os.path.join(out_dir, 'index.html')
    with open(path, 'w') as fh:
        fh.write(html_doc)
    if not quiet:
        print(f'dashboard: {path} ({len(html_doc)//1024} KB) · providers={summary["providers"]} '
              f'up={summary["providers_up"]} down={summary["providers_down"]} · '
              f'lanes={summary["lanes_total"]} probed={summary["lanes_probed"]} '
              f'up={summary["lanes_up"]} down={summary["lanes_down"]} · '
              f'transitions24h={summary["transitions_24h"]} · snapshots={summary["snapshots"]}')
    return summary


CSS = """
:root{--bg:#0d1117;--panel:#161b22;--line:#262d36;--fg:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--down:#f85149;--warn:#d29922;--off:#6e7681;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{padding:14px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;
background:linear-gradient(180deg,#0d1117f2,#0d1117e0);backdrop-filter:blur(6px);z-index:5}
h1{margin:0 0 4px;font-size:16px}
h2{margin:22px 16px 8px;font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
.meta{color:var(--dim);font-size:12px}
.kpis{display:flex;flex-wrap:wrap;gap:8px;padding:10px 16px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 12px;min-width:96px}
.kpi b{display:block;font-size:18px}
.kpi span{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.wrap{overflow-x:auto;padding:0 16px 8px}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--dim);font-weight:600;position:sticky;top:62px;background:#0d1117f7;z-index:2}
td.err,td.mono{white-space:normal;max-width:420px;color:var(--dim);font-size:11px}
tr:hover td{background:#1c2430}
.ok{color:var(--ok)}.down{color:var(--down)}.warn{color:var(--warn)}
.off{color:var(--off)}.acc{color:var(--acc)}
.pill{border:1px solid var(--line);border-radius:10px;padding:1px 7px;font-size:11px}
.barwrap{display:inline-block;width:70px;height:8px;background:#21262d;border-radius:4px;overflow:hidden;vertical-align:middle}
.bar{display:block;height:100%;background:var(--ok)}
.bar.low{background:var(--warn)}.bar.bad{background:var(--down)}
.pct{margin-left:6px;color:var(--dim);font-size:11px}
.na{color:var(--off);font-size:11px}
.sp{vertical-align:middle}
.sp polyline{fill:none;stroke:var(--acc);stroke-width:1.3}
input[type=search]{background:var(--panel);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:6px 10px;width:min(420px,90vw);font:inherit}
.controls{padding:0 16px 6px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.tl{border-left:2px solid var(--line);margin:0 16px 8px;padding-left:12px}
.tl div{margin:3px 0;font-size:12px}
footer{color:var(--dim);font-size:11px;padding:16px;border-top:1px solid var(--line)}
a{color:var(--acc)}
"""

JS = """
function flt(id, inp){var q=document.getElementById(inp).value.toLowerCase();
var hideDis=document.getElementById('hidedis').checked;
var rows=document.querySelectorAll('#'+id+' tbody tr');var n=0;
rows.forEach(function(r){var hit=r.textContent.toLowerCase().indexOf(q)>=0;
if(hideDis&&r.getAttribute('data-dis')==='1')hit=false;
r.style.display=hit?'':'none';if(hit)n++;});
document.getElementById(id+'-n').textContent=n+' rows';}
function tgl(){flt('lanes','lq');}
"""


def render(s, prov_rows, lane_rows, transitions):
    sev = {'DOWN': 0, 'SLOW': 2, 'THINKING': 2, 'OK': 1, 'DISABLED': 3, 'UNPROBED': 4}
    prov_sorted = sorted(prov_rows, key=lambda r: (sev.get(r['status'], 5), r['provider']))
    lane_sorted = sorted(lane_rows, key=lambda r: (sev.get(r['status'], 5),
                                                   not r['disabled'], r['provider'], r['model']))

    def cls(st):
        return {'OK': 'ok', 'DOWN': 'down', 'SLOW': 'warn', 'THINKING': 'warn',
                'DISABLED': 'off', 'UNPROBED': 'off', 'EXCLUDED': 'off'}.get(st, '')

    age = ''
    if s['probe_ts']:
        try:
            d = dt.datetime.fromisoformat(s['probe_ts'])
            mins = int((dt.datetime.now(dt.timezone.utc) - d).total_seconds() // 60)
            age = f'{mins} min ago'
        except Exception:
            age = ''

    kpis = [
        (f"{s['providers_up']}/{s['providers']}", 'providers up', 'ok'),
        (str(s['providers_down']), 'providers down', 'down' if s['providers_down'] else ''),
        (str(s['providers_disabled']), 'disabled', 'off'),
        (str(s.get('providers_registry_only') or 0), 'registry-only', 'off'),
        (f"{s['lanes_up']}", 'lanes up', 'ok'),
        (str(s['lanes_down']), 'lanes down', 'down' if s['lanes_down'] else ''),
        (f"{s['lanes_probed']}/{s['lanes_total']}", 'lanes probed', ''),
        (str(s['transitions_24h']), 'transitions 24h', 'warn' if s['transitions_24h'] else ''),
        (str(s['snapshots']), 'snapshots', ''),
    ]
    kpi_html = ''.join(
        f'<div class="kpi"><b class="{c}">{esc(v)}</b><span>{esc(l)}</span></div>'
        for v, l, c in kpis)

    tl = ''.join(
        f'<div><span class="meta">{esc(t["ts"].strftime("%m-%d %H:%M"))}</span> '
        f'<b>{esc(t["provider"])}</b> '
        f'<span class="{cls(t["from"])}">{esc(t["from"])}</span> &rarr; '
        f'<span class="{cls(t["to"])}">{esc(t["to"])}</span></div>'
        for t in reversed(transitions)) or '<div class="meta">none in the last 24h</div>'

    def prow(r):
        w = r['w24'] or {}
        pct = w.get('pct')
        bcls = 'low' if (pct is not None and pct < 95) else ''
        bcls = 'bad' if (pct is not None and pct < 60) else bcls
        lt = r['last_transition']
        lt_s = (f'{lt["from"]}&rarr;{lt["to"]} @ {lt["ts"].strftime("%m-%d %H:%M")}'
                if lt else '<span class="na">none in window</span>')
        lat = f'{r["latency_ms"]} ms' if isinstance(r['latency_ms'], (int, float)) else '<span class="na">n/a</span>'
        err = esc(r['error']) if r['error'] else '<span class="na">—</span>'
        return (f'<tr><td class="acc">{esc(r["provider"])}</td>'
                f'<td class="{cls(r["status"])}">{esc(r["status"])}</td>'
                f'<td>{lat}</td>'
                f'<td>{r["up"]}/{r["models"] or "—"}</td>'
                f'<td>{r["down"] or ""}</td>'
                f'<td>{bar(pct, bcls)}</td>'
                f'<td>{spark(r["lat_series"])}</td>'
                f'<td class="meta">{lt_s}</td>'
                f'<td class="err">{err}</td></tr>')

    def lrow(r):
        lat = (f'{r["latency_ms"]} ms <span class="meta">({r["latency_src"]})</span>'
               if r['latency_ms'] is not None else '<span class="na">n/a</span>')
        pr = (f'{usd(r["in_per_m"])}/{usd(r["out_per_m"])}'
              if r['in_per_m'] is not None or r['out_per_m'] is not None
              else '<span class="na">no price</span>')
        tk = tok(r['tokens_24h']) if r['tokens_24h'] else '<span class="na">no sample in 24h window</span>'
        flags = ''.join(x for x in ('<span class="pill">think</span>' if r['thinking'] else '',
                                    '<span class="pill off">disabled</span>' if r['disabled'] else ''))
        note = esc(r['error'] or r['note'] or r['reason'] or '')
        return (f'<tr data-dis="{1 if r["disabled"] else 0}">'
                f'<td class="acc">{esc(r["provider"])}</td><td>{esc(r["model"])}</td>'
                f'<td class="{cls(r["status"])}">{esc(r["status"])}</td>'
                f'<td>{lat}</td><td>{pr}</td><td>{tk}</td>'
                f'<td>{esc(r["context_limit"]) if r["context_limit"] else "<span class=na>n/a</span>"}</td>'
                f'<td>{flags}</td><td class="err">{note}</td></tr>')

    down_first = [r for r in lane_sorted if r['status'] == 'DOWN']
    prov_hdr = f"providers ({s['providers']} probed" + (
        f" + {s['providers_registry_only']} registry-only" if s.get('providers_registry_only') else '') + ')'
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>provider health — task-router</title><style>{CSS}</style></head><body onload="tgl()">
<header>
<h1>provider health</h1>
<div class="meta">probe <b>{esc(s['probe_ts'] or 'unknown')}</b> ({esc(age)}) ·
v{esc(s['probe_version'] or '?')} · history {esc((s['history_from'] or '?')[:10])} → {esc((s['history_to'] or '?')[:10])}
({s['snapshots']} snapshots{f", {s['snapshot_parse_errors']} unparsed" if s['snapshot_parse_errors'] else ''}) ·
built {esc(s['generated_at'])}</div>
<div style="margin-top:8px"><a href="latest.json">latest.json</a> · <a href="latest.csv">latest.csv</a>
<span class="meta"> (CSV = every lane, one row, null reasons included)</span></div>
</header>
<div class="kpis">{kpi_html}</div>

<h2>transitions — last 24h</h2>
<div class="tl">{tl}</div>

<h2>{prov_hdr}</h2>
<div class="wrap"><table><thead><tr><th>provider</th><th>status</th><th>latency</th>
<th>lanes up</th><th>down</th><th>uptime 24h</th><th>latency (24 probes)</th>
<th>last transition (window)</th><th>last error / note</th></tr></thead>
<tbody>{''.join(prow(r) for r in prov_sorted)}</tbody></table></div>

<h2>lanes ({len(lane_rows)}) — {len(down_first)} down</h2>
<div class="controls">
<input type="search" id="lq" placeholder="filter lanes (provider, model, status, error)…"
 oninput="flt('lanes','lq')">
<label class="meta"><input type="checkbox" id="hidedis" checked onchange="tgl()"> hide disabled lanes</label>
<span class="meta" id="lanes-n">{len(lane_rows)} rows</span></div>
<div class="wrap"><table id="lanes"><thead><tr><th>provider</th><th>model</th><th>status</th>
<th>latency</th><th>price in/out $/M</th><th>tokens 24h</th><th>ctx</th><th>flags</th>
<th>error / null reason</th></tr></thead><tbody>{''.join(lrow(r) for r in lane_sorted)}</tbody></table></div>

<footer>source: ~/.hermes/model-router/health.jsonl + health-state.json ·
registry: task-router/data/tables/models.jsonl · usage: outcomes-averages.jsonl ·
generated by ~/.hermes/scripts/provider_health_dashboard.py — data of record is the
probe's JSONL, this page is a view.</footer>
<script>{JS}</script></body></html>"""
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()
    build(a.out, a.quiet)
    return 0


if __name__ == '__main__':
    sys.exit(main())
