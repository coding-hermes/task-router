#!/usr/bin/env python3
"""Can the dashboard Bane describes be built from data we already have?
Join: registry (models/prices/context) x health-state (up-down/latency) x averages (tokens/cost/wall)."""
import json, os, collections

TR = os.path.dirname(os.path.abspath(__file__)) + '/..'
TR = os.path.abspath(TR)

# 1. registry
reg = {}
for line in open(os.path.join(TR, 'data/tables/models.jsonl'), errors='replace'):
    if line.strip():
        try:
            m = json.loads(line)
        except Exception:
            continue
        reg[(m.get('provider'), m.get('model'))] = m

# 2. health state (per provider + per model, with latency_ms)
hs_path = os.path.expanduser('~/.hermes/model-router/health-state.json')
hs = {}
prov_status = {}
if os.path.exists(hs_path):
    d = json.load(open(hs_path, encoding='utf-8', errors='replace'))
    provs = d.get('providers') or d
    for p, v in provs.items():
        if not isinstance(v, dict):
            continue
        prov_status[p] = (v.get('status'), v.get('latency_ms'), v.get('error'))
        for mname, mv in (v.get('models') or {}).items():
            if isinstance(mv, dict):
                hs[(p, mname)] = mv

# 3. rolling averages (tokens / cost / wall time per provider+model)
avg = collections.defaultdict(dict)
ap = os.path.join(TR, 'data/state/outcomes-averages.jsonl')
n_avg = 0
if os.path.exists(ap):
    for line in open(ap, errors='replace'):
        if not line.strip().startswith('{'):
            continue
        try:
            a = json.loads(line)
        except Exception:
            continue
        n_avg += 1
        avg[(a.get('provider'), a.get('model'))] = a

print('sources: registry=%d lanes | health entries=%d provider rows=%d | averages=%d' % (
    len(reg), len(hs), len(prov_status), n_avg))
print()

# join and rank by tokens actually used in the last 168h
rows = []
for (p, m), meta in reg.items():
    a = avg.get((p, m)) or {}
    h = hs.get((p, m)) or {}
    ps = prov_status.get(p) or (None, None, None)
    tok = a.get('avg_tokens_total_168h')
    rows.append({
        'provider': p, 'model': m,
        'up': h.get('status') or ps[0],
        'latency_ms': h.get('latency_ms') if h.get('latency_ms') is not None else ps[1],
        'tokens_168h': tok, 'samples': a.get('n_samples'),
        'wall_s': a.get('avg_wall_time_168h'), 'cost': a.get('avg_cost_task_168h'),
        'price': meta.get('public_price'), 'ctx': meta.get('context_limit'),
    })

scored = [r for r in rows if isinstance(r['tokens_168h'], (int, float))]
scored.sort(key=lambda r: -r['tokens_168h'])
print('lanes with measured token usage: %d of %d' % (len(scored), len(rows)))
print()
print('  %-13s %-30s %-7s %8s %10s %7s %7s %8s' % ('provider', 'model', 'up', 'lat_ms', 'tok_168h', 'samples', 'wall_s', 'price'))
for r in scored[:14]:
    print('  %-13s %-30s %-7s %8s %10s %7s %7s %8s' % (
        str(r['provider'])[:13], str(r['model'])[:30], str(r['up'])[:7],
        r['latency_ms'] if r['latency_ms'] is not None else '-',
        int(r['tokens_168h']) if r['tokens_168h'] else '-', r['samples'] or '-',
        round(r['wall_s'], 1) if isinstance(r['wall_s'], (int, float)) else '-',
        r['price'] if r['price'] is not None else '-'))

print()
print('per-provider rollup (what a provider panel would show):')
agg = collections.defaultdict(lambda: {'lanes': 0, 'up': 0, 'down': 0, 'lat': [], 'tok': 0, 'samples': 0})
for r in rows:
    a = agg[r['provider']]
    a['lanes'] += 1
    if str(r['up']).upper() == 'OK':
        a['up'] += 1
    elif str(r['up']).upper() in ('DOWN', 'SLOW', 'DISABLED'):
        a['down'] += 1
    if isinstance(r['latency_ms'], (int, float)):
        a['lat'].append(r['latency_ms'])
    if isinstance(r['tokens_168h'], (int, float)):
        a['tok'] += r['tokens_168h']
        a['samples'] += r['samples'] or 0
for p, a in sorted(agg.items(), key=lambda kv: -kv[1]['tok'])[:10]:
    lat = round(sum(a['lat']) / len(a['lat'])) if a['lat'] else '-'
    print('  %-15s lanes=%-4d up=%-4d down=%-4d avg_latency=%-7s tok_168h=%-10s samples=%d' % (
        p[:15], a['lanes'], a['up'], a['down'], lat, int(a['tok']) if a['tok'] else '-', a['samples']))
