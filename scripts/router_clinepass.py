#!/usr/bin/env python3
"""router_clinepass.py — clinepass catalog + billing sync (Bane 2026-08-27).

clinepass is NOT on models.dev. Its real data comes from its own API:
  GET  https://api.cline.bot/api/v1/models  -> 417 models (id = org/model)
  GET  https://api.cline.bot/api/v1/plans   -> Cline Pass flat $9.99/mo,
       2-5x usage vs API rate, caps $1B/5h $2.5B/7d $5B/30d, included list
       (Kimi K3, GLM 5.2, Kimi K2.6, K2.7 Code, Mimo v2.5/Pro, Minimax M3,
       Qwen3.7 Plus/Max, DeepSeek V4 Pro/Flash), monthly plan carries
       features.discount=0.5 (promo flag — verify)

What it does (idempotent):
  sync  — model_catalog.jsonl (api_id = CALLABLE form 'cline-pass/<bare>';
          the org/model ids in /models are Cline-product-surface ONLY, 403 on
          raw API — verified live) + models.jsonl rows (price NULL ->
          pricing gap; the pricing engine prices the flat-plan included set)
        — plan_terms.jsonl row for clinepass (flat_subscription) if missing
        — temporary_discounts.jsonl rows for the :free lanes (free until
          revoked) if missing
  --dry-run  print, write nothing
  --commit   git commit + push the task-router repo

Stdlib only. Repo-relative paths. Key: CLINEPASS_API_KEY in ~/.hermes/.env.
"""
import argparse
import datetime
import json
import os
import subprocess
import urllib.request

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
BASE = 'https://api.cline.bot/api/v1'
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'}

# media/embedding/safety ids are not chat-routeable
SKIP = ('embed', 'tts', 'asr', 'audio', 'image', 'video', 'whisper', 'guard',
        'modera', 'rerank', 'speech', 'stt', 'ocr', 'img', 'translate')


def _key():
    p = os.path.expanduser('~/.hermes/.env')
    if os.path.exists(p):
        for line in open(p):
            if 'CLINEPASS_API_KEY' in line and '=' in line:
                k = line.strip().split('=', 1)[1].strip().strip('"\'')
                if k and k.lower() not in ('none', 'null', 'changeme'):
                    return k
    return None


def _get(path):
    key = _key()
    assert key, 'CLINEPASS_API_KEY not found in ~/.hermes/.env'
    req = urllib.request.Request(BASE + path, headers={'Authorization': f'Bearer {key}', **UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _rows(name):
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    out = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _write(name, rows):
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    with open(path + '.tmp', 'w') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(path + '.tmp', path)


def _chat_ok(mid):
    low = mid.lower()
    return not any(s in low for s in SKIP)


def _bare(mid):
    return mid.split('/', 1)[1] if '/' in mid else mid


def sync(dry_run):
    api = _get('/models')
    raw = sorted(d['id'] for d in api.get('data', []))
    print(f'clinepass API: {len(raw)} models')

    models = _rows('models')
    catalog = _rows('model_catalog')
    terms = _rows('plan_terms')
    discounts = _rows('temporary_discounts')
    have = {(m['provider'], m['model']) for m in models}
    have_cat = {(c['provider'], c['model']) for c in catalog}
    have_terms = {t['provider'] for t in terms}
    have_disc = {(d['provider'], d['model']) for d in discounts}
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    added = skipped_media = skipped_dup = 0
    for mid in raw:
        if not _chat_ok(mid):
            skipped_media += 1
            continue
        name = _bare(mid)
        if ('clinepass', name) in have:
            skipped_dup += 1
            continue
        models.append({'provider': 'clinepass', 'model': name, 'normalized_price': None,
                       'price_evidence': 'clinepass-api', 'data_class': 'zdr', 'plan_tier': None,
                       'token_factor': 1.0, 'archive': False, 'valid_from': today, 'valid_to': None})
        have.add(('clinepass', name))
        added += 1
        if ('clinepass', name) not in have_cat:
            catalog.append({'provider': 'clinepass', 'model': name, 'api_id': f'cline-pass/{name}',
                            'api_note': 'API-callable form (org/model ids are Cline-product-only)',
                            'context_window': None, 'reasoning': None, 'tool_call': None,
                            'vision': None, 'modality': None, 'knowledge_cutoff': None,
                            'cost_input': None, 'cost_output': None, 'family': None,
                            'source': 'clinepass-api', 'fetched_at': now, 'archive': False})
            have_cat.add(('clinepass', name))

    # plan_terms — flat_subscription row if missing
    term_added = 0
    if 'clinepass' not in have_terms:
        terms.append({
            'provider': 'clinepass', 'billing_model': 'flat_subscription',
            'plan_cost': 9.99, 'interval': 'monthly', 'usage_multiplier': 3.0,
            'included_models': ['kimi-k3', 'glm-5.2', 'kimi-k2.6', 'kimi-k2.7-code',
                                'mimo-v2.5', 'mimo-v2.5-pro', 'minimax-m3',
                                'qwen3.7-plus', 'qwen3.7-max', 'deepseek-v4-pro',
                                'deepseek-v4-flash'],
            'note': 'Cline Pass $9.99/mo flat — 2-5x usage vs standard API rate (docs.cline.bot); '
                    'caps $1B/5h $2.5B/7d $5B/30d (usage-cost, effectively unlimited); plans API '
                    'carries features.discount=0.5 flag on monthly (promo? verify); non-included '
                    'models are PAYG at published per-token prices (research agent to fill).',
            'source': 'clinepass-plans-api + docs.cline.bot 2026-08-27', 'added': today})
        have_terms.add('clinepass')
        term_added = 1

    # temporary discounts for :free lanes
    disc_added = 0
    for mid in raw:
        if not mid.endswith(':free'):
            continue
        name = _bare(mid)
        if ('clinepass', name) in have_disc:
            continue
        discounts.append({'provider': 'clinepass', 'model': name,
                          'discount_type': 'free', 'value': 1.0,
                          'valid_from': today, 'valid_to': None,
                          'source': 'clinepass-api :free lane',
                          'note': 'temporary free lane — verify end date'})
        have_disc.add(('clinepass', name))
        disc_added += 1

    if dry_run:
        print(f'DRY-RUN: would add {added} models, {term_added} plan_terms, {disc_added} discounts; '
              f'{skipped_media} media skipped, {skipped_dup} dup names')
        return 0

    _write('models', models)
    _write('model_catalog', catalog)
    _write('plan_terms', terms)
    _write('temporary_discounts', discounts)
    print(f'wrote: +{added} models, +{term_added} plan_terms, +{disc_added} discounts '
          f'({skipped_media} media skipped, {skipped_dup} dup)')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('action', choices=['sync'])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--commit', action='store_true')
    args = ap.parse_args(argv)
    rc = sync(args.dry_run)
    if rc == 0 and args.commit:
        subprocess.run(['git', 'add', '-A'], cwd=_REPO, check=True)
        subprocess.run(['git', 'commit', '-m',
                        'chore(data): clinepass catalog sync',
                        '--author', 'Hermes <hermes@localhost>'], cwd=_REPO)
        subprocess.run(['git', 'push', 'origin', 'main'], cwd=_REPO, check=True)
    return rc


if __name__ == '__main__':
    import sys
    sys.exit(main())
