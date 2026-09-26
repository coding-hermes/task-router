#!/usr/bin/env python3
"""NULL census across the registry tables: meaningful absences vs junk."""
import collections
import json
import os

T = '/home/kara/task-router/data/tables'

#: fields whose absence is MEANINGFUL by design (unknown != missing)
MEANINGFUL_KNOWN = {
    'valid_to', 'available_from', 'replaced_by', 'disabled_reason', 'archive',
    'public_cache_read_per_m', 'public_cache_write_per_m',   # NULL = provider does not publish it
    'normalized_price',                                        # NULL = unpriced (PAYG / no sticker)
    'public_price', 'public_in_per_m', 'public_out_per_m',
    'plan_tier',                                               # NULL = not plan-bucketed (PAYG)
    'token_factor', 'data_class', 'context_limit',
    'why', 'codes', 'notes', 'note', 'evidence', 'source_ref', 'level', 'min_perf',
    'avg_cost_task_24h', 'avg_wall_time_24h', 'avg_turns_24h', 'success_rate',
    'n_completed', 'n_success_known', 'complexity', 'complexity_sig',
}
#: fields that must exist on a LIVE lane or the lane is malformed
CORE = ('provider', 'model', 'context_limit', 'normalized_price', 'public_price')


def load(name):
    p = os.path.join(T, name)
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p, errors='replace'):
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def census(name):
    rows = load(name)
    if not rows:
        return None
    cols = collections.Counter()
    for r in rows:
        for k in r:
            if r[k] is None or r[k] == '':
                cols[k] += 1
    return rows, cols


print('=== null census per table (column: null rows / total) ===')
for fn in sorted(os.listdir(T)):
    if not fn.endswith('.jsonl'):
        continue
    c = census(fn)
    if not c:
        continue
    rows, cols = c
    n = len(rows)
    dead = [(k, v) for k, v in cols.items() if v == n]
    notable = [(k, v) for k, v in sorted(cols.items(), key=lambda kv: -kv[1])
               if 0 < v < n and k not in MEANINGFUL_KNOWN][:6]
    print('\n  %-28s rows=%-5d columns=%-3d 100%%-null columns: %s' % (
        fn, n, len(set(k for r in rows for k in r)), ', '.join(k for k, _ in dead) or 'none'))
    if notable:
        print('     notable nulls: ' + ' · '.join('%s %d (%.0f%%)' % (k, v, 100.0 * v / n) for k, v in notable))

print('\n=== live lanes missing a CORE field (enabled, not archived, not retired) ===')
models = load('models.jsonl')
live = [r for r in models if not r.get('archive') and not r.get('disabled')
        and not r.get('valid_to') and r.get('valid_from') is None or
        (not r.get('archive') and not r.get('disabled') and not r.get('valid_to'))]
live = [r for r in models if not r.get('archive') and not r.get('disabled') and not r.get('valid_to')]
print('  live lanes:', len(live))
missing = collections.Counter()
for r in live:
    for k in CORE:
        if r.get(k) is None:
            missing[k] += 1
for k, v in missing.most_common():
    print('    %-20s %d lanes missing (%.0f%% of live)' % (k, v, 100.0 * v / len(live)))

print('\n=== the known overlay-wipe shape: live lanes with MANY null fields at once ===')
probe = []
for r in live:
    nulls = sum(1 for v in r.values() if v is None)
    probe.append((nulls, len(r), r.get('provider'), r.get('model')))
probe.sort(reverse=True)
print('  top by null COUNT (nulls/columns):')
for nulls, total, prov, mod in probe[:8]:
    print('    %-34s %s/%s  (%s)' % ('%s/%s' % (prov, mod), nulls, total,
                                     'WIPED-SHAPE' if nulls >= total * 0.5 else 'ok'))

print('\n=== dates without provenance (must not exist) ===')
bad = [r for r in models if (r.get('valid_to') or r.get('available_from')) and not r.get('lifecycle_source')]
print('  rows with a date but NO lifecycle_source:', len(bad))

print('\n=== ledger: nulls by class (meaningful vs junk) ===')
store = '/home/kara/task-router/data/state/outcomes.jsonl'
cls = collections.defaultdict(lambda: collections.Counter())
with open(store, errors='replace') as fh:
    for line in fh:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get('source_system') != 'router-proxy':
            continue
        k = 'served' if d.get('success') is True else ('no-hops' if d.get('route_outcome') == 'no-hops' else 'other')
        cls[k]['rows'] += 1
        for f in ('cost_usd', 'price_basis', 'chain', 'session_id', 'tokens_used'):
            if d.get(f) is None:
                cls[k][f + '_null'] += 1
        if isinstance(d.get('cost_usd'), (int, float)) and d.get('cost_usd') == 0 and d.get('price_basis') is None:
            cls[k]['ZERO_cost_NO_basis'] += 1
for k, c in cls.items():
    n = c['rows']
    print('  %-8s rows=%-5d ' % (k, n) + ' · '.join('%s %d' % (kk.replace('_null', ''), vv) for kk, vv in c.items() if kk != 'rows'))
print('\n  ZERO-cost-with-no-basis (the junk class): %d' % sum(c['ZERO_cost_NO_basis'] for c in cls.values()))
