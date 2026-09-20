"""TR-071 — the hermes proxy driver (SPEC-PROXY-DRIVERS).

Acceptance: one live session routed via the proxy produces a TR-049 outcome row
with source_system="hermes" and a visible complexity classification.

The product gap this closes: `_proxy_record` always stamped 'router-proxy', so a
proxied hermes session was indistinguishable from any other anonymous proxy
traffic — the host could not be attributed at all, which is the point of wiring
it to the proxy.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
sys.path.insert(0, os.path.join(REPO, 'scripts', 'drivers'))

import drivers          # noqa: E402
import router_server as rs   # noqa: E402


# ------------------------------------------------------------- the contract
def test_driver_registry_resolves_hermes():
    assert 'hermes' in drivers.list_drivers()
    d = drivers.get_driver('hermes')
    assert d.id == 'hermes'
    assert 'W' in d.surfaces and 'T' in d.surfaces
    assert d.wire_format == 'openai-chat'


def test_unknown_driver_returns_none_instead_of_raising():
    """Callers must be able to degrade visibly, not crash."""
    assert drivers.get_driver('does-not-exist') is None


def test_driver_config_points_at_the_proxy_and_declares_its_caller():
    d = drivers.get_driver('hermes')
    cfg = d.config('http://127.0.0.1:9092')
    entry = cfg['providers']['router']
    assert entry['base_url'] == 'http://127.0.0.1:9092/v1'
    assert entry['headers']['x-router-caller'] == 'hermes'
    # a driver must never carry a credential itself
    assert 'api_key' not in entry
    assert entry['api_key_env'] == 'ROUTER_PROXY_KEY'


def test_the_telemetry_reader_is_delegated_not_duplicated():
    """T must reuse router_outcomes.import_hermes — two readers would drift."""
    import inspect
    import hermes as h
    src = inspect.getsource(h.HermesDriver.rows_from_state_db)
    assert 'ro.import_hermes' in src, (
        'the hermes T reader must delegate to the existing importer')
    row = h.HermesDriver.row_for(session_id='s', provider='p', model='m')
    assert row['source_system'] == 'hermes'


# ------------------------------------------------- attribution through proxy
@pytest.fixture
def outcomes(tmp_path, monkeypatch):
    p = tmp_path / 'outcomes.jsonl'
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(p))
    return p


def _fake_chain(hops=2):
    return lambda *a, **k: {
        'chain': [{'hop': i + 1, 'provider': f'p{i+1}', 'model': f'm{i+1}',
                   'usd_1m': float(i + 1)} for i in range(hops)],
        'exclusions': [], 'gate_reasons': [], 'sort': 'price'}


def _request(headers, calls, upstream_kind='first-fails'):
    def upstream(path, body, headers):
        calls.append(headers.get('x-router-provider'))
        if upstream_kind == 'first-fails' and len(calls) == 1:
            return 429, {'error': 'rate limited'}
        return 200, {'choices': [{'message': {'content': 'ok'}}]}
    return rs.proxy_chat('/v1/chat/completions',
                         {'messages': [{'role': 'user', 'content': 'hi'}]},
                         headers, upstream=upstream)


def test_a_declared_caller_is_attributed_on_every_row(outcomes, monkeypatch):
    monkeypatch.setattr(rs, '_proxy_chain', _fake_chain())
    calls = []
    status, out = _request({'x-router-caller': 'hermes',
                            'x-router-profile': 'P1_CODING'}, calls)
    assert status == 200
    assert out['_router']['caller'] == 'hermes'
    rows = [json.loads(l) for l in open(outcomes)]
    assert rows, 'the proxy must write an outcome row per attempt'
    assert {r['source_system'] for r in rows} == {'hermes'}, (
        'every attempted hop belongs to the declared caller')


def test_complexity_classification_is_visible_in_the_envelope(outcomes, monkeypatch):
    """Acceptance requires a VISIBLE complexity classification."""
    monkeypatch.setattr(rs, '_proxy_chain', _fake_chain())
    calls = []
    _, out = _request({'x-router-caller': 'hermes', 'x-router-profile': 'P1_CODING'}, calls)
    assert out['_router']['complexity_source'] == 'declared'
    assert out['_router']['requirements']['profile_id'] == 'P1_CODING'


def test_session_id_is_one_per_request_not_per_hop(outcomes, monkeypatch):
    """The store dedupes on (source_system, session_id, model). A per-hop id
    made every collapsed attempt indistinguishable, so a 2-hop walk wrote two
    unrelated 'sessions' for ONE client request."""
    monkeypatch.setattr(rs, '_proxy_chain', _fake_chain())
    calls = []
    _request({'x-router-caller': 'hermes'}, calls)
    rows = [json.loads(l) for l in open(outcomes)]
    assert len(rows) == 2, 'the ladder attempted two hops'
    assert len({r['session_id'] for r in rows}) == 1, (
        'one client request = one session, however many hops it took')


def test_the_successful_row_records_total_ladder_time(outcomes, monkeypatch):
    """On success wall_time_s means 'time to get an answer', so it must be at
    least the failing hop's own latency (measured in the same walk)."""
    monkeypatch.setattr(rs, '_proxy_chain', _fake_chain())
    calls = []
    _, out = _request({'x-router-caller': 'hermes'}, calls)
    rows = [json.loads(l) for l in open(outcomes)]
    ok_row = [r for r in rows if r['success']][0]
    fail_row = [r for r in rows if not r['success']][0]
    assert ok_row['wall_time_s'] >= fail_row['wall_time_s']
    assert ok_row['wall_time_s'] == out['_router']['wall_time_s']


def test_an_undeclared_caller_stays_anonymous(outcomes, monkeypatch):
    """Nothing silently changes class: no caller header -> router-proxy."""
    monkeypatch.setattr(rs, '_proxy_chain', _fake_chain())
    calls = []
    _, out = _request({}, calls)
    assert out['_router']['caller'] == 'router-proxy'
    rows = [json.loads(l) for l in open(outcomes)]
    assert {r['source_system'] for r in rows} == {'router-proxy'}


def test_an_unknown_caller_degrades_visibly_rather_than_inventing_a_source(outcomes, monkeypatch):
    monkeypatch.setattr(rs, '_proxy_chain', _fake_chain())
    calls = []
    _, out = _request({'x-router-caller': 'not-a-driver'}, calls)
    assert out['_router']['caller'] == 'router-proxy'
    assert any('unknown caller' in p for p in out['_router']['problems'])
    rows = [json.loads(l) for l in open(outcomes)]
    assert {r['source_system'] for r in rows} == {'router-proxy'}, (
        'a typo must never mint a source_system')


def test_attribution_survives_a_dead_ladder(outcomes, monkeypatch):
    """Even a fully exhausted walk attributes its attempts — that is exactly
    when the operator needs to know who was hurt."""
    monkeypatch.setattr(rs, '_proxy_chain', _fake_chain(hops=2))
    def upstream(path, body, headers):
        return 503, {'error': 'down'}
    status, out = rs.proxy_chat('/v1/chat/completions',
                                {'messages': [{'role': 'user', 'content': 'x'}]},
                                {'x-router-caller': 'hermes'}, upstream=upstream)
    assert status >= 400 and out['_router']['exhausted'] is True
    rows = [json.loads(l) for l in open(outcomes)]
    assert len(rows) == 2 and {r['source_system'] for r in rows} == {'hermes'}
    assert all(r['success'] is False for r in rows)


def test_a_broken_driver_registry_does_not_break_a_live_request(outcomes, monkeypatch):
    """Fail-open: if the registry cannot be imported, serve as router-proxy."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == 'drivers':
            raise ImportError('registry unavailable')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', boom)
    monkeypatch.setattr(rs, '_proxy_chain', _fake_chain())
    status, out = rs.proxy_chat('/v1/chat/completions',
                                {'messages': [{'role': 'user', 'content': 'x'}]},
                                {'x-router-caller': 'hermes'},
                                upstream=lambda p, b, h: (200, {'ok': True}))
    assert status == 200
    assert out['_router']['caller'] == 'router-proxy'
    assert any('caller lookup failed' in p for p in out['_router']['problems'])
