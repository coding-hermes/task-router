"""TR-121 tests: classifier resilience on 429/5xx.

Bane's live evidence (2026-09-23): the z.ai classifier 429'd on two consecutive
proxied requests — every row degraded to matrix=null exactly when traffic was
busiest. Rules under test:

  * a 429-then-success primary RETRIES and returns a matrix (no degrade);
  * persistent 429 degrades VISIBLY with the reason preserved and never
    crashes the request (R10 untouched);
  * the fallback lane is used ONLY after the primary is fully exhausted
    (no lane flapping, one fallback attempt, its own timeout budget);
  * retries/backoff are env/data-driven (ROUTER_CLASSIFIER_RETRIES) — no
    provider names in code.

Hermetic: the LLM call is injected; sleep is stubbed so backoff is shape-tested
without wall time.
"""
import json
import os
import sys
import urllib.error

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_classify as rc   # noqa: E402

CATS = ['code_gen', 'debug', 'security', 'test', 'long_doc']


import time as _time


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(_time, 'sleep', lambda s: None)


def _http_error(code):
    return urllib.error.HTTPError('url', code, 'Too Many Requests', None, None)


# ---------- bounded retry on the primary lane ----------

def test_429_then_success_retries_and_returns_matrix(monkeypatch):
    monkeypatch.setenv('ROUTER_CLASSIFIER_RETRIES', '2')
    calls = {'n': 0}
    delays = []

    import router_classify as rc
    monkeypatch.setenv('ROUTER_CLASSIFIER_BASE_URL', 'http://primary.test/v1')
    monkeypatch.setenv('ROUTER_CLASSIFIER_KEY_ENV', '')
    good = json.dumps({'categories': {'code_gen': 2}, 'confidence': 0.7})
    responses = [_http_error(429), {'choices': [{'message': {'content': good}}]}]
    seen_urls = []

    def fake_urlopen(req, timeout=None):
        seen_urls.append(req.full_url)
        if responses and isinstance(responses[0], Exception):
            raise responses.pop(0)
        return _Resp(responses.pop(0))

    monkeypatch.setattr(rc.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setattr(_time, 'sleep', delays.append)
    out = rc.classify('do a thing', categories=CATS)
    assert out['matrix'] == {'code_gen': 2}
    assert out['problems'] == []
    assert seen_urls == ['http://primary.test/v1/chat/completions'] * 2
    assert delays == [1]  # one backoff between attempt 1 and 2


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------- persistent 429 degrades visibly, never crashes ----------

def test_persistent_429_degrades_visibly_with_reason(monkeypatch):
    import router_classify as rc
    monkeypatch.setenv('ROUTER_CLASSIFIER_BASE_URL', 'http://primary.test/v1')
    monkeypatch.setenv('ROUTER_CLASSIFIER_RETRIES', '1')
    monkeypatch.setattr(rc.urllib.request, 'urlopen',
                        lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429)))
    res = rc.classify('do a thing', categories=CATS)
    assert res['matrix'] is None
    assert any('429' in p for p in res['problems'])
    # degrade shape the proxy relies on
    assert res['prompt_version'] == rc.DEFAULT_PROMPT_VERSION


def test_non_retryable_4xx_is_never_retried(monkeypatch):
    import router_classify as rc
    monkeypatch.setenv('ROUTER_CLASSIFIER_BASE_URL', 'http://primary.test/v1')
    monkeypatch.setenv('ROUTER_CLASSIFIER_RETRIES', '3')
    calls = {'n': 0}

    def fake_urlopen(req, timeout=None):
        calls['n'] += 1
        raise _http_error(401)

    monkeypatch.setattr(rc.urllib.request, 'urlopen', fake_urlopen)
    res = rc.classify('do a thing', categories=CATS)
    assert calls['n'] == 1  # 401 is a payload bug, not a hiccup
    assert res['matrix'] is None
    assert any('401' in p for p in res['problems'])


# ---------- fallback lane only after exhaustion ----------

def test_fallback_lane_used_only_after_primary_exhaustion(monkeypatch):
    import router_classify as rc
    monkeypatch.setenv('ROUTER_CLASSIFIER_BASE_URL', 'http://primary.test/v1')
    monkeypatch.setenv('ROUTER_CLASSIFIER_FALLBACK_BASE_URL', 'http://backup.test/v1')
    monkeypatch.setenv('ROUTER_CLASSIFIER_FALLBACK_MODEL', 'backup-model')
    monkeypatch.setenv('ROUTER_CLASSIFIER_FALLBACK_TIMEOUT_S', '90')
    monkeypatch.setenv('ROUTER_CLASSIFIER_RETRIES', '1')
    seen = []
    good = json.dumps({'categories': {'test': 1}, 'confidence': 0.6})

    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, timeout))
        if req.full_url.startswith('http://primary'):
            raise _http_error(429)
        return _Resp({'choices': [{'message': {'content': good}}]})

    monkeypatch.setattr(rc.urllib.request, 'urlopen', fake_urlopen)
    res = rc.classify('do a thing', categories=CATS)
    assert res['matrix'] == {'test': 1}
    # primary: 1 initial + 1 retry, then ONE fallback call with ITS OWN timeout
    assert seen == [('http://primary.test/v1/chat/completions', 60.0),
                    ('http://primary.test/v1/chat/completions', 60.0),
                    ('http://backup.test/v1/chat/completions', 90.0)]


def test_healthy_primary_never_touches_the_fallback(monkeypatch):
    import router_classify as rc
    monkeypatch.setenv('ROUTER_CLASSIFIER_BASE_URL', 'http://primary.test/v1')
    monkeypatch.setenv('ROUTER_CLASSIFIER_FALLBACK_BASE_URL', 'http://backup.test/v1')
    seen = []
    good = json.dumps({'categories': {'code_gen': 1}, 'confidence': 0.9})

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _Resp({'choices': [{'message': {'content': good}}]})

    monkeypatch.setattr(rc.urllib.request, 'urlopen', fake_urlopen)
    res = rc.classify('do a thing', categories=CATS)
    assert res['matrix'] == {'code_gen': 1}
    assert seen == ['http://primary.test/v1/chat/completions']


def test_persistent_429_on_every_lane_degrades_not_raises(monkeypatch):
    import router_classify as rc
    monkeypatch.setenv('ROUTER_CLASSIFIER_BASE_URL', 'http://primary.test/v1')
    monkeypatch.setenv('ROUTER_CLASSIFIER_FALLBACK_BASE_URL', 'http://backup.test/v1')
    monkeypatch.setenv('ROUTER_CLASSIFIER_RETRIES', '0')
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise _http_error(429)

    monkeypatch.setattr(rc.urllib.request, 'urlopen', fake_urlopen)
    res = rc.classify('do a thing', categories=CATS)
    assert res['matrix'] is None
    assert any('429' in p for p in res['problems'])
    assert len(calls) == 2  # one try on each lane, no retry loop runaway


def test_lane_config_is_env_driven_no_hardcoded_names(monkeypatch):
    import router_classify as rc
    monkeypatch.delenv('ROUTER_CLASSIFIER_BASE_URL', raising=False)
    monkeypatch.delenv('ROUTER_CLASSIFIER_FALLBACK_BASE_URL', raising=False)
    assert rc._classifier_lanes() == []
    monkeypatch.setenv('ROUTER_CLASSIFIER_BASE_URL', 'http://a.test/v1')
    lanes = rc._classifier_lanes()
    assert len(lanes) == 1 and lanes[0]['base'] == 'http://a.test/v1'
    monkeypatch.setenv('ROUTER_CLASSIFIER_FALLBACK_BASE_URL', 'http://b.test/v1')
    assert len(rc._classifier_lanes()) == 2