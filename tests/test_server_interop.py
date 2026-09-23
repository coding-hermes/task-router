"""Interop + availability contracts for the router front door (2026-09-23).

Two failures found by wiring a real client at the router:

1. AVAILABILITY — the startup classifier self-check returned False on a 429 and
   the server did `sys.exit(1)`: a RATE LIMIT took the whole router down, so the
   wired client had no router at all. A transient failure must warn and serve
   (the request path already degrades visibly, R10); only a credential failure
   (401/403) is a deployment error worth refusing to boot over.

2. INTEROP — `/v1/models` answered 404. Every OpenAI-compatible client probes the
   model list before sending traffic (Hermes did), and a 404 reads as "provider
   broken" and pushes the caller to a fallback lane.

Hermetic: the payload helper is fed a temp table, no network.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402


# ---------- 1. transient vs fatal ----------

@pytest.mark.parametrize('text', [
    "classifier call failed: HTTP Error 429: Too Many Requests",
    "classifier call failed: HTTP Error 500: Internal Server Error",
    "classifier call failed: timed out",
    "connection refused",
])
def test_transient_classifier_failures_do_not_block_startup(text):
    assert rsrv._classifier_failure_is_fatal(text) is False


@pytest.mark.parametrize('text', [
    "classifier call failed: HTTP Error 401: Unauthorized",
    "classifier call failed: HTTP Error 403: Forbidden",
    "invalid api key",
    "authentication failed",
])
def test_credential_failures_are_fatal(text):
    assert rsrv._classifier_failure_is_fatal(text) is True


def test_missing_problem_text_is_not_fatal():
    assert rsrv._classifier_failure_is_fatal(None) is False
    assert rsrv._classifier_failure_is_fatal('') is False


# ---------- 2. /v1/models ----------

def _table(tmp_path, rows):
    p = tmp_path / 'models.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return str(tmp_path)


def test_models_list_has_the_openai_shape(tmp_path):
    d = _table(tmp_path, [{'provider': 'zai-glm', 'model': 'glm-5.3-flash'}])
    payload = rsrv.openai_models_payload(data_dir=d, max_age_s=0)
    assert payload['object'] == 'list'
    entry = payload['data'][0]
    assert entry == {'id': 'glm-5.3-flash', 'object': 'model', 'created': 0, 'owned_by': 'zai-glm'}


def test_models_list_hides_unreachable_lanes(tmp_path):
    """Only lanes a request could actually reach are advertised: disabled,
    archived, not-yet-available (future available_from) and retired ones are out."""
    d = _table(tmp_path, [
        {'provider': 'p', 'model': 'live'},
        {'provider': 'p', 'model': 'off', 'disabled': True},
        {'provider': 'p', 'model': 'archived', 'archive': True},
        {'provider': 'p', 'model': 'announced', 'available_from': '2099-01-01'},
        {'provider': 'p', 'model': 'retired', 'valid_to': '2020-01-01'},
    ])
    ids = [m['id'] for m in rsrv.openai_models_payload(data_dir=d, max_age_s=0)['data']]
    assert ids == ['live']


def test_models_list_caches_briefly(tmp_path):
    d = _table(tmp_path, [{'provider': 'p', 'model': 'one'}])
    rsrv._OPENAI_MODELS_CACHE.update({'at': 0.0, 'payload': None})
    first = rsrv.openai_models_payload(data_dir=d, max_age_s=300)
    _table(tmp_path, [{'provider': 'p', 'model': 'changed'}])
    again = rsrv.openai_models_payload(data_dir=d, max_age_s=300)
    assert again == first, 'a hot client loop must not re-scan the table every call'
    fresh = rsrv.openai_models_payload(data_dir=d, max_age_s=0)
    assert [m['id'] for m in fresh['data']] == ['changed']


def test_missing_table_is_an_empty_list_not_a_crash(tmp_path):
    payload = rsrv.openai_models_payload(data_dir=str(tmp_path / 'nope'), max_age_s=0)
    assert payload == {'object': 'list', 'data': []}
