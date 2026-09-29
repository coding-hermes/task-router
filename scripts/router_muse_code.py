#!/usr/bin/env python3
"""router_muse_code.py — Meta Muse Code SUBSCRIPTION lane sync (Bane 2026-09-26).

Why this exists
---------------
`providers.jsonl` carried the belief "Bane $50 Power sub is Muse Code CLI-only,
does not bill API keys" (meta-model row, added 2026-09-01) — so only PAYG
`meta-model` lanes existed. The `hermes-muse-code` plugin disproves that: a
Meta device-code login mints a stable inference key that authenticates against
`api.meta.ai` (verified live 2026-09-26: GET /v1/models 200 with 8 models;
POST /v1/responses 200 with cached_tokens reporting). This script registers the
SUBSCRIPTION billing path as its own provider id so the router can rank it
separately from PAYG.

Live catalog source
-------------------
  GET https://api.meta.ai/v1/models   (Authorization: Bearer <mint key>)
Chat models only: drops muse-image-* / muse-voice-* (the plugin filters these
too) and sam-* (segmentation, not a chat lane).

Billing caveat — READ BEFORE REPRICING
--------------------------------------
Whether usage on the minted key draws down the Muse Code subscription or bills
PAYG at $1.25/$4.25 per 1M is NOT yet proven from this host (no subscription
usage endpoint is exposed: /muse-code/subscription, /v1/usage, /v1/me all 404).
Until Bane confirms on his Meta billing page, this lane is priced at PAYG
sticker (billing_model=per_token) — deliberately conservative: a wrong
flat-subscription multiplier would misroute the fleet onto a metered lane.

Usage
-----
  python3 scripts/router_muse_code.py --dry-run
  python3 scripts/router_muse_code.py --commit [--push]
Key: ~/.hermes/muse-code-sub.json (written by the plugin's login) or
MUSE_CODE_SUB_TOKEN in the environment. Never printed.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import lifecycle_gate  # noqa: E402  (TR-199: R4 no anonymous dates)
CACHE = os.environ.get('MUSE_CODE_SUB_CREDENTIALS') or os.path.expanduser('~/.hermes/muse-code-sub.json')
API = 'https://api.meta.ai/v1/models'
PROVIDER = 'muse-code'
TODAY = datetime.date.today().isoformat()

#: model row schema (2026-09-19 letter) — every writer emits the full key set.
MODEL_KEYS = ('provider', 'model', 'normalized_price', 'price_evidence', 'public_price',
              'public_in_per_m', 'public_out_per_m', 'public_cache_read_per_m',
              'public_cache_write_per_m', 'data_class', 'plan_tier', 'perf_agent_tick',
              'perf_long_doc', 'perf_debug', 'perf_schema', 'perf_e2e_vision', 'perf_review',
              'perf_delegation', 'perf_guard', 'perf_mock', 'perf_reasoning', 'valid_from',
              'available_from', 'valid_to', 'archive', 'lifecycle_source',
              'lifecycle_checked_at', 'replaced_by', 'token_factor', 'disabled',
              'disabled_reason', 'context_limit', 'api_type', 'vision', 'thinking',
              'training_model_level', 'training_provider_level')

PAYG_IN, PAYG_OUT, PAYG_CACHE = 1.25, 4.25, 0.15   # meta-model rows, ev meta-docs-2026-09-02
CTX = 1048576                                       # meta-model rows carry 1M ctx


def _rows(name):
    path = os.path.join(DATA_DIR, name + '.jsonl')
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]


def _write(name, rows):
    # TR-199 (spec R4): no anonymous dates on the write path.
    lifecycle_gate.gate_rows(name, rows)
    path = os.path.join(DATA_DIR, name + '.jsonl')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        for r in rows:
            # Canonical repo style is json.dumps DEFAULT spacing AND
            # ensure_ascii=FALSE (raw unicode). Writing ensure_ascii=True escapes
            # every em dash in notes/plan strings, which creates mass churn and
            # breaks tests/test_web.py::test_edit_is_surgical_and_restorable (its
            # editor writes the unicode form, so one edited row shows as N changed
            # lines). Verified the hard way on 2026-09-26.
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, path)


def _key():
    if os.getenv('MUSE_CODE_SUB_TOKEN', '').strip():
        return os.environ['MUSE_CODE_SUB_TOKEN'].strip()
    try:
        return json.load(open(CACHE, encoding='utf-8'))['apiKey'].strip()
    except Exception:
        print(f'no credential cache at {CACHE} — run the plugin login first', file=sys.stderr)
        raise SystemExit(2)


def live_chat_models(key):
    req = urllib.request.Request(API, headers={'Authorization': f'Bearer {key}',
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    ids = [m.get('id') for m in (body.get('data') or [])]
    return sorted(i for i in ids if i and i.startswith('muse-spark-') and 'contributor' not in i)


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--dry-run', action='store_true')
    g.add_argument('--commit', action='store_true')
    ap.add_argument('--push', action='store_true')
    a = ap.parse_args(argv)

    models = live_chat_models(_key())
    print(f'live chat models on the subscription key: {", ".join(models)}')

    mrows, changed = _rows('models'), 0
    have = {(r['provider'], r['model']) for r in mrows}
    for m in models:
        if (PROVIDER, m) in have:
            continue
        row = {k: None for k in MODEL_KEYS}
        row.update({
            'provider': PROVIDER, 'model': m,
            'normalized_price': round((PAYG_IN + PAYG_OUT) / 2, 4),
            'price_evidence': f'meta-docs-{TODAY} (PAYG sticker; subscription billing unproven)',
            'public_price': round((PAYG_IN + PAYG_OUT) / 2, 4),
            'public_in_per_m': PAYG_IN, 'public_out_per_m': PAYG_OUT,
            'public_cache_read_per_m': PAYG_CACHE,
            'data_class': 'zdr', 'valid_from': TODAY, 'token_factor': 1.0,
            'disabled': False, 'context_limit': CTX,
            'api_type': 'responses', 'vision': True, 'thinking': True,
            'training_model_level': False, 'training_provider_level': False,
        })
        mrows.append(row)
        changed += 1

    prows, new_provider = _rows('providers'), False
    if not any(r.get('id') == PROVIDER for r in prows):
        prows.append({
            'id': PROVIDER,
            'plan': 'Muse Code subscription (tier TBC — Bane reports a $50 Power sub)',
            'quota_unit': 'subscription (meter unproven)', 'windows': '—', 'concurrency': None,
            'tos_class': 'api-sub', 'data_class': 'zdr', 'trains_on_hosted': False,
            'valid_from': TODAY, 'valid_to': None, 'archive': False,
            'api_base_url': 'https://api.meta.ai/v1', 'api_key_env': 'MUSE_CODE_SUB_TOKEN',
        })
        new_provider = True

    trows = _rows('plan_terms')
    trows = [r for r in trows if r.get('provider') != PROVIDER]
    trows.append({
        'provider': PROVIDER, 'billing_model': 'per_token', 'plan_cost': 50.0,
        'interval': 'monthly', 'usage_multiplier': None, 'included_models': models,
        'note': ('Muse Code subscription via device-code-minted key (hermes-muse-code plugin). '
                 'Priced at PAYG sticker ON PURPOSE: no subscription usage endpoint exists '
                 '(/muse-code/subscription, /v1/usage, /v1/me all 404 on 2026-09-26), so whether '
                 'this key draws the sub or meters PAYG is unproven. Reclassify to '
                 'flat_subscription with a measured multiplier once Bane confirms the meter '
                 'on his Meta billing page. plan_cost=50 records the reported Power tier, not a '
                 'verified charge for this path.'),
        'source': f'api.meta.ai/v1/models + hermes-muse-code live probe {TODAY}',
        'added': TODAY,
    })

    print(f'would write: models +{changed}, providers +{1 if new_provider else 0}, plan_terms 1 (replaced)')
    if a.dry_run or not a.commit:
        print('DRY-RUN (pass --commit to write)')
        return 0

    _write('models', mrows)
    _write('providers', prows)
    _write('plan_terms', trows)
    files = ['data/tables/models.jsonl', 'data/tables/providers.jsonl', 'data/tables/plan_terms.jsonl']
    subprocess.run(['git', 'add', *files], cwd=_REPO, check=True)
    msg = (f'feat(registry): add {PROVIDER} subscription lane\n\n'
           f'Live catalog from the minted Muse Code key; {changed} model row(s), provider row, '
           f'plan_terms (PAYG sticker pending meter proof).')
    subprocess.run(['git', 'commit', '-m', msg], cwd=_REPO, check=True)
    print('committed:', *files)
    if a.push:
        subprocess.run(['git', 'push', 'origin', 'HEAD'], cwd=_REPO, check=True)
        print('pushed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
