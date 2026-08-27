#!/usr/bin/env python3
"""provider_health_probe.py — hourly provider/model ping (the Load-Master's heartbeat).

For each provider with a key in ~/.hermes/.env: one minimal chat-completions call
(max_tokens=1, no streaming, 12s timeout) against a cheap/representative model.
Writes health.jsonl (append) + health-state.json (current).
Status: DOWN = error/5xx/timeout · SLOW = latency > 10s · OK otherwise.
No-LLM, fail-graceful, silent unless a provider flips state (for the no_agent cron:
empty stdout = nothing to report; transition lines = alert).
"""
import json, os, sys, time, datetime, urllib.request, urllib.error

MR = os.path.expanduser('~/.hermes/model-router')
HEALTH_JSONL = f'{MR}/health.jsonl'
HEALTH_STATE = f'{MR}/health-state.json'
TIMEOUT_S = 12
SLOW_MS = 10000

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

# provider -> (base_url, key_env, probe model). None key = skipped (marked UNVERIFIED).
PROVIDERS = {
    'deepseek':        ('https://api.deepseek.com/v1', 'DEEPSEEK_API_KEY', 'deepseek-v4-flash'),
    'deepseek-foreman':('https://api.deepseek.com/v1', 'DEEPSEEK_FOREMAN_API_KEY', 'deepseek-v4-flash'),
    'clinepass':       ('https://api.cline.bot/api/v1', 'CLINEPASS_API_KEY', 'cline-pass/deepseek-v4-flash'),
    'ollama-cloud':    ('https://ollama.com/v1', 'OLLAMA_CLOUD_API_KEY', 'glm-5.2'),
    'kimi-for-coding': ('https://api.kimi.com/coding/v1', 'KIMI_API_KEY', 'kimi-for-coding'),
    'neuralwatt':      ('https://api.neuralwatt.com/v1', 'NEURALWATT_API_KEY', 'deepseek-v4-flash'),
    'zai-glm':         ('https://api.z.ai/api/coding/paas/v4', 'ZAI_API_KEY', 'glm-5.2'),
    'opencode-go':     ('https://opencode.ai/zen/go/v1', 'OPENCODE_GO_API_KEY', 'ox-alpha-free'),
    'groq':            ('https://api.groq.com/openai/v1', 'GROQ_API_KEY', 'gpt-oss-120b'),
    'xai':             ('https://api.x.ai/v1', 'XAI_API_KEY', 'grok-4.5'),
    'openai-codex':    ('https://api.openai.com/v1', 'OPENAI_ACCESS_TOKEN', 'gpt-5.6-luna'),
    'stepfun':         ('https://api.stepfun.com/v1', 'STEPFUN_STEP_PLAN_KEY', 'step-3.7-flash'),
    'minimax':         ('https://api.minimaxi.com/v1', 'MINIMAX_API_KEY', 'minimax-m3'),
    'synthetic':       ('https://api.synthetic.new/v1', 'SYNTHETIC_API_KEY', 'gpt-oss-120b'),
}

def ping(base, key, model):
    if not base:
        return {'status': 'SKIP', 'error': 'no endpoint configured'}
    body = json.dumps({'model': model,
                       'messages': [{'role': 'user', 'content': 'ping'}],
                       'max_tokens': 1, 'stream': False}).encode()
    req = urllib.request.Request(base + '/chat/completions', data=body,
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': f'Bearer {key}'})
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

def main():
    env = load_env()
    os.makedirs(MR, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    try:
        prev = json.load(open(HEALTH_STATE))
    except Exception:
        prev = {'updated': None, 'providers': {}}
    prev_provs = prev.get('providers', {})

    results, alerts = {}, []
    for prov, (base, key_env, model) in PROVIDERS.items():
        key = env.get(key_env, '')
        if not key:
            results[prov] = {'status': 'NO_KEY', 'model': model, 'ts': ts}
            continue
        r = ping(base, key, model)
        entry = {'status': r['status'], 'model': model, 'latency_ms': r.get('latency_ms'),
                 'error': r.get('error'), 'ts': ts}
        results[prov] = entry
        p = prev_provs.get(prov, {})
        if p.get('status') in ('OK', 'SLOW') and r['status'] == 'DOWN':
            alerts.append(f'⚠️ {prov} DOWN ({model}): {r.get("error")} — was {p.get("status")}')
        elif p.get('status') == 'DOWN' and r['status'] in ('OK', 'SLOW'):
            alerts.append(f'✅ {prov} back UP ({model}, {r.get("latency_ms")}ms)')

    state = {'updated': ts, 'providers': results}
    tmp = HEALTH_STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, HEALTH_STATE)
    with open(HEALTH_JSONL, 'a') as f:
        f.write(json.dumps({'ts': ts, 'providers': results}) + '\n')

    if alerts:
        print('\n'.join(alerts))
    # else: silent — no_agent cron suppresses empty stdout

if __name__ == '__main__':
    main()
