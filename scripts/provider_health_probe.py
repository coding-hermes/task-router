#!/usr/bin/env python3
"""provider_health_probe.py v2 — hourly model battery + credit check (Bane 2026-08-27).

v2 (was: 1 model per provider): the probe now checks EVERY offered model per
provider — the registry (task-router/registry.json, models table) is the source
of what we OFFER: priced / plan-tiered, not disabled, not archived, capped at
MAX_PER_PROVIDER per provider sorted by normalized price (cheapest first — the
models we would actually route to), plus the static DEFAULT models. Per-model
status lives in health-state.json (providers.<p>.models); the provider
aggregate keeps the old flat contract (status/model/latency_ms/error) using the
default model's result. Credit balances are fetched where the provider exposes
an endpoint (CREDIT_ENDPOINTS — graceful: any failure = "unknown", never fails
the probe).

Status semantics: DOWN = error/5xx/timeout · SLOW = latency > 10s · OK otherwise
NO_KEY = key env var missing · UNSUPPORTED = deliberately excluded (reason always).

Alerts: provider aggregate transitions only (no per-model spam) + one degraded
summary line per run when anything is down (watchdog contract: empty stdout =
all healthy).

Calibration gotchas (TR-001, live-verified 2026-08-27):
- Every request sends a real User-Agent: Cloudflare-fronted endpoints (groq,
  opencode.ai) answer urllib's default Python-urllib UA with 403 "error code: 1010"
  — that is bot filtering, NOT an outage or auth failure.
- Fast 401/403/400 on a ping = auth/endpoint/model misconfig (expired JWT,
  exhausted quota, wrong base_url/model id) — a calibrated result, not downtime.
  429 = real capacity pressure; 5xx = genuine provider issue.
- clinepass model ids carry the cline-pass/ prefix (e.g. cline-pass/deepseek-v4-flash).
- zai-glm base is https://api.z.ai/api/coding/paas/v4 (coding endpoint).
- openai-codex authenticates with OPENAI_API_KEY (sk-svcac service key) — the
  OAuth ChatGPT account (prolite plan) has NO API scopes (model.request /
  api.responses.write), verified 2026-08-27; never re-auth OAuth for this lane.
- gpt-5.6 models reject `max_tokens` (use max_completion_tokens, min 16) — see
  PROBE_PARAMS.
- kimi 403 access_terminated = weekly 7-day quota exhausted (window resets).
"""
import json, os, time, datetime, urllib.request, urllib.error

MR = os.path.expanduser('~/.hermes/model-router')
HEALTH_JSONL = f'{MR}/health.jsonl'
HEALTH_STATE = f'{MR}/health-state.json'
REGISTRY = os.path.expanduser('~/task-router/registry.json')
TIMEOUT_S = 8
SLOW_MS = 10000
WALL_BUDGET_S = 900   # 15 min global cap; stop probing further providers when exceeded
MAX_PER_PROVIDER = 10
UA = 'hermes-provider-health-probe/2.0'

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

# provider -> (base_url, key_env, default probe model). Endpoints/models match
# the authoritative entries in ~/.hermes/config.yaml (ground truth per TR-001).
PROVIDERS = {
    'deepseek':        ('https://api.deepseek.com/v1', 'DEEPSEEK_API_KEY', 'deepseek-v4-flash'),
    'deepseek-foreman':('https://api.deepseek.com/v1', 'DEEPSEEK_FOREMAN_API_KEY', 'deepseek-v4-flash'),
    'clinepass':       ('https://api.cline.bot/api/v1', 'CLINEPASS_API_KEY', 'cline-pass/deepseek-v4-flash'),
    'ollama-cloud':    ('https://ollama.com/v1', 'OLLAMA_CLOUD_API_KEY', 'glm-5.2'),
    'kimi-for-coding': ('https://api.kimi.com/coding/v1', 'KIMI_API_KEY', 'kimi-for-coding'),
    'neuralwatt':      ('https://api.neuralwatt.com/v1', 'NEURALWATT_API_KEY', 'deepseek-v4-flash'),
    'zai-glm':         ('https://api.z.ai/api/coding/paas/v4', 'ZAI_API_KEY', 'glm-5.2'),
    'opencode-go':     ('https://opencode.ai/zen/go/v1', 'OPENCODE_GO_API_KEY', 'glm-5.3-flash'),
    'groq':            ('https://api.groq.com/openai/v1', 'GROQ_API_KEY', 'qwen/qwen3.6-27b'),
    'xai':             ('https://api.x.ai/v1', 'XAI_API_KEY', 'grok-4.5'),
    'openai-codex':    ('https://api.openai.com/v1', 'OPENAI_API_KEY', 'gpt-5.6-luna'),
    'stepfun':         ('https://api.stepfun.ai/step_plan/v1', 'STEPFUN_STEP_PLAN_KEY', 'step-3.7-flash'),
    'minimax':         ('https://api.minimax.io/v1', 'MINIMAX_API_KEY', 'MiniMax-M3'),
    'synthetic':       ('https://api.synthetic.new/v1', 'SYNTHETIC_API_KEY', 'syn:small:text'),
}

# Providers that cannot be probed at all get an explicit UNSUPPORTED status with a
# reason here — health-state.json must never fall back to a silent default or omit
# a configured provider. Only deliberate exclusions belong here.
UNSUPPORTED = {
    # 'example': 'reason — e.g. billing-plan-only account, no inference endpoint',
}

# Per-provider body overrides: gpt-5.6 models reject `max_tokens`
# (use max_completion_tokens, min 16) — verified 2026-08-27.
PROBE_PARAMS = {
    'openai-codex': {'max_completion_tokens': 16},
}

# Credit/balance endpoints (graceful — parse failure or HTTP error => "unknown").
# key: provider id in PROVIDERS. url: absolute. All GET with Bearer key.
CREDIT_ENDPOINTS = {
    'deepseek':   ('https://api.deepseek.com/user/balance', 'DEEPSEEK_API_KEY'),
    'stepfun':    ('https://api.stepfun.ai/billing/balance', 'STEPFUN_STEP_PLAN_KEY'),
    'zai-glm':    ('https://api.z.ai/user/balance', 'ZAI_API_KEY'),
    'neuralwatt': ('https://api.neuralwatt.com/v1/balance', 'NEURALWATT_API_KEY'),
    'minimax':    ('https://api.minimax.io/v1/query/balance', 'MINIMAX_API_KEY'),
}

UP_LIKE = ('OK', 'SLOW')


def build_probe_set():
    """{provider: [(base_url, key_env, model), ...]} — static defaults + registry
    priced/active models (cap MAX_PER_PROVIDER/provider, cheapest first)."""
    out = {}
    for prov, (base, key_env, model) in PROVIDERS.items():
        out.setdefault(prov, []).append((base, key_env, model))
    try:
        reg = json.load(open(REGISTRY))
        models = reg.get('tables', {}).get('models', [])
        today = datetime.date.today().isoformat()
        by_prov = {}
        for r in models:
            p = r.get('provider')
            if p not in PROVIDERS:
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
            base, key_env, _ = PROVIDERS[p]
            existing = {m for _, _, m in out[p]}
            for _, m in rows[:MAX_PER_PROVIDER]:
                if m not in existing:
                    out[p].append((base, key_env, m))
                    existing.add(m)
    except Exception:
        pass  # registry unreadable -> static defaults only
    return out


def ping(base, key, model, params=None):
    if not base:
        return {'status': 'SKIP', 'error': 'no endpoint configured'}
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
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            r.read()
            ms = int((time.time() - t0) * 1000)
            return {'status': 'SLOW' if ms > SLOW_MS else 'OK', 'latency_ms': ms}
    except urllib.error.HTTPError as e:
        ms = int((time.time() - t0) * 1000)
        return {'status': 'DOWN', 'error': f'HTTP {e.code}', 'latency_ms': ms}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {'status': 'DOWN', 'error': str(e)[:120], 'latency_ms': ms}


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
        # common shapes: balance, credits, total_balance, data.balance,
        # data.credits, balance_infos[0].total_balance
        cands = [raw.get('balance'), raw.get('credits'), raw.get('total_balance'),
                 (raw.get('data') or {}).get('balance'),
                 (raw.get('data') or {}).get('credits'),
                 (raw.get('data') or {}).get('total_balance')]
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
        if not currency and isinstance(raw.get('data'), dict):
            currency = raw['data'].get('currency')
        return {'source': 'api', 'balance': val, 'currency': currency,
                'note': 'raw keys: ' + ','.join(list(raw.keys())[:6])}
    except Exception as e:
        return {'source': 'unknown', 'note': str(e)[:100]}


def aggregate(models):
    """models: {model: {status,...}} -> (status, stats)"""
    st = [m['status'] for m in models.values()]
    ok = st.count('OK'); slow = st.count('SLOW'); down = st.count('DOWN')
    total = len(st)
    if ok or slow:
        status = 'OK' if ok else 'SLOW'
    elif down:
        status = 'DOWN'
    else:
        status = 'SKIP'
    return status, {'ok': ok, 'slow': slow, 'down': down, 'total': total}


def main():
    env = load_env()
    os.makedirs(MR, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    try:
        prev = json.load(open(HEALTH_STATE))
    except Exception:
        prev = {'updated': None, 'providers': {}}
    prev_provs = prev.get('providers', {})

    probe_set = build_probe_set()
    results, alerts = {}, []
    wall_start = time.time()

    for prov, lanes in probe_set.items():
        if time.time() - wall_start > WALL_BUDGET_S:
            alerts.append(f'⏱️ wall budget exceeded — remaining providers skipped')
            for p in list(probe_set)[list(probe_set).index(prov):]:
                results[p] = {'status': 'SKIP', 'model': lanes[0][2], 'error': 'wall budget',
                              'models': {}, 'credits': {'source': 'none'}, 'ts': ts}
            break
        if prov in UNSUPPORTED:
            results[prov] = {'status': 'UNSUPPORTED', 'model': lanes[0][2], 'latency_ms': None,
                             'error': f'unsupported: {UNSUPPORTED[prov]}', 'models': {},
                             'credits': {'source': 'none'}, 'ts': ts}
            continue
        key = env.get(lanes[0][1], '')
        if not key:
            results[prov] = {'status': 'NO_KEY', 'model': lanes[0][2], 'models': {},
                             'credits': {'source': 'none'}, 'ts': ts}
            continue
        models = {}
        for base, key_env, model in lanes:
            k = env.get(key_env, key)
            params = PROBE_PARAMS.get(prov)
            r = ping(base, k, model, params)
            models[model] = {'status': r['status'], 'latency_ms': r.get('latency_ms'),
                             'error': r.get('error')}
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
                alerts.append(f'⚠️ {prov} DOWN ({stats["ok"]}/{stats["total"]} models up, {stats["down"]} down): '
                              f'{dflt.get("error")} — was {p_status}')
            elif (not p_up) and now_up:
                alerts.append(f'✅ {prov} back UP ({stats["ok"]} models, {dflt.get("latency_ms")}ms)')

    state = {'updated': ts, 'probe_version': 2, 'providers': results}
    tmp = HEALTH_STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, HEALTH_STATE)
    with open(HEALTH_JSONL, 'a') as f:
        f.write(json.dumps({'ts': ts, 'probe_version': 2, 'providers': results}) + '\n')

    # degraded summary: providers with down models (runs every time, not just transitions)
    degraded = []
    for prov, r in results.items():
        st = r.get('model_stats')
        if st and st['down'] > 0:
            down_models = [m for m, mm in r.get('models', {}).items() if mm['status'] == 'DOWN'][:3]
            errs = {mm['error'] for mm in r.get('models', {}).values() if mm.get('error')}
            err = next(iter(errs)) if errs else ''
            degraded.append(f"{prov} {st['ok']}/{st['total']} up ({', '.join(down_models)}{'…' if st['down'] > 3 else ''}{(' — ' + err) if err else ''})")
        elif r.get('status') == 'NO_KEY':
            degraded.append(f"{prov} NO_KEY")
    if alerts:
        print('\n'.join(alerts))
    if degraded:
        print(f"⚠️ {len(degraded)} degraded: " + '; '.join(degraded))
    # else: silent — no_agent cron suppresses empty stdout


if __name__ == '__main__':
    main()
