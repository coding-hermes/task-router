#!/usr/bin/env python3
"""provider_health_probe.py — hourly provider/model ping (the Load-Master's heartbeat).

For each provider with a key in ~/.hermes/.env: one minimal chat-completions call
(max_tokens=1, no streaming, 12s timeout) against a cheap/representative model.
Writes health.jsonl (append) + health-state.json (current).
Status: DOWN = error/5xx/timeout · SLOW = latency > 10s · OK otherwise ·
NO_KEY = key env var missing · UNSUPPORTED = deliberately excluded (see UNSUPPORTED
map below — always carries a reason, never a silent omission).
No-LLM, fail-graceful, silent unless a provider flips state (for the no_agent cron:
empty stdout = nothing to report; transition lines = alert).

Calibration gotchas (TR-001, live-verified 2026-08-27):
- Every request sends a real User-Agent: Cloudflare-fronted endpoints (groq,
  opencode.ai) answer urllib's default Python-urllib UA with 403 "error code: 1010"
  — that is bot filtering, NOT an outage or auth failure.
- Fast 401/403/400 on a ping = auth/endpoint/model misconfig (expired JWT,
  exhausted quota, wrong base_url/model id) — a calibrated result, not downtime.
  429 = real capacity pressure; 5xx = genuine provider issue.
- clinepass model ids carry the cline-pass/ prefix (e.g. cline-pass/deepseek-v4-flash).
- zai-glm base is https://api.z.ai/api/coding/paas/v4 (coding endpoint).
- openai-codex authenticates with OPENAI_ACCESS_TOKEN (Bearer JWT), not an API key.
"""
import json, os, time, datetime, urllib.request, urllib.error

MR = os.path.expanduser('~/.hermes/model-router')
HEALTH_JSONL = f'{MR}/health.jsonl'
HEALTH_STATE = f'{MR}/health-state.json'
TIMEOUT_S = 12
SLOW_MS = 10000
UA = 'hermes-provider-health-probe/1.0'

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

# provider -> (base_url, key_env, probe model). Models/endpoints match the
# authoritative entries in ~/.hermes/config.yaml (ground truth per TR-001).
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
    'openai-codex':    ('https://api.openai.com/v1', 'OPENAI_ACCESS_TOKEN', 'gpt-5.6-luna'),
    'stepfun':         ('https://api.stepfun.ai/step_plan/v1', 'STEPFUN_STEP_PLAN_KEY', 'step-3.7-flash'),
    'minimax':         ('https://api.minimax.io/v1', 'MINIMAX_API_KEY', 'MiniMax-M3'),
    'synthetic':       ('https://api.synthetic.new/v1', 'SYNTHETIC_API_KEY', 'syn:small:text'),
}

# Providers that cannot be probed at all get an explicit UNSUPPORTED status with a
# reason here — health-state.json must never fall back to a silent default or omit
# a configured provider. Only deliberate exclusions belong here (no live key by
# design, no chat-completions-compatible endpoint, etc.): a fast 401/403/400 after
# endpoint/model fixes is a calibrated DOWN, NOT unsupported.
UNSUPPORTED = {
    # 'example': 'reason — e.g. billing-plan-only account, no inference endpoint',
}

UP_LIKE = ('OK', 'SLOW')

def ping(base, key, model):
    if not base:
        return {'status': 'SKIP', 'error': 'no endpoint configured'}
    body = json.dumps({'model': model,
                       'messages': [{'role': 'user', 'content': 'ping'}],
                       'max_tokens': 1, 'stream': False}).encode()
    req = urllib.request.Request(base + '/chat/completions', data=body,
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
        if prov in UNSUPPORTED:
            results[prov] = {'status': 'UNSUPPORTED', 'model': model, 'latency_ms': None,
                             'error': f'unsupported: {UNSUPPORTED[prov]}', 'ts': ts}
            continue
        key = env.get(key_env, '')
        if not key:
            results[prov] = {'status': 'NO_KEY', 'model': model, 'ts': ts}
            continue
        r = ping(base, key, model)
        entry = {'status': r['status'], 'model': model, 'latency_ms': r.get('latency_ms'),
                 'error': r.get('error'), 'ts': ts}
        results[prov] = entry
        p = prev_provs.get(prov, {})
        p_status = p.get('status')
        if p_status:  # alert only on transitions between known states
            p_up, now_up = p_status in UP_LIKE, r['status'] in UP_LIKE
            if p_up and not now_up:
                alerts.append(f'⚠️ {prov} {r["status"]} ({model}): {r.get("error")} — was {p_status}')
            elif (not p_up) and now_up:
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
