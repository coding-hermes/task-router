#!/usr/bin/env python3
"""provider_health_probe.py v3 — hourly model battery + credit check (Bane 2026-08-31).

v3 changes (Bane 08-31: "expand the list of providers", "each provider on a new
line, up/down indented, up first then down alphabetical", "stop showing timeout
scares for thinking models", "wire the daily model-name sync to the probe logs"):
- Providers, id-fixes and excludes are DATA (data/tables/probe_providers.jsonl,
  probe_fixes.jsonl, probe_excludes.jsonl) — DATA > CODE: no provider/model
  facts hardcoded; missing file = visible gap, never a silent default.
- Provider list expanded 14 -> 21: + deepseek-duckbrain-sync, kimi (moonshot),
  grok-build (xai sub), crof, openrouter, gemini, nvidia.
- Two-stage timeout: 8s fast path, then ONE 60s retry. A model that answers on
  the slow path is SLOW (thinking), not DOWN. Only a second timeout is TIMEOUT
  (distinct status, does NOT flip the provider aggregate to DOWN).
- One retry on 5xx (transient capacity), never on 4xx (deterministic).
- Full report every run (no truncation): per-provider line, models indented,
  UP section first then SLOW/TIMEOUT then DOWN then UNPROBED, alphabetical
  within each group.

v2 (was: 1 model per provider): probes every offered model per provider — the
registry (task-router/registry.json, models table) is the source of what we
OFFER: priced / plan-tiered, not disabled, not archived, capped at
MAX_PER_PROVIDER per provider sorted by normalized price (cheapest first — the
models we would actually route to), plus the static DEFAULT models. Per-model
status lives in health-state.json (providers.<p>.models); the provider
aggregate keeps the old flat contract (status/model/latency_ms/error) using the
default model's result. Credit balances fetched where the provider exposes an
endpoint (CREDIT_ENDPOINTS — graceful: any failure = "unknown").

Status semantics: DOWN = error/5xx (after retry) · TIMEOUT = no response after
60s retry (thinking?) · SLOW = latency > 10s · OVERLOADED = HTTP 503 ·
OK otherwise · NO_KEY = key env var missing · UNSUPPORTED = deliberately
excluded (reason always). Provider DOWN only when EVERY probed model is DOWN.

Alerts: provider aggregate transitions only (no per-model spam). The full
report prints every run (no_agent cron delivers stdout verbatim).

Calibration gotchas (TR-001, live-verified 2026-08-27/08-31):
- Every request sends a real User-Agent: Cloudflare-fronted endpoints (groq,
  opencode.ai) answer urllib's default Python-urllib UA with 403 "error code: 1010"
  — that is bot filtering, NOT an outage or auth failure.
- Fast 401/403/400 on a ping = auth/endpoint/model misconfig (expired JWT,
  exhausted quota, wrong base_url/model id) — a calibrated result, not downtime.
  429 = real capacity pressure; 5xx = genuine provider issue (retried once).
- clinepass model ids carry vendor prefixes (deepseek/deepseek-v4-flash,
  xiaomi/mimo-v2.5, ...) — GET /models 2026-08-31 (424 ids).
- zai-glm base is https://api.z.ai/api/coding/paas/v4 (coding endpoint).
- openai-codex authenticates with OPENAI_API_KEY (sk-svcac service key) — the
  OAuth ChatGPT account (prolite plan) has NO API scopes (model.request /
  api.responses.write), verified 2026-08-27; never re-auth OAuth for this lane.
- gpt-5.6 + clinepass models reject `max_tokens` (use max_completion_tokens,
  min 16) — clinepass answers HTTP 500 "empty response content" to max_tokens.
- kimi 403 access_terminated = weekly 7-day quota exhausted (window resets).
- Timeout ≠ down: nemotron-3-ultra ~28s, kimi k3-256k ~10s first token —
  thinking models (Bane 08-31).
"""
import json, os, socket, time, datetime, urllib.request, urllib.error

MR = os.path.expanduser('~/.hermes/model-router')
HEALTH_JSONL = f'{MR}/health.jsonl'
HEALTH_STATE = f'{MR}/health-state.json'
REGISTRY = os.path.expanduser('~/task-router/registry.json')
DATA_DIR = os.path.expanduser('~/task-router/data/tables')
TIMEOUT_S = 8        # fast path
LONG_TIMEOUT_S = 60  # slow path — thinking models
SLOW_MS = 10000
WALL_BUDGET_S = 900   # 15 min global cap; stop probing further providers when exceeded
MAX_PER_PROVIDER = 10
UA = 'hermes-provider-health-probe/3.0'

# Credit/balance endpoints (graceful — parse failure or HTTP error => "unknown").
# key: provider id in probe_providers.jsonl. url: absolute. All GET with Bearer key.
CREDIT_ENDPOINTS = {
    'deepseek':   ('https://api.deepseek.com/user/balance', 'DEEPSEEK_API_KEY'),
    'stepfun':    ('https://api.stepfun.ai/billing/balance', 'STEPFUN_STEP_PLAN_KEY'),
    'zai-glm':    ('https://api.z.ai/user/balance', 'ZAI_API_KEY'),
    'neuralwatt': ('https://api.neuralwatt.com/v1/balance', 'NEURALWATT_API_KEY'),
    'minimax':    ('https://api.minimax.io/v1/query/balance', 'MINIMAX_API_KEY'),
    'openrouter': ('https://openrouter.ai/api/v1/credits', 'OPENROUTER_API_KEY'),
}

# Per-provider body overrides: gpt-5.6 AND clinepass deepseek models reject
# `max_tokens` (use max_completion_tokens, min 16) — verified 2026-08-27/08-31.
PROBE_PARAMS = {
    'openai-codex': {'max_completion_tokens': 16},
    'clinepass':    {'max_completion_tokens': 16},
}

UP_LIKE = ('OK', 'SLOW', 'OVERLOADED', 'TIMEOUT')

ICON = {'OK': '✓', 'SLOW': '🐢', 'OVERLOADED': '⚠️', 'DOWN': '✗',
        'TIMEOUT': '⏳', 'EXCLUDED': '–', 'SKIP': '∅'}

# Model-bucket order for per-provider listing: up-ish first, problems last,
# alphabetical inside each bucket.
MODEL_BUCKET = {'OK': 0, 'SLOW': 0, 'OVERLOADED': 0, 'TIMEOUT': 1, 'DOWN': 1, 'EXCLUDED': 2, 'SKIP': 2}


def load_env():
    env = {}
    try:
        for line in open(os.path.expanduser('~/.hermes/.env')):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


def load_providers():
    """probe_providers.jsonl -> {id: (base_url, key_env, default_model)}.
    Missing/empty file = visible gap (report shows NOTHING, never fabricates)."""
    provs = {}
    path = os.path.join(DATA_DIR, 'probe_providers.jsonl')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get('enabled', True) and row.get('id') and row.get('base_url') and row.get('key_env'):
                provs[row['id']] = (row['base_url'], row['key_env'], row.get('default_model'))
    return provs


def load_fix_rows():
    """probe_fixes.jsonl -> {(provider, model): fix_to}; probe_excludes.jsonl ->
    {(provider, model): reason}. Data rows override nothing here (dict keyed by
    source id) — a row is a row."""
    fixes, excludes = {}, {}
    for fname, target, key in (('probe_fixes.jsonl', fixes, 'fix_to'),
                               ('probe_excludes.jsonl', excludes, 'reason')):
        path = os.path.join(DATA_DIR, fname)
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                prov, model = row.get('provider'), row.get('model')
                if prov and model and row.get(key):
                    target[(prov, model)] = row
    return fixes, excludes


def build_probe_set(providers):
    """{provider: [(base_url, key_env, model), ...]} — static defaults + registry
    priced/active models (cap MAX_PER_PROVIDER/provider, cheapest first)."""
    out = {}
    for prov, (base, key_env, model) in providers.items():
        out.setdefault(prov, []).append((base, key_env, model))
    try:
        reg = json.load(open(REGISTRY))
        models = reg.get('tables', {}).get('models', [])
        today = datetime.date.today().isoformat()
        by_prov = {}
        for r in models:
            p = r.get('provider')
            if p not in providers:
                continue  # only probe providers we have endpoints/keys for
            if r.get('disabled') or r.get('archive'):
                continue
            vt = r.get('valid_to')
            if vt and str(vt) < today:
                continue
            price = r.get('normalized_price')
            if price is None and r.get('plan_tier') is None:
                continue  # not an offered/priced model
            by_prov.setdefault(p, []).append((price if price is not None else 1e9, r.get('model')))
        for p, rows in by_prov.items():
            rows.sort(key=lambda x: (x[0], x[1]))
            base, key_env, _ = providers[p]
            existing = {m for _, _, m in out[p]}
            for _, m in rows[:MAX_PER_PROVIDER]:
                if m not in existing:
                    out[p].append((base, key_env, m))
                    existing.add(m)
    except Exception:
        pass  # registry unreadable -> static defaults only
    return out


def _req(base, key, model, params, timeout):
    body = {'model': model,
            'messages': [{'role': 'user', 'content': 'ping'}],
            'stream': False}
    body.update(params or {'max_tokens': 16})
    req = urllib.request.Request(base + '/chat/completions', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': f'Bearer {key}',
                                          'User-Agent': UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return {'status': 'OK', 'latency_ms': int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {'status': 'HTTPERR', 'code': e.code, 'latency_ms': int((time.time() - t0) * 1000)}
    except Exception as e:
        msg = str(e)
        if isinstance(e, (socket.timeout, TimeoutError)) or 'timed out' in msg.lower() or 'timeout' in msg.lower():
            return {'status': 'TIMEOUT_ERR', 'latency_ms': int((time.time() - t0) * 1000)}
        return {'status': 'ERR', 'error': msg[:120], 'latency_ms': int((time.time() - t0) * 1000)}


def ping(base, key, model, params=None):
    if not base:
        return {'status': 'SKIP', 'error': 'no endpoint configured'}
    r = _req(base, key, model, params, TIMEOUT_S)
    if r['status'] == 'OK':
        return {'status': 'SLOW' if r['latency_ms'] > SLOW_MS else 'OK', 'latency_ms': r['latency_ms']}
    if r['status'] == 'HTTPERR':
        if r['code'] == 503:
            return {'status': 'OVERLOADED', 'error': 'HTTP 503 (overloaded)', 'latency_ms': r['latency_ms']}
        if r['code'] >= 500:
            # one retry — 5xx is transient capacity, not an outage (Bane 08-28/08-31)
            r2 = _req(base, key, model, params, TIMEOUT_S)
            if r2['status'] == 'OK':
                return {'status': 'SLOW' if r2['latency_ms'] > SLOW_MS else 'OK',
                        'latency_ms': r2['latency_ms'], 'note': f'ok on 5xx retry (first HTTP {r["code"]})'}
            if r2['status'] == 'HTTPERR' and r2['code'] < 500:
                return {'status': 'DOWN', 'error': f'HTTP {r2["code"]} (after HTTP {r["code"]})',
                        'latency_ms': r2['latency_ms']}
            return {'status': 'DOWN', 'error': f'HTTP {r["code"]} (persists after retry)',
                    'latency_ms': r['latency_ms']}
        return {'status': 'DOWN', 'error': f'HTTP {r["code"]}', 'latency_ms': r['latency_ms']}
    if r['status'] == 'TIMEOUT_ERR':
        # thinking models need a long rope — retry once at LONG_TIMEOUT_S (Bane 08-31)
        r2 = _req(base, key, model, params, LONG_TIMEOUT_S)
        if r2['status'] == 'OK':
            return {'status': 'SLOW', 'latency_ms': r2['latency_ms'],
                    'note': f'thinking — answered in {r2["latency_ms"]}ms (first attempt timed out at {TIMEOUT_S}s)'}
        if r2['status'] == 'TIMEOUT_ERR':
            return {'status': 'TIMEOUT', 'error': f'no response in {LONG_TIMEOUT_S}s (thinking?)', 'latency_ms': None}
        if r2['status'] == 'HTTPERR':
            return {'status': 'DOWN', 'error': f'HTTP {r2["code"]} on slow retry', 'latency_ms': r2['latency_ms']}
        return {'status': 'DOWN', 'error': r2.get('error', 'error on slow retry'), 'latency_ms': r2.get('latency_ms')}
    return {'status': 'DOWN', 'error': r.get('error'), 'latency_ms': r.get('latency_ms')}


def check_credits(prov, env):
    """Best-effort credit balance. Never raises; failures => source 'unknown'."""
    entry = CREDIT_ENDPOINTS.get(prov)
    if not entry:
        return {'source': 'none', 'note': 'no balance endpoint'}
    url, key_env = entry
    key = env.get(key_env, '')
    if not key:
        return {'source': 'none', 'note': f'no key ({key_env})'}
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {key}',
                                               'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = json.loads(r.read().decode(errors='replace'))
        data = raw.get('data') or {}
        cands = [raw.get('balance'), raw.get('credits'), raw.get('total_balance'),
                 data.get('balance'), data.get('credits'), data.get('total_balance'),
                 data.get('total_credits'), data.get('total_usage')]
        if isinstance(raw.get('balance_infos'), list) and raw['balance_infos']:
            bi = raw['balance_infos'][0]
            cands += [bi.get('total_balance'), bi.get('balance')]
        val = next((c for c in cands if isinstance(c, (int, float))), None)
        if val is None:  # some APIs return numeric strings (deepseek)
            for c in cands:
                if isinstance(c, str):
                    try:
                        val = float(c)
                        break
                    except ValueError:
                        continue
        currency = None
        if isinstance(raw.get('balance_infos'), list) and raw['balance_infos']:
            currency = raw['balance_infos'][0].get('currency')
        if not currency and data:
            currency = data.get('currency') or data.get('currency_code')
        return {'source': 'api', 'balance': val, 'currency': currency,
                'note': 'raw keys: ' + ','.join(list(raw.keys())[:6])}
    except Exception as e:
        return {'source': 'unknown', 'note': str(e)[:100]}


def aggregate(models):
    """models: {model: {status,...}} -> (status, stats). EXCLUDED rows don't
    count toward totals. Provider DOWN only when EVERY probed model is down —
    a single up/overloaded model keeps the provider OK (Bane 08-28); all-timeout
    (thinking) providers are TIMEOUT, not DOWN (Bane 08-31)."""
    st = [m['status'] for m in models.values() if m['status'] != 'EXCLUDED']
    ok = st.count('OK'); slow = st.count('SLOW'); ov = st.count('OVERLOADED')
    timeout = st.count('TIMEOUT'); down = st.count('DOWN')
    total = len(st)
    if total and down == total:
        status = 'DOWN'
    elif ok or ov:
        status = 'OK'
    elif slow:
        status = 'SLOW'
    elif timeout:
        status = 'TIMEOUT'
    else:
        status = 'SKIP'
    return status, {'ok': ok, 'slow': slow, 'overloaded': ov, 'timeout': timeout,
                    'down': down, 'total': total}


def fmt_provider_block(prov, entry):
    """One provider block: name line + indented model lines, up models first
    then problems, alphabetical within each bucket."""
    st = entry.get('model_stats', {})
    status = entry.get('status', '?')
    lines = []
    stats = f"{st.get('ok', 0)}/{st.get('total', 0)} up"
    extra = []
    if st.get('slow'):
        extra.append(f"{st['slow']} slow")
    if st.get('timeout'):
        extra.append(f"{st['timeout']} timing out")
    if st.get('overloaded'):
        extra.append(f"{st['overloaded']} overloaded")
    if st.get('down'):
        extra.append(f"{st['down']} down")
    if extra:
        stats += ' · ' + ', '.join(extra)
    lat = entry.get('latency_ms')
    if lat is not None:
        stats += f" · {lat}ms"
    cr = entry.get('credits', {})
    if cr.get('source') == 'api' and cr.get('balance') is not None:
        cur = (cr.get('currency') or '') + ' '
        stats += f" · 💰 {cur}{cr.get('balance'):g}"
    if entry.get('status') == 'NO_KEY':
        lines.append(f"  {prov} — NO_KEY (no {entry.get('key_env', 'key')} in .env)")
        return lines
    if entry.get('status') == 'UNSUPPORTED':
        lines.append(f"  {prov} — unsupported: {entry.get('error', '')}")
        return lines
    if entry.get('status') == 'SKIP' and not entry.get('models'):
        lines.append(f"  {prov} — skipped ({entry.get('error', 'no lanes')})")
        return lines
    lines.append(f"  {prov} {stats}")
    models = entry.get('models', {})
    ordered = sorted(models.items(),
                     key=lambda kv: (MODEL_BUCKET.get(kv[1].get('status'), 2),
                                     (kv[1].get('probed_as') or kv[0]).lower()))
    for model, mm in ordered:
        stt = mm.get('status', '?')
        icon = ICON.get(stt, '?')
        disp = mm.get('probed_as') or model
        det = ''
        if mm.get('latency_ms') is not None:
            det = f" {mm['latency_ms']}ms"
        elif mm.get('error'):
            det = f" {mm['error']}"
        if mm.get('note'):
            det += f" ({mm['note']})"
        lines.append(f"    {icon} {disp}{det}")
    return lines


def main(config_path=None, only_providers=None, output_path=None):
    # Backwards compatibility: old callers pass no arguments; argparse callers
    # pass the parsed values. Unset values keep the module defaults.
    if output_path:
        global HEALTH_STATE, HEALTH_JSONL
        HEALTH_STATE = output_path
        HEALTH_JSONL = output_path.rsplit('.', 1)[0] + '.jsonl' if '.' in output_path else output_path + '.jsonl'
    env = load_env()
    os.makedirs(MR, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    try:
        prev = json.load(open(HEALTH_STATE))
    except Exception:
        prev = {'updated': None, 'providers': {}}
    prev_provs = prev.get('providers', {})

    providers = load_providers()
    if only_providers:
        providers = {p: v for p, v in providers.items() if p in only_providers}
    if not providers:
        print('⚠️  no probe_providers.jsonl — nothing probed (data file missing at '
              f'{DATA_DIR}); refusing to fabricate a provider list')
        return 1
    fixes, excludes = load_fix_rows()
    probe_set = build_probe_set(providers)
    results, alerts = {}, []
    wall_start = time.time()

    for prov, lanes in probe_set.items():
        if time.time() - wall_start > WALL_BUDGET_S:
            alerts.append(f'⏱️ wall budget exceeded — remaining providers skipped')
            for p in list(probe_set)[list(probe_set).index(prov):]:
                results[p] = {'status': 'SKIP', 'model': lanes[0][2], 'error': 'wall budget',
                              'models': {}, 'credits': {'source': 'none'}, 'ts': ts}
            break
        key = env.get(lanes[0][1], '')
        if not key:
            results[prov] = {'status': 'NO_KEY', 'model': lanes[0][2], 'models': {},
                             'key_env': lanes[0][1], 'credits': {'source': 'none'}, 'ts': ts}
            continue
        models = {}
        for i, (base, key_env, model) in enumerate(lanes):
            if (prov, model) in excludes:
                models[model] = {'status': 'EXCLUDED', 'latency_ms': None,
                                 'error': excludes[(prov, model)]['reason']}
                continue
            params = PROBE_PARAMS.get(prov)
            r = ping(base, key, model, params)
            if r['status'] == 'DOWN' and (prov, model) in fixes:
                alt = fixes[(prov, model)]['fix_to']
                r2 = ping(base, key, alt, params)
                if r2['status'] in ('OK', 'SLOW', 'OVERLOADED', 'TIMEOUT'):
                    r2['note'] = (r2.get('note') + '; ' if r2.get('note') else '') + \
                                 f'id corrected: {model} → {alt}'
                    r2['probed_as'] = alt
                    r = r2
            models[model] = {'status': r['status'], 'latency_ms': r.get('latency_ms'),
                             'error': r.get('error'), 'note': r.get('note'),
                             'probed_as': r.get('probed_as')}
            if i < len(lanes) - 1:
                time.sleep(0.15)  # gentle: never blast a provider (Bane 08-28)
        status, stats = aggregate(models)
        dflt = models.get(lanes[0][2], next(iter(models.values())))
        entry = {'status': status, 'model': lanes[0][2],
                 'latency_ms': dflt.get('latency_ms'), 'error': dflt.get('error'),
                 'models': models, 'model_stats': stats,
                 'credits': check_credits(prov, env), 'ts': ts}
        results[prov] = entry
        p = prev_provs.get(prov, {})
        p_status = p.get('status')
        if p_status:  # alert only on aggregate transitions between known states
            p_up, now_up = p_status in UP_LIKE, status in UP_LIKE
            if p_up and not now_up:
                alerts.append(f'⚠️ {prov} DOWN ({stats["ok"]}/{stats["total"]} models up, '
                              f'{stats["down"]} down, {stats["timeout"]} timing out): '
                              f'{dflt.get("error")} — was {p_status}')
            elif (not p_up) and now_up:
                alerts.append(f'✅ {prov} back UP ({stats["ok"]} models, '
                              f'{dflt.get("latency_ms")}ms)')

    state = {'updated': ts, 'probe_version': 3, 'providers': results}
    tmp = HEALTH_STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, HEALTH_STATE)
    with open(HEALTH_JSONL, 'a') as f:
        f.write(json.dumps({'ts': ts, 'probe_version': 3, 'providers': results}) + '\n')

    # ---- report: transitions, then full formatted listing --------------------
    out = []
    if alerts:
        out.extend(alerts)
        out.append('')
    n = len(results)
    out.append(f'📡 Provider health — {n} providers · {ts} UTC')

    def group(statuses):
        return sorted([(p, r) for p, r in results.items() if r.get('status') in statuses],
                      key=lambda kv: kv[0])

    up = group(('OK', 'SLOW', 'OVERLOADED'))
    think = group(('TIMEOUT',))
    down = group(('DOWN',))
    unprobed = group(('NO_KEY', 'UNSUPPORTED', 'SKIP'))

    if up:
        out.append('')
        out.append(f'🟢 UP ({len(up)})')
        for p, r in up:
            out.extend(fmt_provider_block(p, r))
    if think:
        out.append('')
        out.append(f'🟠 THINKING / TIMEOUT ({len(think)})')
        for p, r in think:
            out.extend(fmt_provider_block(p, r))
    if down:
        out.append('')
        out.append(f'🔴 DOWN ({len(down)})')
        for p, r in down:
            out.extend(fmt_provider_block(p, r))
    if unprobed:
        out.append('')
        out.append(f'⚪ UNPROBED ({len(unprobed)})')
        for p, r in unprobed:
            out.extend(fmt_provider_block(p, r))
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Provider health probe (fast, no side effects with --help)')
    # TR-029: --help must be safe and print help without live-probing.
    # The full --dry-run / --no-write ergonomics are TR-031; here we add only the
    # minimal argparse wrapper so -h / --help exits before any network call.
    ap.add_argument('--config', default='config/probe_config.yaml')
    ap.add_argument('--providers', nargs='+')
    ap.add_argument('--output', default='state/health-state.json')
    args = ap.parse_args()
    raise SystemExit(main(args.config, args.providers, args.output))
