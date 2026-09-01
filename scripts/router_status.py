#!/usr/bin/env python3
"""router_status.py — one-command task-router overview (TR-030).

Answers "why is my head X / what state is routing in" without running five
tools: registry freshness, provider health/quota/circuit summary, in-flight
spawns, data-quality gap count, and a per-provider gate roll-up in one shot.

Read-only aggregator — every section is assembled from the same state the
resolver uses, and every sub-source degrades independently (fail-open, same
doctrine as router_spawn.py): a broken source gets `unavailable: true` plus
the error string, never a crash, never a fabricated value. Exit code is 0
even when every source is broken (it is a status tool, not a gate).

Sources (env-overridable for hermetic tests, matching the other tools):
  registry.json   — ROUTING_REGISTRY (else repo root); metadata generated_at,
                    else file mtime; data/tables fallback reported loudly.
  circuit-state.json — ROUTER_STATE_DIR (else ~/.hermes/model-router);
                    OPEN = open_until in the future (router_circuit.py rule).
  quota-state.json   — provider policy gates (GATED = blocked, reason).
  health-state.json  — probe v3 output (providers.<p>.status + .models).
  ledger.jsonl    — LEDGER_FILE (else ROUTER_STATE_DIR/ledger.jsonl); a trace
                    whose LAST row is outcome=started and younger than 30 min
                    is in flight (mirrors router_ledger.py / router_spawn.py).
  data/tables     — ROUTING_DATA_DIR; gap assessment reuses router_gaps logic.

CLI:
  router_status.py [--format json|text]
    --format json  pure machine-parseable JSON on stdout (the default,
                   matching router_spawn.py's default).
    --format text  compact aligned human table.
Exit: 0 always.
"""
import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
REGISTRY = os.environ.get('ROUTING_REGISTRY', os.path.join(_REPO, 'registry.json'))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
MR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))

# Same stale window as router_ledger.STALE_MS / router_spawn.STALE_MS — a
# 'started' row older than this is a crashed spawn and does not count.
STALE_MS = 30 * 60 * 1000


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _parse_utc(ts):
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _load_json(path):
    """json.load or None on any error (caller decides what 'absent' means)."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _ledger_path():
    """Same resolution order as router_ledger._ledger_path (LEDGER_FILE wins)."""
    env = os.environ.get('LEDGER_FILE')
    if env:
        return env
    return os.path.join(MR, 'ledger.jsonl')


# ------------------------------------------------------------- registry ----

def _count_models(tables):
    models = tables.get('models') or []
    active = [m for m in models
              if not m.get('archive') and m.get('valid_to') is None
              and not m.get('disabled')]
    priced = [m for m in active if m.get('normalized_price') is not None]
    return {
        'rows': len(models),
        'active': len(active),
        'priced': len(priced),
        'providers': len(tables.get('providers') or []),
    }


def registry_section():
    """registry.json freshness + shape; data/tables fallback stays visible."""
    path = REGISTRY
    doc = _load_json(path)
    if isinstance(doc, dict) and isinstance(doc.get('tables'), dict) and doc['tables']:
        gen = doc.get('generated_at')
        gen_src = 'metadata' if gen else None
        if not gen:
            try:
                gen = datetime.datetime.fromtimestamp(
                    os.path.getmtime(path), tz=datetime.timezone.utc
                ).isoformat(timespec='seconds')
                gen_src = 'file mtime'
            except Exception:
                gen = None
        return {
            'source': 'registry.json', 'path': path,
            'unavailable': False, 'error': None,
            'generated_at': gen, 'generated_at_source': gen_src,
            'engine': doc.get('source'),
            'fallback_used': False, 'warning': None,
            'counts': _count_models(doc['tables']),
        }
    # registry.json missing/corrupt/empty → the committed tables are the live
    # source; report that loudly (TR-025 visibility, same as router_spawn).
    if doc is None:
        err = 'registry.json missing or unreadable'
    elif not isinstance(doc, dict):
        err = 'registry.json present but not an object (corrupt)'
    else:
        err = 'registry.json present but empty tables key (corrupt or unseeded)'
    tables = {}
    try:
        for fn in sorted(os.listdir(DATA_DIR)):
            if fn.endswith('.jsonl'):
                rows = []
                with open(os.path.join(DATA_DIR, fn)) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows.append(json.loads(line))
                tables[fn[:-len('.jsonl')]] = rows
    except Exception:
        tables = {}
    return {
        'source': 'data/tables', 'path': DATA_DIR,
        'unavailable': True, 'error': err,
        'generated_at': None, 'generated_at_source': None,
        'engine': None,
        'fallback_used': True,
        'warning': f'{err} — reading committed data/tables fallback',
        'counts': _count_models(tables),
    }


# -------------------------------------------------------------- circuit ----

def circuit_section():
    path = os.path.join(MR, 'circuit-state.json')
    doc = _load_json(path)
    if doc is None:
        present = os.path.exists(path)
        return {'source': path, 'present': present, 'unavailable': present,
                'error': 'circuit-state.json unreadable (corrupt)' if present else None,
                'open': 0, 'cooling': 0, 'pairs': []}
    now = _now_iso()
    pairs = doc.get('pairs') if isinstance(doc, dict) else None
    if not isinstance(pairs, dict):
        pairs = {}
    out, cooling = [], []
    for key, c in sorted(pairs.items()):
        if not isinstance(c, dict):
            continue
        is_open = bool(c.get('open_until') and c['open_until'] > now)
        entry = {'pair': key, 'state': 'OPEN' if is_open else 'cooling',
                 'failures': c.get('failures', 0),
                 'open_until': c.get('open_until'),
                 'reason': c.get('reason', '')}
        (out if is_open else cooling).append(entry)
    return {'source': path, 'present': True, 'unavailable': False, 'error': None,
            'open': len(out), 'cooling': len(cooling),
            'pairs': out + cooling}


# ---------------------------------------------------------------- quota ----

def quota_section():
    path = os.path.join(MR, 'quota-state.json')
    doc = _load_json(path)
    if doc is None:
        present = os.path.exists(path)
        return {'source': path, 'present': present, 'unavailable': present,
                'error': 'quota-state.json unreadable (corrupt)' if present else None,
                'open': 0, 'gated': 0, 'gated_providers': []}
    provs = doc.get('providers') if isinstance(doc, dict) else None
    if not isinstance(provs, dict):
        provs = {}
    gated = []
    n_open = 0
    for pid, q in sorted(provs.items()):
        if not isinstance(q, dict):
            continue
        if q.get('status') == 'open':
            n_open += 1
        elif q.get('status'):  # gated / anything non-open is a visible block
            gated.append({'provider': pid, 'status': q.get('status'),
                          'reason': q.get('reason', '')})
    return {'source': path, 'present': True, 'unavailable': False, 'error': None,
            'open': n_open, 'gated': len(gated), 'gated_providers': gated}


# --------------------------------------------------------------- health ----

def health_section():
    path = os.path.join(MR, 'health-state.json')
    doc = _load_json(path)
    if doc is None:
        present = os.path.exists(path)
        return {'source': path, 'present': present, 'unavailable': present,
                'error': 'health-state.json unreadable (corrupt)' if present else None,
                'updated': None, 'ok': 0, 'down': [], 'slow': [], 'unknown': 0,
                'models_down': 0, 'models_slow': 0}
    provs = doc.get('providers') if isinstance(doc, dict) else None
    if not isinstance(provs, dict):
        provs = {}
    down, slow, n_ok, m_down, m_slow = [], [], 0, 0, 0
    for pid, h in sorted(provs.items()):
        if not isinstance(h, dict):
            continue
        st = h.get('status')
        if st == 'OK':
            n_ok += 1
        elif st == 'DOWN':
            down.append(pid)
        elif st == 'SLOW':
            slow.append(pid)
        for mn, mh in (h.get('models') or {}).items():
            if isinstance(mh, dict):
                if mh.get('status') == 'DOWN':
                    m_down += 1
                elif mh.get('status') == 'SLOW':
                    m_slow += 1
    return {'source': path, 'present': True, 'unavailable': False, 'error': None,
            'updated': doc.get('updated') if isinstance(doc, dict) else None,
            'ok': n_ok, 'down': down, 'slow': slow,
            'unknown': 0,  # filled by gates (registry provider union)
            'models_down': m_down, 'models_slow': m_slow}


# ------------------------------------------------------------ in-flight ----

def in_flight_section():
    """Trace-walk mirroring router_ledger.cmd_status / router_spawn.ledger_in_flight."""
    path = _ledger_path()
    last, pair_of = {}, {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not isinstance(r, dict):
                    continue
                tid = r.get('trace_id')
                if not tid:
                    continue
                rec = last.setdefault(tid, {'outcome': None, 'ts': None})
                if r.get('provider') and r.get('model'):
                    pair_of[tid] = f"{r['provider']}/{r['model']}"
                if r.get('outcome') is not None:
                    rec['outcome'] = r['outcome']
                if r.get('ts') is not None:
                    rec['ts'] = r['ts']
    except FileNotFoundError:
        return {'source': path, 'present': False, 'unavailable': False,
                'error': None, 'wired': False, 'stale_after_minutes': STALE_MS // 60000,
                'total': 0, 'pairs': []}
    except Exception as e:
        return {'source': path, 'present': True, 'unavailable': True,
                'error': f'ledger.jsonl unreadable: {type(e).__name__}: {e}',
                'wired': False, 'stale_after_minutes': STALE_MS // 60000,
                'total': 0, 'pairs': []}
    now = datetime.datetime.now(datetime.timezone.utc)
    inflight, last_outcome = {}, {}
    for tid, rec in last.items():
        pair = pair_of.get(tid)
        if not pair:
            continue
        tsdt = _parse_utc(rec['ts'])
        fresh = bool(tsdt is not None and
                     (now - tsdt).total_seconds() * 1000 <= STALE_MS)
        if rec['outcome'] == 'started' and fresh:
            inflight[pair] = inflight.get(pair, 0) + 1
        if rec['outcome'] in ('success', 'failure', 'error'):
            last_outcome[pair] = rec['outcome']
    pairs = sorted(set(inflight) | set(last_outcome))
    return {'source': path, 'present': True, 'unavailable': False, 'error': None,
            'wired': len(last) > 0,
            'stale_after_minutes': STALE_MS // 60000,
            'total': sum(inflight.values()),
            'pairs': [{'pair': p, 'in_flight': inflight.get(p, 0),
                       'last_outcome': last_outcome.get(p)} for p in pairs]}


# ----------------------------------------------------------------- gaps ----

def gaps_section():
    """Gap count via router_gaps.assess (exact same dimensions/criteria)."""
    try:
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import router_gaps  # reads ROUTING_DATA_DIR at import — same env
        tables = router_gaps.load()
        per = router_gaps.assess(tables)
    except Exception as e:  # noqa: BLE001 — fail-open, visible
        return {'source': DATA_DIR, 'unavailable': True,
                'error': f'gap assessment failed: {type(e).__name__}: {e}',
                'total_models': 0, 'gapped': 0, 'dimensions': {}}
    gapped = [x for x in per if x['n_missing'] > 0]
    dims = {}
    for x in gapped:
        for d in x['missing']:
            key = str(d).split('(')[0]
            dims[key] = dims.get(key, 0) + 1
    return {'source': DATA_DIR, 'unavailable': False, 'error': None,
            'total_models': len(per), 'gapped': len(gapped),
            'dimensions': dims}


# ---------------------------------------------------------------- gates ----

def gates_section(reg, quota, health, circuit, inflight, tables):
    """Per-provider roll-up: plan / quota / health / circuit / in-flight."""
    providers = {}
    for row in tables.get('providers') or []:
        pid = row.get('id')
        if pid:
            providers.setdefault(pid, {'plan': row.get('plan')})
    qs = {}
    qpath = os.path.join(MR, 'quota-state.json')
    qdoc = _load_json(qpath)
    if isinstance(qdoc, dict) and isinstance(qdoc.get('providers'), dict):
        qs = qdoc['providers']
    hs = {}
    hdoc = _load_json(os.path.join(MR, 'health-state.json'))
    if isinstance(hdoc, dict) and isinstance(hdoc.get('providers'), dict):
        hs = hdoc['providers']
    for pid in list(qs) + list(hs):
        providers.setdefault(pid, {'plan': None})
    open_by_prov = {}
    for p in circuit.get('pairs') or []:
        if p['state'] == 'OPEN':
            prov = p['pair'].split('/', 1)[0]
            open_by_prov[prov] = open_by_prov.get(prov, 0) + 1
    inf_by_prov = {}
    for p in inflight.get('pairs') or []:
        prov = p['pair'].split('/', 1)[0]
        inf_by_prov[prov] = inf_by_prov.get(prov, 0) + p['in_flight']
    rows = []
    for pid in sorted(providers):
        q = qs.get(pid) if isinstance(qs.get(pid), dict) else {}
        h = hs.get(pid) if isinstance(hs.get(pid), dict) else {}
        models = h.get('models') or {}
        m_down = sum(1 for v in models.values()
                     if isinstance(v, dict) and v.get('status') == 'DOWN')
        m_slow = sum(1 for v in models.values()
                     if isinstance(v, dict) and v.get('status') == 'SLOW')
        rows.append({
            'provider': pid,
            'plan': providers[pid].get('plan'),
            'quota': q.get('status'),
            'quota_reason': q.get('reason'),
            'health': h.get('status'),
            'health_latency_ms': h.get('latency_ms'),
            'models_down': m_down,
            'models_slow': m_slow,
            'circuit_open': open_by_prov.get(pid, 0),
            'in_flight': inf_by_prov.get(pid, 0),
        })
    return {'providers': rows}


# ----------------------------------------------------------------- text ----

def _fmt(v, dash='-'):
    return dash if v is None else str(v)


def render_text(doc):
    lines = []
    r = doc['registry']
    lines.append(f"task-router status — {doc['generated_at']}")
    if r['unavailable']:
        lines.append(f"registry   UNAVAILABLE: {r['error']} (reading {r['path']})")
    else:
        gen = f"generated {r['generated_at']} ({r['generated_at_source']})" if r['generated_at'] else 'generated ?'
        lines.append(f"registry   {r['path']}  {gen}  engine={_fmt(r['engine'])}")
    c = r['counts']
    lines.append(f"models     {c['rows']} rows · {c['active']} active · "
                 f"{c['priced']} priced · providers {c['providers']}")
    h = doc['health']
    if h['unavailable']:
        lines.append(f"health     UNAVAILABLE: {h['error']}")
    else:
        upd = f" (updated {h['updated']})" if h.get('updated') else ''
        lines.append(f"health     {h['ok']} OK · {len(h['down'])} DOWN · "
                     f"{len(h['slow'])} SLOW · models {h['models_down']} down / "
                     f"{h['models_slow']} slow{upd}")
        if h['down']:
            lines.append(f"           DOWN: {', '.join(h['down'])}")
        if h['slow']:
            lines.append(f"           SLOW: {', '.join(h['slow'])}")
    q = doc['quota']
    if q['unavailable']:
        lines.append(f"quota      UNAVAILABLE: {q['error']}")
    else:
        names = ', '.join(g['provider'] for g in q['gated_providers'])
        lines.append(f"quota      {q['open']} open · {q['gated']} gated"
                     + (f" ({names})" if names else ''))
    ci = doc['circuit']
    if ci['unavailable']:
        lines.append(f"circuit    UNAVAILABLE: {ci['error']}")
    else:
        lines.append(f"circuit    {ci['open']} OPEN · {ci['cooling']} cooling")
        for p in ci['pairs'][:5]:
            until = f" until {p['open_until']}" if p['state'] == 'OPEN' else ''
            lines.append(f"           {p['pair']:<44} {p['state']}{until} "
                         f"failures={p['failures']}")
        if len(ci['pairs']) > 5:
            lines.append(f"           … +{len(ci['pairs']) - 5} more")
    inf = doc['in_flight']
    if inf['unavailable']:
        lines.append(f"in-flight  UNAVAILABLE: {inf['error']}")
    else:
        wire = 'yes' if inf['wired'] else 'no (concurrency gate inactive)'
        lines.append(f"in-flight  {inf['total']} across "
                     f"{sum(1 for p in inf['pairs'] if p['in_flight'])} pairs "
                     f"(ledger wired: {wire})")
        for p in inf['pairs']:
            if p['in_flight']:
                lo = f" last={p['last_outcome']}" if p['last_outcome'] else ''
                lines.append(f"           {p['pair']:<44} in_flight={p['in_flight']}{lo}")
    g = doc['gaps']
    if g['unavailable']:
        lines.append(f"gaps       UNAVAILABLE: {g['error']}")
    else:
        dims = ' '.join(f"{k}={v}" for k, v in sorted(g['dimensions'].items()))
        lines.append(f"gaps       {g['gapped']}/{g['total_models']} active models "
                     f"missing >=1 dimension" + (f"  [{dims}]" if dims else ''))
    lines.append('')
    lines.append('per-provider gates (health / quota / circuit-open / in-flight):')
    lines.append(f"  {'provider':<20} {'plan':<26} {'health':<7} {'quota':<7} "
                 f"{'circ':>4} {'flt':>4} models")
    for row in doc['gates']['providers']:
        mm = f"{row['models_down']}d/{row['models_slow']}s" if (row['models_down'] or row['models_slow']) else ''
        lines.append(f"  {row['provider']:<20} {_fmt(row['plan']):<26} "
                     f"{_fmt(row['health']):<7} {_fmt(row['quota']):<7} "
                     f"{row['circuit_open']:>4} {row['in_flight']:>4} {mm}")
    return '\n'.join(lines)


def build():
    reg = registry_section()
    circuit = circuit_section()
    quota = quota_section()
    health = health_section()
    inflight = in_flight_section()
    gaps = gaps_section()
    tables = {}
    doc = _load_json(REGISTRY)
    if isinstance(doc, dict) and isinstance(doc.get('tables'), dict):
        tables = doc['tables']
    else:
        try:
            for fn in sorted(os.listdir(DATA_DIR)):
                if fn.endswith('.jsonl'):
                    rows = []
                    with open(os.path.join(DATA_DIR, fn)) as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                rows.append(json.loads(line))
                    tables[fn[:-len('.jsonl')]] = rows
        except Exception:
            tables = {}
    gates = gates_section(reg, quota, health, circuit, inflight, tables)
    # providers probed by health but absent from the registry count as unknown
    known = {row['provider'] for row in gates['providers']}
    health['unknown'] = sum(1 for pid in (health.get('down') or []) +
                            (health.get('slow') or []) if pid not in known)
    return {'generated_at': _now_iso(),
            'registry': reg, 'health': health, 'quota': quota,
            'circuit': circuit, 'in_flight': inflight, 'gaps': gaps,
            'gates': gates}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='task-router one-command overview (registry/gates/in-flight/gaps)')
    ap.add_argument('--format', choices=['json', 'text'], default='json',
                    help='json = pure JSON (default), text = human table')
    args = ap.parse_args(argv)
    try:
        doc = build()
    except Exception as e:  # noqa: BLE001 — exit 0 always (status tool)
        doc = {'generated_at': _now_iso(), 'unavailable': True,
               'error': f'status assembly failed: {type(e).__name__}: {e}'}
    if args.format == 'json':
        print(json.dumps(doc, indent=1))
    else:
        print(render_text(doc))
    return 0


if __name__ == '__main__':
    sys.exit(main())
