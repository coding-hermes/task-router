"""TR-287 lane failover: a DEAD primary (401/402/403) must reach the fallback.

Measured 2026-10-07: the DeepSeek primary went 402 Insufficient Balance and
every proxied request degraded to 'default' because 402 is not in
_RETRYABLE_STATUS, so the lane loop raised on the primary and the configured
fallback lane was never attempted. Pins both arms:
  - 402 on primary -> fallback answers (no retry of the dead primary)
  - 429 on primary -> primary retried first (bounded), then fallback

QA-TASK-ROUTER-9 hardening: the import goes through an explicit scripts/
sys.path insert (a bare `import router_classify` fails COLLECTION on any
surface where pytest's rootdir differs from the repo root — measured
standalone in the frozen-tree suite), and the fake response CLOSES its bytes
like a real HTTPResponse so no unclosed-handle warning is left for the
collector to turn into an unraisable crash.
"""

import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_classify  # noqa: E402


class _FakeResponse:
    def __init__(self, code):
        self.code = code
        self._payload = b'{}'

    def read(self, *args, **kwargs):
        return self._payload

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    return urllib.error.HTTPError('u', code, 'err', {}, _FakeResponse(code))


@pytest.fixture
def two_lanes(monkeypatch):
    """Primary = dead lane hook, fallback = answering lane hook."""

    def install(primary_codes, fallback_result='{"code_gen": 2}'):
        calls = {'primary': 0, 'fallback': 0}
        def fake_call_lane(lane, body):
            is_primary = 'PRIMARY' in (lane.get('key_env') or '')
            if is_primary:
                calls['primary'] += 1
                code = primary_codes[min(calls['primary'] - 1, len(primary_codes) - 1)]
                if code:
                    raise _http_error(code)
                return router_classify.Raw(fallback_result, finish_reason='stop')
            calls['fallback'] += 1
            return router_classify.Raw(fallback_result, finish_reason='stop')

        monkeypatch.setattr(router_classify, '_call_lane', fake_call_lane)
        monkeypatch.setattr(
            router_classify, '_classifier_lanes',
            lambda: [
                {'base': 'http://primary', 'model': 'm1',
                 'key_env': 'PRIMARY_KEY', 'key_value': None, 'timeout': 5.0},
                {'base': 'http://fallback', 'model': 'm2',
                 'key_env': 'FALLBACK_KEY', 'key_value': None, 'timeout': 5.0},
            ],
        )
        return calls

    return install


def test_402_primary_fails_over_to_fallback(two_lanes, monkeypatch):
    monkeypatch.setattr(router_classify, '_retry_budget', lambda: 0)
    calls = two_lanes(primary_codes=[402])
    got = router_classify.default_llm('sys', 'rate this')
    assert got.strip(), 'fallback answer expected'
    assert calls['fallback'] == 1, 'fallback lane must be reached on 402'
    assert calls['primary'] == 1, 'a 402 primary is not retried'


def test_401_and_403_also_fail_over(two_lanes, monkeypatch):
    monkeypatch.setattr(router_classify, '_retry_budget', lambda: 0)
    for code in (401, 403):
        calls = two_lanes(primary_codes=[code])
        got = router_classify.default_llm('sys', 'rate this')
        assert got.strip()
        assert calls['fallback'] == 1 and calls['primary'] == 1


def test_429_still_retries_primary_first(two_lanes, monkeypatch):
    monkeypatch.setattr(router_classify, '_retry_budget', lambda: 1)
    monkeypatch.setattr(router_classify, '_backoff_delay', lambda a: 0)
    calls = two_lanes(primary_codes=[429, 429])
    got = router_classify.default_llm('sys', 'rate this')
    assert got.strip()
    assert calls['primary'] == 2, '429 is retryable: primary retried before failover'
    assert calls['fallback'] == 1


def test_dead_primary_with_no_fallback_still_raises(two_lanes, monkeypatch):
    """Single-lane config: a 402 must still raise (caller degrades visibly)."""
    monkeypatch.setattr(router_classify, '_retry_budget', lambda: 0)
    two_lanes(primary_codes=[402])
    # Remove the fallback lane: only the primary remains.
    def one_lane():
        return [{'base': 'http://primary', 'model': 'm1',
                 'key_env': 'PRIMARY_KEY', 'key_value': None, 'timeout': 5.0}]
    router_classify._classifier_lanes = one_lane
    with pytest.raises(urllib.error.HTTPError):
        router_classify.default_llm('sys', 'rate this')
