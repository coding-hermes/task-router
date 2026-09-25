"""Proxy METERING tests (2026-09-23): a proxied request is the only place the
router sees the work itself, so the outcome row it writes must carry the usage
block and a price. Before this, `_proxy_record` hardcoded tokens/cost to None,
which is why the cost-per-task averages could only ever learn from post-hoc
state.db imports.

Rules under test (all Bane law):
  * no fake zeros — an unmeasured meter stays None, and an unpriced hop reports
    cost None with a reason, never 0.0;
  * reporting uses the PUBLIC list price; a plan lane stamped `public_price: 0`
    must not read as FREE (the hop's in_per_m/out_per_m already carry the
    normalized fallback applied by router_spawn._pub_prices);
  * ordering/other proxy semantics are untouched.

Hermetic: the upstream call is injected, no network, no registry dependency.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402


# ---------- unit: usage extraction ----------

def test_usage_chat_completions_shape():
    assert rsrv._proxy_usage({'usage': {'prompt_tokens': 1200, 'completion_tokens': 340}}) == (1200, 340)


def test_usage_responses_shape():
    """The second proxied path spells the same meters differently."""
    assert rsrv._proxy_usage({'usage': {'input_tokens': 900, 'output_tokens': 12}}) == (900, 12)


def test_usage_absent_is_none_not_zero():
    for payload in ({}, {'usage': None}, {'usage': {}}, None, {'usage': 'n/a'}):
        assert rsrv._proxy_usage(payload) == (None, None)


def test_usage_ignores_booleans_and_junk():
    assert rsrv._proxy_usage({'usage': {'prompt_tokens': True, 'completion_tokens': 'x'}}) == (None, None)
    assert rsrv._proxy_usage({'usage': {'prompt_tokens': 5, 'completion_tokens': None}}) == (5, None)


# ---------- unit: pricing ----------

def test_cost_from_public_split():
    hop = {'in_per_m': 0.30, 'out_per_m': 1.20, 'usd_1m': 0.5}
    cost, basis = rsrv._proxy_cost(hop, 1_000_000, 500_000)
    assert cost == pytest.approx(0.30 + 0.60)
    assert 'public split' in basis


def test_cost_falls_back_to_blended_when_no_split():
    cost, basis = rsrv._proxy_cost({'usd_1m': 2.0}, 250_000, 250_000)
    assert cost == pytest.approx(1.0)
    assert 'blended' in basis


def test_plan_lane_zero_public_price_is_not_free():
    """pub==0 with a normalized rate: the split is zeroed but the blended
    fallback carries the real rate — FREE must not win."""
    cost, basis = rsrv._proxy_cost({'in_per_m': 0.0, 'out_per_m': 0.0, 'usd_1m': 0.7}, 200_000, 100_000)
    assert cost == pytest.approx(0.21)
    assert 'blended' in basis


def test_unpriced_hop_reports_unknown_not_zero():
    cost, basis = rsrv._proxy_cost({}, 1000, 1000)
    assert cost is None and 'unknown' in basis
    cost, basis = rsrv._proxy_cost({'in_per_m': 0.0, 'out_per_m': 0.0, 'usd_1m': 0.0}, 1000, 1000)
    assert cost is None and 'unknown' in basis


def test_no_usage_means_no_cost_even_with_prices():
    cost, basis = rsrv._proxy_cost({'in_per_m': 1.0, 'out_per_m': 1.0}, None, None)
    assert cost is None and 'no usage' in basis


# ---------- end to end through the ladder ----------

def _chain(monkeypatch, hops, requirements=None):
    chain = {'chain': hops}
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda *a, **k: chain)
    monkeypatch.setattr(rsrv, '_proxy_requirements',
                        lambda *a, **k: ('classifier', requirements or {
                            'matrix': {'code_gen': 1}, 'complexity_sig': 'abc', 'profile_id': None}))
    return chain


def _recorder(monkeypatch):
    rows = []
    monkeypatch.setattr(rsrv, '_proxy_record',
                        lambda provider, model, ok, req, **kw: rows.append(
                            {'provider': provider, 'model': model, 'ok': ok, **kw}))
    monkeypatch.setattr(rsrv, '_subprocess_text', lambda *a, **k: '')
    return rows


def test_proxied_success_records_tokens_and_cost(monkeypatch):
    _chain(monkeypatch, [{'hop': 1, 'provider': 'zai-glm', 'model': 'glm-5.3-flash',
                          'in_per_m': 0.5, 'out_per_m': 1.5}])
    rows = _recorder(monkeypatch)
    body = {'model': 'auto', 'messages': [{'role': 'user', 'content': 'hi'}]}
    status, out = rsrv.proxy_chat('/v1/chat/completions', body, {},
                                  upstream=lambda p, b, h: (200, {
                                      'choices': [{'message': {'content': 'ok'}}],
                                      'usage': {'prompt_tokens': 1_000_000, 'completion_tokens': 1_000_000}}))
    assert status == 200
    assert len(rows) == 1 and rows[0]['ok'] is True
    assert rows[0]['tokens_in'] == 1_000_000 and rows[0]['tokens_out'] == 1_000_000
    assert rows[0]['cost_usd'] == pytest.approx(2.0)
    served = out['_router']['served_by']
    assert served['cost_usd'] == pytest.approx(2.0)
    assert served['price_basis']
    assert out['_router']['ladder'][0]['tokens_in'] == 1_000_000


def test_proxied_success_without_usage_records_no_cost(monkeypatch):
    """The realistic case today: an upstream that returns no usage block. The
    row must say not-measured, not zero."""
    _chain(monkeypatch, [{'hop': 1, 'provider': 'p', 'model': 'm', 'in_per_m': 1.0, 'out_per_m': 1.0}])
    rows = _recorder(monkeypatch)
    status, out = rsrv.proxy_chat('/v1/chat/completions',
                                  {'model': 'auto', 'messages': [{'role': 'user', 'content': 'hi'}]}, {},
                                  upstream=lambda p, b, h: (200, {'choices': [{'message': {'content': 'ok'}}]}))
    assert status == 200
    assert rows[0]['tokens_in'] is None and rows[0]['cost_usd'] is None
    assert out['_router']['served_by']['cost_usd'] is None


def test_failed_hop_records_no_usage(monkeypatch):
    _chain(monkeypatch, [{'hop': 1, 'provider': 'p', 'model': 'm', 'in_per_m': 1.0, 'out_per_m': 1.0}])
    rows = _recorder(monkeypatch)
    status, out = rsrv.proxy_chat('/v1/chat/completions',
                                  {'model': 'auto', 'messages': [{'role': 'user', 'content': 'hi'}]}, {},
                                  upstream=lambda p, b, h: (500, {'error': 'boom', 'usage': {'prompt_tokens': 10}}))
    assert rows[0]['ok'] is False
    assert rows[0]['tokens_in'] is None and rows[0]['cost_usd'] is None


def test_responses_path_meters_too(monkeypatch):
    _chain(monkeypatch, [{'hop': 1, 'provider': 'p', 'model': 'm', 'usd_1m': 3.0}])
    rows = _recorder(monkeypatch)
    status, out = rsrv.proxy_chat('/v1/responses', {'model': 'auto', 'input': 'hi'}, {},
                                  upstream=lambda p, b, h: (200, {'output': [],
                                                                  'usage': {'input_tokens': 500_000,
                                                                            'output_tokens': 500_000}}))
    assert status == 200
    assert rows[0]['tokens_in'] == 500_000 and rows[0]['cost_usd'] == pytest.approx(3.0)


# ---------- bounded hop timeout ----------

def test_hop_timeout_is_bounded_and_env_tunable(monkeypatch):
    """A dead lane must fail fast so the ladder advances; before 2026-09-23 the
    mirror waited 1800s per hop and held the caller's request (and tick)."""
    monkeypatch.delenv('ROUTER_PROXY_HOP_TIMEOUT_S', raising=False)
    assert rsrv._proxy_hop_timeout_s() == 180.0
    monkeypatch.setenv('ROUTER_PROXY_HOP_TIMEOUT_S', '45')
    assert rsrv._proxy_hop_timeout_s() == 45.0
    for junk in ('nonsense', '0', '-3', ''):
        monkeypatch.setenv('ROUTER_PROXY_HOP_TIMEOUT_S', junk)
        assert rsrv._proxy_hop_timeout_s() == 180.0, junk


def test_upstream_uses_the_bounded_timeout(monkeypatch):
    seen = {}
    class FakeResp:
        status = 200
        def read(self): return b'{"ok": true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        seen['timeout'] = timeout
        return FakeResp()
    monkeypatch.setattr(rsrv.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setenv('ROUTER_PROXY_HOP_TIMEOUT_S', '30')
    # TR-138 changed WHICH bound applies to a gateway chat hop: the hop now asks
    # to stream so the idle watch can replace the wall clock. The guard itself
    # still holds — the timeout is always bounded, never open-ended.
    monkeypatch.setenv('ROUTER_PROXY_STREAM_HOPS', '0')
    status, payload = rsrv._proxy_upstream_default('/v1/chat/completions', {'model': 'x'}, {})
    assert status == 200 and payload == {'ok': True}
    assert seen['timeout'] == 30.0, 'streaming disabled -> the bounded wall still applies'

    monkeypatch.delenv('ROUTER_PROXY_STREAM_HOPS', raising=False)
    monkeypatch.setenv('ROUTER_PROXY_IDLE_TIMEOUT_S', '300')
    monkeypatch.setenv('ROUTER_PROXY_HOP_WALL_S', '3600')
    rsrv._proxy_upstream_default('/v1/chat/completions', {'model': 'x'}, {})
    assert seen['timeout'] == 3600.0, 'streaming enabled -> the wall is a backstop, still bounded'
    assert seen['timeout'] >= 300.0


# ---------- session association (TR-120) ----------

def test_caller_session_is_associated_and_accumulated(monkeypatch, tmp_path):
    """The caller declares its Hermes session; the proxy's outcome row carries it
    as parent_session_id and accumulates onto a per-session row instead of
    inventing an id per step."""
    store = tmp_path / 'outcomes.jsonl'
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(store))
    import importlib
    import router_outcomes as ro
    importlib.reload(ro)
    monkeypatch.setattr(rsrv, 'router_outcomes', ro, raising=False)
    _chain(monkeypatch, [{'hop': 1, 'provider': 'zai-glm', 'model': 'glm-5.3-flash',
                          'in_per_m': 1.0, 'out_per_m': 1.0}])
    monkeypatch.setattr(rsrv, '_subprocess_text', lambda *a, **k: '')
    body = {'model': 'auto', 'messages': [{'role': 'user', 'content': 'hi'}]}
    up = lambda p, b, h: (200, {'choices': [], 'usage': {'prompt_tokens': 1000, 'completion_tokens': 500}})
    for _ in range(2):
        status, out = rsrv.proxy_chat('/v1/chat/completions', body,
                                      {'x-router-session': '20260827_002414_35850a9e',
                                       'x-router-caller': 'hermes'}, upstream=up)
        assert status == 200
    rows = [json.loads(l) for l in open(store) if l.strip()]
    assert len(rows) == 1, 'two steps of one session = one task row'
    r = rows[0]
    assert r['parent_session_id'] == '20260827_002414_35850a9e'
    assert r['session_id'] == 'hermes:20260827_002414_35850a9e'
    assert r['source_system'] == 'hermes'
    assert r['steps'] == 2 and r['turns'] == 2
    assert r['tokens_in'] == 2000 and r['cost_usd'] == pytest.approx(0.003)
    assert out['_router']['outcome_row']['parent_session_id'] == '20260827_002414_35850a9e'


def test_without_a_declared_session_each_request_stands_alone(monkeypatch, tmp_path):
    store = tmp_path / 'outcomes.jsonl'
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(store))
    import importlib
    import router_outcomes as ro
    importlib.reload(ro)
    monkeypatch.setattr(rsrv, 'router_outcomes', ro, raising=False)
    _chain(monkeypatch, [{'hop': 1, 'provider': 'p', 'model': 'm', 'usd_1m': 1.0}])
    monkeypatch.setattr(rsrv, '_subprocess_text', lambda *a, **k: '')
    up = lambda p, b, h: (200, {'usage': {'prompt_tokens': 10, 'completion_tokens': 10}})
    for _ in range(2):
        rsrv.proxy_chat('/v1/chat/completions', {'model': 'auto', 'messages': []}, {}, upstream=up)
    rows = [json.loads(l) for l in open(store) if l.strip()]
    assert len(rows) == 2 and all(r['parent_session_id'] is None for r in rows)
