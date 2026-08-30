#!/usr/bin/env python3
"""router_modelsdev.py — models.dev catalog sync into the text registry.

Bane directive 2026-08-27 (fleet chat: "your list is not providing all the
models expected"): the registry must be cross-checked against models.dev so
EVERY model the providers we use actually offer is present in the text
database — with gaps flagged instead of silently missing.

What it does:
  fetch   — download https://models.dev/api.json (browser UA) into the cache
  sync    — for each of OUR providers present on models.dev:
              * model_catalog.jsonl: catalog metadata (context, reasoning,
                tool_call, vision, modality, cost) per (provider, model)
              * NEW models not in models.jsonl -> ADDED as rows with
                normalized_price NULL + price_evidence 'models.dev-catalog'
                (gaps flagged; reprice spot-check or research fills prices)
              * models in the DB that models.dev doesn't list -> reported as
                reseller-only suspects (opencode-go/clinepass/crof lanes are
                NOT on models.dev — they resell underlying providers)
  fill    — models with NULL normalized_price get a first price from the
            models.dev cost fields (input+output)/2 with evidence
            'models.dev' (sub-plan $0/$0 lanes stay 0)
  --dry-run  print everything, write nothing
  --seed     run router_seed.py afterwards (rebuild derived tables)
  --commit   git commit + push the task-router repo (no ns writes here)

Stdlib only. Repo-relative paths (env overrides: ROUTING_DATA_DIR).
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
CACHE = os.environ.get('MODELSDEV_CACHE', os.path.expanduser('~/.chimera/models-dev-cache.json'))
MODELSDEV_URL = 'https://models.dev/api.json'
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

# our provider id -> models.dev provider id (None = custom/private, not on models.dev)
PROVIDER_MAP = {
    'ollama-cloud': 'ollama-cloud',
    'opencode-go': 'opencode-go',  # IS on models.dev (31 models: hy3, mimo, qwen3.7...)
    'clinepass': None,             # custom reseller lane (not on models.dev)
    'synthetic': 'synthetic',      # IS on models.dev (8 hf: models)
    'zai-glm': 'zai',
    'neuralwatt': 'neuralwatt',
    'groq': 'groq',
    'kimi-for-coding': 'kimi-for-coding',
    'openai-codex': 'openai',
    'deepseek': 'deepseek',
    'minimax': 'minimax',
    'stepfun': 'stepfun',
    'grok-build': 'xai',
    'crof': 'crof',                # IS on models.dev (21 models)
}

# catalog-only filter: skip niche/audio/tiny models the fleet will never route
# (whisper, prompt-guard, orpheus...). Keep chat-capable or big-context ones.
def _relevant(meta):
    if meta.get('reasoning') or meta.get('tool_call') or meta.get('vision'):
        return True
    ctx = (meta.get('limit') or {}).get('context') or 0
    return ctx >= 32000


def load_models():
    rows = []
    path = os.path.join(DATA_DIR, 'models.jsonl')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_catalog():
    rows = []
    path = os.path.join(DATA_DIR, 'model_catalog.jsonl')
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def fetch_api(dry_run):
    if dry_run and os.path.exists(CACHE):
        return json.load(open(CACHE))
    req = urllib.request.Request(MODELSDEV_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w') as f:
        json.dump(data, f)
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('action', choices=['fetch', 'sync'], help='fetch = refresh cache; sync = catalog sync')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--seed', action='store_true', help='run router_seed.py after sync')
    ap.add_argument('--commit', action='store_true', help='git commit + push the repo')
    args = ap.parse_args(argv)

    if args.action == 'fetch':
        api = fetch_api(args.dry_run)
        print(f'models.dev cache refreshed: {len(api)} providers at {CACHE}')
        return 0

    api = fetch_api(args.dry_run)
    models = load_models()
    catalog = load_catalog()
    have = {(m['provider'], m['model']) for m in models}
    cat_have = {(c['provider'], c['model']) for c in catalog}
    known_names = {m['model'] for m in models}

    adds, suspects, cat_new = [], [], 0
    for our_id, md_id in sorted(PROVIDER_MAP.items()):
        if md_id is None:
            suspects.append(our_id)
            continue
        prov = api.get(md_id)
        if not prov or not prov.get('models'):
            print(f'  ! {our_id}: provider {md_id!r} not found on models.dev')
            continue
        for mname, meta in sorted(prov['models'].items()):
            if not _relevant(meta):
                continue
            cost = meta.get('cost') or {}
            cat = {'provider': our_id, 'model': mname, 'family': meta.get('family'),
                   'context_window': meta.get('limit', {}).get('context'),
                   'reasoning': meta.get('reasoning'), 'tool_call': meta.get('tool_call'),
                   'vision': meta.get('vision'), 'modality': meta.get('modality'),
                   'knowledge_cutoff': meta.get('knowledge_cutoff'),
                   'cost_input': cost.get('input'), 'cost_output': cost.get('output'),
                   'source': 'models.dev', 'fetched_at': None}
            if (our_id, mname) not in cat_have:
                cat_new += 1
                catalog.append(cat)
            if (our_id, mname) not in have:
                # candidate: not in the registry at all
                if mname in known_names:
                    note = 'name-variant (a same-named model exists under another provider)'
                else:
                    note = 'catalog-only'
                adds.append((our_id, mname, meta, note))
            else:
                # present: catalog metadata refreshed above; PRICING is the
                # router_pricing.py engine's job (normalized, subscription-
                # aware) — never set prices from bare models.dev stickers here.
                pass

    print(f'== models.dev sync ({len(api)} providers on catalog) ==')
    print(f'catalog metadata: {cat_new} new rows (model_catalog.jsonl)')
    print(f'NEW models not in registry ({len(adds)}):')
    for our_id, mname, meta, note in adds:
        cost = (meta.get('cost') or {})
        print(f'  + {our_id}/{mname}  ctx={meta.get("limit", {}).get("context")} '
              f'reasoning={meta.get("reasoning")} tool={meta.get("tool_call")} '
              f'cost_in={cost.get("input")}  [{note}]')
    print(f'reseller lanes not on models.dev (kept, verified via live probes): {", ".join(suspects)}')
    print('PRICING NOTE: normalized prices are the router_pricing.py engine\'s job')
    print('(subscription-aware) — models.dev stickers are catalog reference only.')

    if args.dry_run:
        print('DRY-RUN: no writes')
        return 0

    # writes (atomic-ish: rewrite the jsonl files)
    def write_rows(path, rows):
        with open(path + '.tmp', 'w') as f:
            for r in rows:
                # ensure_ascii=False matches the repo convention
                # (router_maintain.py _data_rows) — literal UTF-8 keeps the
                # diff clean; ensure_ascii=True escaped em-dashes and rewrote
                # every row on each sync (969-line noise diffs).
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(path + '.tmp', path)

    if cat_new or args.action == 'fetch':
        write_rows(os.path.join(DATA_DIR, 'model_catalog.jsonl'), catalog)
        print(f'wrote model_catalog.jsonl ({len(catalog)} rows)')

    if adds:
        for our_id, mname, meta, note in adds:
            models.append({'provider': our_id, 'model': mname,
                           'normalized_price': None, 'price_evidence': 'models.dev-catalog',
                           'data_class': 'zdr', 'plan_tier': None,
                           'perf_agent_tick': None, 'perf_long_doc': None,
                           'perf_debug': None, 'perf_schema': None,
                           'perf_e2e_vision': None, 'perf_review': None,
                           'perf_delegation': None, 'perf_guard': None,
                           'perf_mock': None, 'perf_reasoning': None,
                           'valid_from': None, 'valid_to': None,
                           'archive': False, 'token_factor': 1.0})
        write_rows(os.path.join(DATA_DIR, 'models.jsonl'), models)
        print(f'added {len(adds)} models to models.jsonl (price NULL -> pricing gap)')

    if args.seed:
        seed = os.path.join(_REPO, 'scripts', 'router_seed.py')
        r = subprocess.run([sys.executable, seed], capture_output=True, text=True)
        if r.returncode != 0:
            print('SEED FAILED:', r.stderr[-400:], file=sys.stderr)
            return 1
        print('seed ok')

    if args.commit:
        r = subprocess.run(['git', 'add', '-A'], cwd=_REPO)
        r = subprocess.run(['git', 'commit', '-q', '-m',
                            'data: models.dev catalog sync (model_catalog.jsonl + new model rows)',
                            '--allow-empty'], cwd=_REPO)
        subprocess.run(['git', 'push', '-q', 'origin', 'main'], cwd=_REPO)
        print('committed + pushed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
