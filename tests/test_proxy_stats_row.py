"""TR-143: a routed request must leave a COMPLETE statistics row — success or failure.

The row is the only place the router sees the work itself, so every fact the
envelope claims must be reconstructible from it: which session, which complexity
matrix, which hop served it, how many steps it took, what it cost and on which
price basis, the cache meters, and — on failure — a reason CODE rather than prose.

These tests drive the real record path (no _proxy_record stub): the ledger is
redirected to a tmp file and the row is read back off disk. A test that asserts
the shape of an argument would pass while the row stayed blind.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402
import router_server as rsrv   # noqa: E402


def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m, 'usd_1m': 0.1 * (i + 1),
                       'outcomes': {'stats_fallback': 'unconditioned'}}
                      for i, (p, m) in enumerate(pairs)],
            'sort': 'price'}


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """Redirect the outcome ledger and quiet the breaker side-effect."""
    path = tmp_path / 'outcomes.jsonl'
    path.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(path))
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 3, 'debug': 1}, 'complexity_sig': 'sig123',
                       'profile_id': None, 'problems': []}))
    return path


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _serve(monkeypatch, upstream, pairs=(('p1', 'm1'),)):
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(*pairs))
    return rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                           {'x-router-session': 'sess-9'}, upstream=upstream)


def test_a_served_request_writes_a_row_carrying_every_measured_fact(ledger, monkeypatch):
    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'ok'}}],
                     'usage': {'prompt_tokens': 1000, 'completion_tokens': 50,
                               'prompt_tokens_details': {'cached_tokens': 900},
                               'completion_tokens_details': {'reasoning_tokens': 7}},
                     '_router_hermes_session_id': 'gw-sess-1'}
    status, payload = _serve(monkeypatch, upstream)
    assert status == 200

    rows = _rows(ledger)
    assert len(rows) == 1
    row = rows[0]
    # identity + join keys
    assert row['session_id'] == 'router-proxy:sess-9'
    assert row['gateway_session_id'] == 'gw-sess-1'
    assert row['source_system'] == 'router-proxy'
    # the complexity that chose the lane, not just prose
    assert row['complexity_sig'] == 'sig123'
    assert row['required_categories'] == {'code_gen': 3, 'debug': 1}
    assert row['complexity_source'] == 'classifier'
    # the ladder facts
    assert row['route_outcome'] == 'served'
    assert row['served_by_hop'] == 1 and row['hops_attempted'] == 1 and row['steps'] == 1
    # meters, including the cache reads the economics actually turn on
    assert row['tokens_in'] == 1000 and row['tokens_out'] == 50
    assert row['cache_read_tokens'] == 900
    assert row['tokens_reasoning'] == 7
    assert row['success'] is True and row['failure_reason'] is None


def test_a_failed_hop_writes_a_row_with_a_reason_CODE_and_null_cost(ledger, monkeypatch):
    def upstream(path, body, headers):
        raise TimeoutError('timed out')
    _serve(monkeypatch, upstream, pairs=(('p1', 'm1'), ('p2', 'm2')))

    rows = _rows(ledger)
    assert len(rows) == 2, 'one row per attempted hop: the audit trail IS the ladder'
    assert [r['route_outcome'] for r in rows] == ['failed', 'failed']
    assert [r['failure_reason'] for r in rows] == ['hop-wall-timeout'] * 2
    assert all(r['success'] is False for r in rows)
    assert all(r['served_by_hop'] is None for r in rows)
    # steps grow with the ladder, so a 2-hop failure is not a 1-step row
    assert [r['steps'] for r in rows] == [1, 2]
    assert all(r['cost_usd'] is None for r in rows)
    assert all(r['degrade_reason'] is None for r in rows)


def test_a_served_second_hop_records_the_steps_it_took(ledger, monkeypatch):
    def upstream(path, body, headers):
        if body['model'] == 'bad':
            raise ConnectionError('refused')
        return 200, {'choices': [{'message': {'content': 'served'}}]}
    _, payload = _serve(monkeypatch, upstream, pairs=(('p1', 'bad'), ('p2', 'good')))
    assert payload['_router']['served_by']['model'] == 'good'

    rows = _rows(ledger)
    assert [r['steps'] for r in rows] == [1, 2]
    assert [r['route_outcome'] for r in rows] == ['failed', 'served']
    assert rows[1]['served_by_hop'] == 2 and rows[1]['hops_attempted'] == 2
    assert rows[1]['failure_reason'] is None


def test_unreported_meters_stay_null_and_are_never_faked_to_zero(ledger, monkeypatch):
    """No usage block upstream != free. The row must say 'not measured'."""
    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'ok'}}]}
    _serve(monkeypatch, upstream)
    row = _rows(ledger)[0]
    for key in ('tokens_in', 'tokens_out', 'cache_read_tokens',
                'cache_write_tokens', 'tokens_reasoning', 'cost_usd'):
        assert row[key] is None, f'{key} must be None when the wire did not report it'


def test_the_no_eligible_hop_exit_still_leaves_a_row(ledger, monkeypatch):
    """The one case that used to leave NO trace at all."""
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {'chain': []})
    status, _ = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                upstream=lambda *a: (200, {}))
    assert status == 503
    rows = _rows(ledger)
    assert len(rows) == 1
    assert rows[0]['route_outcome'] == 'no-hops'
    assert rows[0]['failure_reason'] == 'no-hops'
    assert rows[0]['steps'] == 0 and rows[0]['hops_attempted'] == 0
    assert rows[0]['provider'] == 'none' and rows[0]['model'] == 'none'


def test_the_row_still_satisfies_the_ledger_contract(ledger, monkeypatch):
    """Extra keys are additive: every STORE_FIELD must survive (test_outcomes pins
    the same subset rule)."""
    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'ok'}}]}
    _serve(monkeypatch, upstream)
    row = _rows(ledger)[0]
    assert set(ro.STORE_FIELDS) <= set(row), set(ro.STORE_FIELDS) - set(row)


def test_the_usage_extractor_reads_both_wire_shapes():
    openai_style = rsrv._proxy_usage_full({'usage': {
        'prompt_tokens': 10, 'completion_tokens': 2,
        'prompt_tokens_details': {'cached_tokens': 8}}})
    assert (openai_style['tokens_in'], openai_style['cache_read_tokens']) == (10, 8)

    anthropic_style = rsrv._proxy_usage_full({'usage': {
        'input_tokens': 20, 'output_tokens': 4,
        'cache_read_input_tokens': 16, 'cache_creation_input_tokens': 3}})
    assert (anthropic_style['tokens_in'], anthropic_style['cache_read_tokens'],
            anthropic_style['cache_write_tokens']) == (20, 16, 3)


def test_an_empty_or_absent_usage_block_yields_all_nones():
    for payload in (None, {}, {'usage': None}, {'usage': 'nope'}, {'usage': {}}):
        m = rsrv._proxy_usage_full(payload)
        assert all(v is None for v in m.values()), (payload, m)
