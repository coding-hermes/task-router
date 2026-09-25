"""Rolling averages over the proxy's own traffic, written to JSONL (TR-144).

WHY THIS EXISTS
---------------
The proxy is the only component that sees a routed task end to end: which prompt
complexity arrived, which lane the chain picked, how many fallback hops it took,
what it cost and whether it worked. TR-143 made that one row complete. This turns
rows into the thing you actually steer with — rolling averages per model AND per
complexity band — so "is this lane worth keeping for THIS kind of work" is a
number rather than an opinion.

HONESTY RULES (the same ones the ledger follows)
------------------------------------------------
- A metric with no samples is None plus a reason, never 0. A cold start must not
  look like a perfect 0% failure rate.
- Every average discloses its sample count, and cost averages disclose how many
  of those samples were actually priced (a plan lane with no published price is
  unmeasured, not free).
- `generated_at` is caller-supplied for a rebuild, so the same input produces the
  same bytes: the JSONL is reproducible, which is what makes it auditable.

The shared ledger is ~115 MB / 320k rows, so a full scan per request is not free:
callers go through `get_rollup()` which caches with a DISCLOSED age, and the scan
itself uses a substring prefilter before parsing a line.
"""
import json
import os
import time

#: Bands are derived from the required levels, so the key is readable in a report
#: and stable across runs: max level => band.
BAND_STEPS = ((0, 'trivial'), (2, 'light'), (3, 'medium'), (4, 'heavy'), (5, 'frontier'))

DEFAULT_WINDOWS_H = (24, 168)


def _env_float(name, default):
    try:
        v = os.environ.get(name)
        return float(v) if v not in (None, '') else default
    except (TypeError, ValueError):
        return default


def windows_from_env():
    raw = os.environ.get('ROUTER_STATS_WINDOWS') or ''
    hours = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            hour = float(part)
        except ValueError:
            continue
        if hour > 0:
            hours.append(hour)
    return tuple(hours) or DEFAULT_WINDOWS_H


def band_for(row):
    """(band, source) for one outcome row — None band when the row says nothing.

    An unknown complexity is left unknown: importing a band from a proxy id or
    guessing from the model name would put rows in a bucket nothing measured.
    """
    cats = row.get('required_categories') if isinstance(row.get('required_categories'), dict) else None
    if cats is None and isinstance(row.get('complexity'), dict):
        cats = row['complexity']
    if not cats:
        return None, 'unknown'
    levels = []
    for v in cats.values():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            levels.append(int(v))
    if not levels:
        return None, 'unknown'
    top = max(levels)
    for threshold, name in BAND_STEPS:
        if top <= threshold:
            return name, 'matrix'
    return 'frontier', 'matrix'


def is_proxy_row(row):
    """Proxy rows only. TR-143 stamps `route_outcome`; older rows are recognised by
    their source_system so the history is not thrown away."""
    if row.get('route_outcome'):
        return True
    return str(row.get('source_system') or '').startswith('router-proxy')


def collect(path, window_h, now=None):
    """Rows for the proxy, inside the window. Prefiltered, then parsed.

    Returns (kept, scanned, matched) so a report can disclose how much of the
    file it looked at — a silent partial read is how a "no traffic" verdict gets
    invented.
    """
    now = time.time() if now is None else now
    cutoff = now - window_h * 3600.0
    kept, scanned, matched = [], 0, 0
    try:
        fh = open(path, 'r')
    except OSError:
        return kept, scanned, matched
    with fh as line_iter:
        for line in line_iter:
            scanned += 1
            if 'router-proxy' not in line and 'route_outcome' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not is_proxy_row(row):
                continue
            matched += 1
            ts = row.get('ts')
            if isinstance(ts, (int, float)) and ts < cutoff:
                continue
            kept.append(row)
    return kept, scanned, matched


def _mean(values):
    vals = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return (sum(vals) / len(vals)) if vals else None


def rollup(rows, grouping='model_band'):
    """Aggregate rows into {key: metrics}.

    grouping 'model_band' keys by (provider, model, band); 'model' collapses the
    band; 'band' collapses the lane. Each entry carries its sample counts so a
    thin average cannot masquerade as a strong one.
    """
    buckets = {}
    for row in rows:
        provider = str(row.get('provider') or 'unknown')
        model = str(row.get('model') or 'unknown')
        band, band_source = band_for(row)
        if grouping == 'model':
            key = f'{provider}/{model}'
        elif grouping == 'band':
            key = band or 'unknown'
        else:
            key = f'{provider}/{model}|{band or "unknown"}'
        b = buckets.setdefault(key, {'provider': provider, 'model': model,
                                     'band': band, 'band_source': band_source,
                                     'samples': 0, 'successes': 0, 'priced_samples': 0,
                                     'metered_samples': 0,
                                     '_cost': [], '_steps': [], '_wall': [],
                                     '_cache_read': 0, '_tokens_in': 0,
                                     '_failure_reasons': {}})
        b['samples'] += 1
        if row.get('success') is True:
            b['successes'] += 1
        if isinstance(row.get('cost_usd'), (int, float)) and not isinstance(row.get('cost_usd'), bool):
            b['priced_samples'] += 1
            b['_cost'].append(float(row['cost_usd']))
        if isinstance(row.get('steps'), (int, float)) and not isinstance(row.get('steps'), bool):
            b['_steps'].append(float(row['steps']))
        if isinstance(row.get('wall_time_s'), (int, float)) and not isinstance(row.get('wall_time_s'), bool):
            b['_wall'].append(float(row['wall_time_s']))
        cr, ti = row.get('cache_read_tokens'), row.get('tokens_in')
        if isinstance(cr, int) and isinstance(ti, int) and ti > 0:
            b['metered_samples'] += 1
            b['_cache_read'] += cr
            b['_tokens_in'] += ti
        if row.get('success') is not True and row.get('failure_reason'):
            r = str(row['failure_reason'])
            b['_failure_reasons'][r] = b['_failure_reasons'].get(r, 0) + 1

    out = {}
    for key, b in buckets.items():
        n = b['samples']
        priced = b['priced_samples']
        metered = b['metered_samples']
        entry = {
            'provider': b['provider'], 'model': b['model'],
            'band': b['band'], 'band_source': b['band_source'],
            'samples': n,
            'success_rate': (b['successes'] / n) if n else None,
            'cost_usd_per_task': (sum(b['_cost']) / priced) if priced else None,
            'cost_samples': priced,
            'cost_reason': None if priced else 'no priced samples in window',
            'steps_per_task': _mean(b['_steps']),
            'wall_time_s_per_task': _mean(b['_wall']),
            'cache_read_ratio': (b['_cache_read'] / b['_tokens_in']) if b['_tokens_in'] else None,
            'cache_samples': metered,
            'cache_reason': None if b['_tokens_in'] else 'input tokens not reported',
            'failure_reasons': dict(sorted(b['_failure_reasons'].items())),
        }
        if not n:
            entry['success_reason'] = 'no samples in window'
        out[key] = entry
    return out


def get_rollup(path, windows=None, now=None, grouping='model_band'):
    """{window_label: {'hours','rows_scanned','proxy_rows_matched','groups'}}.

    Every window states its own scan counts, so "nothing ran" and "I did not look"
    are never confused.
    """
    windows = windows or windows_from_env()
    now = time.time() if now is None else now
    out = {}
    for w in windows:
        rows, scanned, matched = collect(path, w, now=now)
        label = f'{int(w)}h' if float(w).is_integer() else f'{w}h'
        out[label] = {'hours': w, 'rows_scanned': scanned,
                      'proxy_rows_matched': matched, 'rows_in_window': len(rows),
                      'groups': rollup(rows, grouping=grouping)}
    return out


def snapshot(path, rollup_data, generated_at):
    """The JSONL snapshot line for one rollup (deterministic given generated_at)."""
    return json.dumps({'generated_at': generated_at, 'kind': 'proxy-rolling-averages',
                       'windows': rollup_data}, sort_keys=True)


def append_snapshot(path, rollup_data, generated_at=None):
    """Append one snapshot, creating the file if needed. Returns the line."""
    line = snapshot(path, rollup_data, generated_at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a') as fh:
        fh.write(line + '\n')
    return line


def rebuild(path, rollup_data, generated_at):
    """Rewrite the snapshot file so identical input yields identical bytes.

    The delete-and-rebuild proof: run it twice on the same rollup and the file
    hashes match, so the JSONL is a function of the data and nothing else (no
    wall-clock, no dict ordering).
    """
    line = snapshot(path, rollup_data, generated_at)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w') as fh:
        fh.write(line + '\n')
    return line


_CACHE = {'at': 0.0, 'path': None, 'value': None}


def cache_ttl_s():
    return _env_float('ROUTER_PROXY_STATS_TTL_S', 60.0)


def get_rollup_cached(path, windows=None, grouping='model_band'):
    """Cached rollup with a DISCLOSED age. The ledger is ~115 MB, so a per-request
    scan is not free; the age is always reported so a stale number is visible as
    a stale number."""
    now = time.time()
    ttl = cache_ttl_s()
    key = (path, grouping, tuple(windows or windows_from_env()))
    fresh = (_CACHE['value'] is not None and _CACHE['path'] == key
             and now - _CACHE['at'] <= ttl)
    if not fresh:
        _CACHE['value'] = get_rollup(path, windows=windows, now=now, grouping=grouping)
        _CACHE['at'] = now
        _CACHE['path'] = key
    return {'windows': _CACHE['value'], 'computed_at': _CACHE['at'],
            'age_s': round(now - _CACHE['at'], 3), 'cached': fresh,
            'ttl_s': ttl}


def rolling_for(provider, model, band, path=None, grouping='model_band'):
    """The served lane's own rolling block, for the proxy envelope.

    Fail-open and None-honest: an unknown lane returns None rather than zeros, so
    a first-ever call does not read as a lane with 0% success.
    """
    try:
        if path is None:
            import router_outcomes as ro
            path = ro.outcomes_path()
        data = get_rollup_cached(path, grouping=grouping)
    except Exception:  # noqa: BLE001
        return None
    key = f'{provider}/{model}|{band or "unknown"}'
    for window in ('24h', '168h'):
        groups = (data['windows'].get(window) or {}).get('groups') or {}
        if key in groups:
            entry = dict(groups[key])
            entry['window'] = window
            entry['age_s'] = data['age_s']
            return entry
    return None
