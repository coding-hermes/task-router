"""TR-172: the proxy bounds its work and rates an identical prompt once.

Origin: the 2026-09-25 incident. The scheduler bounds work by ACCEPTED SLOTS and its
failure retry is bounded + backed off, so a failing call cost it little; this side
accepted every attempt and spent an upstream model call on each — including a rating
call carrying the caller's whole context (~46k tokens). Measured: 495 proxied requests
in 78 minutes produced 430 no-hops rows (zero upstream hops) while still paying for a
classification each, and API calls per session went 6.5 -> 40 (6.1x).

Contracts pinned here:
  * over the cap, the caller gets a RETRYABLE refusal (429 + Retry-After), not a hang
  * the refusal is VISIBLE: a ledger row, and the numbers in /health
  * work under the cap is unaffected (the honest regression guard)
  * an identical prompt is rated ONCE; a FAILED rating is never cached
  * both guards are inert for normal traffic (no behaviour change when not saturated)
"""
import json
import os
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402
import router_server as rsrv   # noqa: E402


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / 'outcomes.jsonl'
    path.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(path))
    # keep the REAL requirements function reachable: the cache test needs it, since the
    # cache lives inside it. Stubbing it wholesale would test the stub.
    monkeypatch.setattr(rsrv, '_REAL_PROXY_REQUIREMENTS', rsrv._proxy_requirements, raising=False)
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 1}, 'complexity_sig': 'sig', 'profile_id': None,
                       'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {
        'chain': [{'hop': 1, 'provider': 'p1', 'model': 'm1'}], 'sort': 'price'})
    return path


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _ok():
    return lambda *a, **k: (200, {'choices': [{'message': {'content': 'ok'}}], 'usage': {}})


@pytest.fixture(autouse=True)
def _fresh_admission(monkeypatch):
    """Each test gets its own cap so a saturated one cannot leak into the next."""
    monkeypatch.setattr(rsrv, '_ADMISSION_SEM', None)
    for k in ('accepted', 'rejected', 'waiting', 'inflight', 'peak_inflight'):
        rsrv._ADMISSION[k] = 0
    with rsrv._CLASSIFY_LOCK:
        rsrv._CLASSIFY_CACHE.clear()
        rsrv._CLASSIFY_CACHE_AT.clear()
        for k in rsrv._CLASSIFY_STATS:
            rsrv._CLASSIFY_STATS[k] = 0
    yield
    rsrv._ADMISSION_SEM = None


def test_work_under_the_cap_is_untouched(ledger, monkeypatch):
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '2')
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                      {'x-router-session': 's-ok'}, upstream=_ok())
    assert status == 200, payload
    assert rsrv._admission_stats()['accepted'] == 1
    assert rsrv._admission_stats()['inflight'] == 0, 'the slot is released on the way out'


def test_over_the_cap_the_caller_gets_a_retryable_refusal(ledger, monkeypatch):
    """A refusal, not a hang and not a silent drop."""
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '1')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_MAX', '0')     # no waiting room at all
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_WAIT_S', '0')
    gate = rsrv._admission()
    gate.__enter__()                                     # occupy the only slot
    try:
        status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                          {'x-router-session': 's-refused'}, upstream=_ok())
    finally:
        gate.__exit__()
    assert status == 429, payload
    assert payload['retry_after_s'] >= 1
    assert payload['_router']['failure_reason'] == 'overloaded'
    assert payload['_router']['usage'] is None and payload['_router']['cost_usd'] is None, \
        'a refusal must not claim to have spent anything'
    row = [r for r in _rows(ledger) if r.get('route_outcome') == 'rejected'][0]
    assert row['failure_reason'] == 'overloaded' and row['hops_attempted'] == 0
    assert rsrv._admission_stats()['rejected'] >= 1


def test_the_refusal_carries_retry_after_on_the_wire_not_only_in_the_body():
    """A real client honours the header; a body-only hint is one nobody must read."""
    import inspect
    src = inspect.getsource(rsrv)
    assert "headers['Retry-After']" in src, 'the 429 must set Retry-After'
    assert 'retry_after_s' in src


def test_health_exposes_the_bound_and_the_cache(ledger, monkeypatch):
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '3')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_MAX', '7')
    payload = rsrv.RouterApplication('read-only', None).dispatch('GET', '/health')[1]
    assert payload['admission']['max_inflight'] == 3
    assert payload['admission']['queue_max'] == 7
    assert 'rejected' in payload['admission'] and 'inflight' in payload['admission']
    assert payload['classify_cache']['hits'] == 0 and payload['classify_cache']['entries'] == 0


def test_an_identical_prompt_is_rated_once(ledger, monkeypatch):
    calls = {'n': 0}

    class FakeClassify:
        @staticmethod
        def classify(text):
            calls['n'] += 1
            return {'matrix': {'code_gen': 2}, 'complexity_sig': 'sig-x', 'confidence': 0.9,
                    'model': 'fake-scorer', 'problems': []}

    monkeypatch.setitem(sys.modules, 'router_classify', FakeClassify)
    monkeypatch.setattr(rsrv, '_proxy_requirements', rsrv._REAL_PROXY_REQUIREMENTS)
    body = {'messages': [{'role': 'user', 'content': 'the very same tick prompt'}]}
    for _ in range(3):
        rsrv.proxy_chat('/v1/chat/completions', body, {'x-router-session': 's-cache'},
                        upstream=_ok())
    assert calls['n'] == 1, f'the rating is a model call; identical prompts must not repay it ({calls["n"]})'
    st = rsrv.classify_cache_stats()
    assert st['hits'] == 2 and st['puts'] == 1 and st['hit_rate'] == pytest.approx(2 / 3, abs=0.01)


def test_a_failed_rating_is_never_cached(monkeypatch):
    """A broken scorer must recover on the NEXT request, so its failure is not frozen."""
    class Failing:
        @staticmethod
        def classify(text):
            return {'matrix': None, 'complexity_sig': None, 'problems': ['no JSON object']}

    monkeypatch.setitem(sys.modules, 'router_classify', Failing)
    rsrv.classify_cache_put('some prompt', {'matrix': None})
    assert rsrv.classify_cache_stats()['entries'] == 0, 'a failure is not a rating'


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setenv('ROUTER_CLASSIFY_CACHE_MAX', '3')
    for i in range(6):
        rsrv.classify_cache_put(f'prompt-{i}', {'matrix': {'code_gen': i}})
    st = rsrv.classify_cache_stats()
    assert st['entries'] == 3, 'unbounded memory growth in a long-lived proxy is a leak'
    assert st['evictions'] == 3


def test_router_internal_outcomes_do_not_open_provider_breakers(monkeypatch):
    calls = []
    monkeypatch.setattr(rsrv, '_subprocess_text', lambda *args, **kwargs: calls.append(args))
    rsrv._proxy_record('none', 'none', False, {}, source='router-proxy',
                       route_outcome='rejected', failure_reason='overloaded')
    rsrv._proxy_record('none', 'none', False, {}, source='router-proxy',
                       route_outcome='no-hops', failure_reason='no-hops')
    assert calls == [], 'router-internal failures must not trip provider-wide breakers'


def test_an_expired_rating_is_not_served(monkeypatch):
    monkeypatch.setenv('ROUTER_CLASSIFY_CACHE_TTL_S', '1')
    rsrv.classify_cache_put('expiring', {'matrix': {'code_gen': 1}})
    hit, _ = rsrv.classify_cache_get('expiring')
    assert hit is True
    rsrv._CLASSIFY_CACHE_AT[rsrv._classify_cache_key('expiring')] = time.time() - 10
    hit2, val2 = rsrv.classify_cache_get('expiring')
    assert hit2 is False and val2 is None, 'a stale rating from another generation must not decide a lane'


def test_concurrent_work_is_actually_bounded(ledger, monkeypatch):
    """The point of the whole change: N callers cannot all be in flight at once."""
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '2')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_MAX', '0')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_WAIT_S', '0')
    seen, lock = [], threading.Lock()

    def slow(*a, **k):
        with lock:
            seen.append(rsrv._admission_stats()['inflight'])
        time.sleep(0.15)
        return 200, {'choices': [{'message': {'content': 'ok'}}], 'usage': {}}

    out = []
    def worker(i):
        out.append(rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                   {'x-router-session': f's{i}'}, upstream=slow)[0])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert max(seen) <= 2, f'never more than the cap in flight, saw {seen}'
    assert out.count(429) >= 3, f'excess callers must be refused, got {out}'
    assert rsrv._admission_stats()['peak_inflight'] <= 2
