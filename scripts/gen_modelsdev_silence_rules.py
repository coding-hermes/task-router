#!/usr/bin/env python3
"""TR-045 one-shot: generate modelsdev-silence rules for KNOWN-DELIBERATE
unmapped models.dev providers. Data-driven from the live cache:
  - plan/token-plan mirrors already carried via DEFAULT alias table
  - raw vendor / hub catalogs the registry deliberately does not import
Everything NOT matched here keeps printing UNMAPPED (the real signal).
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from router_modelsdev import (load_models, load_providers, load_mappings,
                              PROVIDER_MAP, map_provider_name)

CACHE = os.path.expanduser('~/.chimera/models-dev-cache.json')
TABLE = os.path.join(os.path.dirname(__file__), '..', 'data', 'tables',
                     'provider_mappings.jsonl')

api = json.load(open(CACHE))
models = load_models()
providers = load_providers()
mappings = load_mappings()

provider_ids = {p.get('id') for p in providers}
alias_targets = {v for v in PROVIDER_MAP.values() if v}

payload_keys = sorted(k for k in api if k != '_fetched_at')
unmapped = []
for ext in payload_keys:
    mapped, _ = map_provider_name(ext, mappings)
    if mapped in provider_ids:
        continue
    if any(pid.lower() == mapped.lower() for pid in provider_ids):
        continue
    if mapped in alias_targets:
        continue
    unmapped.append(ext)

PLAN_MARKERS = ('-coding-plan', '-token-plan', '-tokenhub', '-step-plan')
# cn/region suffix variants of the mirrored plan families (alibaba-coding-plan-cn,
# xiaomi-token-plan-cn/ams/sgp) — same mirror rationale, checked as SUFFIX.
PLAN_CN_SUFFIXES = ('-cn', '-ams', '-sgp')
PLAN_CN_BASES = ('alibaba-coding-plan', 'alibaba-token-plan',
                 'xiaomi-token-plan', 'scnet-token-plan')
VENDOR_OR_HUB = {
    # raw vendor catalogs imported via other lanes or deliberately not carried
    'anthropic', 'google', 'google-vertex', 'google-vertex-anthropic',
    'amazon-bedrock', 'azure', 'azure-cognitive-services', 'mistral',
    'cohere', 'moonshotai', 'moonshotai-cn', 'zhipuai', 'meta', 'nvidia',
    'minimax-cn', 'alibaba', 'alibaba-cn', 'stepfun-ai', 'upstage', 'sarvam',
    'iflowcn', 'longcat', 'sensenova', 'modelscope', 'huggingface', 'llama',
    'xiaomi', 'v0', 'poe', 'perplexity', 'perplexity-agent',
    'snowflake-cortex', 'watsonx', 'databricks', 'github-copilot', 'gitlab',
    'sap-ai-core', 'stackit', 'ovhcloud', 'scaleway', 'digitalocean',
    'vultr', 'hetzner', 'crusoe', 'gmicloud', 'modal', 'baseten',
    'deepinfra', 'nebius', 'novita-ai', 'friendli', 'cerebras', 'chutes',
    'nano-gpt', 'venice', 'requesty', 'helicone', 'llmgateway',
    'llmgateway-providers', 'openrouter', 'togetherai', 'lmstudio',
    'inference', 'io-net', 'nearai', 'siliconflow', 'siliconflow-cn',
    'tencent-tokenhub', 'kilo', 'fastrouter', 'aihubmix', 'jiekou',
    'klokintegration', '302ai',
    # served through commandcode / opencode-go aggregator lanes already carried
    'thinkingmachines', 'sakana', 'poolside', 'ofox', 'morph', 'wandb',
    'vercel', 'volcengine', 'opencode', 'edenai',
    # cline-pass: models.dev ENTRY EXISTS (15 models, 2026-09-13) but the
    # registry deliberately catalogs clinepass from its OWN API (445 models);
    # a mapping would import a 15-model shadow catalog — see skill pitfall
    # 'UNMAPPED ≠ NEW'.
    'cline-pass',
}

rows = []
existing_silence = set()
for line in open(TABLE):
    try:
        r0 = json.loads(line)
    except Exception:
        continue
    if r0.get('direction') == 'modelsdev-silence':
        existing_silence.add(r0.get('pattern'))

for ext in unmapped:
    is_plan = any(ext.endswith(m) for m in PLAN_MARKERS)
    is_plan_cn = (not is_plan and ext.endswith(PLAN_CN_SUFFIXES)
                  and any(ext.startswith(b) for b in PLAN_CN_BASES))
    if is_plan or is_plan_cn:
        note = ('plan/token-plan mirror — lane already carried via the '
                'DEFAULT alias table (e.g. zai-coding-plan -> zai-glm); '
                'raw models.dev mirror not imported')
    elif ext in VENDOR_OR_HUB:
        note = ('raw vendor/hub catalog — registry carries this family via '
                'other lanes (own provider or aggregator); models.dev '
                'mirror deliberately not imported')
    else:
        continue
    if ext in existing_silence:
        continue
    rows.append({'id': f'silence-{ext}', 'pattern': ext, 'match': 'literal',
                 'replacement': '', 'direction': 'modelsdev-silence',
                 'note': note, 'added': '2026-09-13'})

with open(TABLE, 'a') as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')

covered = {r['pattern'] for r in rows}
print(f'unmapped externals: {len(unmapped)}  silenced now: {len(rows)}')
print('still signaling UNMAPPED (new-provider signal, audit on next report):')
for ext in unmapped:
    if ext not in covered:
        print(' ', ext)
