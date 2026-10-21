"""XK-404-001 — xkiro catalog freshness.

The xkiro rows in data/tables/models.jsonl must track the LIVE catalog at
https://api.xkiro.com/v1/models. 2026-10-19/20 the registry ranked 61 models
xkiro no longer serves (including the whole z-ai/glm family at plan_tier 0),
so chains picked them and burned HTTP 404 "Model does not exist" on every
attempt (90 occurrences in 45 min). This test fails when a non-archived
xkiro row is absent from the live list, naming the stale ids to archive.
"""
import json
import os
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_JSONL = os.path.join(REPO, 'data', 'tables', 'models.jsonl')
CATALOG_URL = 'https://api.xkiro.com/v1/models'

XKIRO_KEY_ENVS = ('XKIRO_API_KEY', 'XKIRO_API_KEY_2')


def _api_key():
    for env in XKIRO_KEY_ENVS:
        key = os.environ.get(env)
        if key:
            return key
    return None


def _load_active_xkiro_models():
    rows = []
    with open(MODELS_JSONL, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get('provider') == 'xkiro' and not row.get('archive'):
                rows.append(row)
    return rows


def _fetch_live_ids():
    req = urllib.request.Request(
        CATALOG_URL,
        # Cloudflare 1010-bans the default 'Python-urllib/x.y' UA; a named
        # tool UA passes (verified live 2026-10-20).
        headers={
            'Authorization': 'Bearer ' + _api_key(),
            'User-Agent': 'task-router-freshness/1.0',
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    return set(entry['id'] for entry in payload.get('data', []))


def _stale_ids(active_rows, live_ids):
    """Pure diff: xkiro row ids absent from the live catalog."""
    return sorted(r['model'] for r in active_rows if r['model'] not in live_ids)


def test_stale_id_differ_names_rows_absent_from_live():
    """Offline pin of the diff logic (XK-404-001): absent rows are the stale set."""
    rows = [
        {'provider': 'xkiro', 'model': 'openai/gpt-5.6-terra'},
        {'provider': 'xkiro', 'model': 'z-ai/glm-5.3'},
    ]
    assert _stale_ids(rows, {'openai/gpt-5.6-terra'}) == ['z-ai/glm-5.3']
    assert _stale_ids(rows, {'openai/gpt-5.6-terra', 'z-ai/glm-5.3'}) == []


def test_non_archived_xkiro_models_are_in_live_catalog():
    if _api_key() is None:
        pytest.skip('no XKIRO_API_KEY(_2) in env — live catalog check skipped')
    live_ids = _fetch_live_ids()
    active_rows = _load_active_xkiro_models()
    stale = _stale_ids(active_rows, live_ids)
    if stale:
        pytest.fail(
            '%d non-archived xkiro models in models.jsonl are absent from live '
            '%s — chains will burn HTTP 404 "Model does not exist" on them. '
            'Stale ids (archive these rows, XK-404-001 pattern):\n  %s'
            % (len(stale), CATALOG_URL, '\n  '.join(stale))
        )
