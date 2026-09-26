"""TR-172: the SESSION's own accounting belongs in the ledger row.

Bane's intent: the end-of-request reply carries the session's stats (tokens, time,
turns) and they should land in the JSONL. Measured against the live gateway, the reply
carries ONLY per-request usage plus the X-Hermes-Session-Id header — no turns, no
cumulative tokens, no timing — so the router fetches them from the gateway's state.db
for the session it just used and stamps them on the row.

Contracts pinned here:
  * a row carries turns / cumulative tokens / duration / tool calls / cost for ITS session
  * a per-model split shows what the session actually ran on
  * an unknown or unreadable session yields None — never a fabricated zero
  * the lookup can never fail a served request
"""
import json
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402
import router_server as rsrv   # noqa: E402

SESSION_COLS = ['id', 'message_count', 'tool_call_count', 'input_tokens', 'output_tokens',
                'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens',
                'api_call_count', 'started_at', 'last_activity_at', 'title',
                'billing_provider', 'billing_base_url', 'estimated_cost_usd',
                'actual_cost_usd', 'cost_status', 'cost_source']


@pytest.fixture()
def gateway_db(tmp_path, monkeypatch):
    """A miniature gateway state.db: only the tables the lookup contracts touch."""
    db = tmp_path / 'state.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE sessions (%s)' % ', '.join(f'{c} TEXT' for c in SESSION_COLS))
    con.execute('CREATE TABLE session_model_usage (session_id TEXT, model TEXT, '
                'billing_provider TEXT, api_call_count INTEGER, input_tokens INTEGER, '
                'output_tokens INTEGER)')
    con.execute('INSERT INTO sessions VALUES (%s)' % ','.join('?' * len(SESSION_COLS)),
                ('api-abc123', '42', '9', '120000', '3500', '90000', '5000', '1200',
                 '7', '1790399000', '1790399600', 'tick 42', 'deepseek-payg',
                 'https://api.deepseek.com/v1', '0.42', None, 'estimated', 'estimated'))
    con.execute('INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)',
                ('api-abc123', 'deepseek-v4-flash', 'deepseek-payg', 5, 100000, 3000))
    con.execute('INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)',
                ('api-abc123', 'z-ai/glm-5.3-flash', 'xkiro', 2, 20000, 500))
    con.commit()
    con.close()
    monkeypatch.setenv('ROUTER_HERMES_STATE_DB', str(db))
    return db


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / 'outcomes.jsonl'
    path.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(path))
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 3}, 'complexity_sig': 'sig', 'profile_id': None,
                       'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {
        'chain': [{'hop': 1, 'provider': 'p1', 'model': 'm1'}], 'sort': 'price'})
    return path


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]



@pytest.fixture(autouse=True)
def _isolated_router_state(tmp_path, monkeypatch):
    """TR-182: this file drives proxy_chat with fake upstreams that RAISE, and every failed
    hop is reported to the circuit. Without an isolated state dir those failures land in the
    LIVE ~/.hermes/model-router/circuit-state.json as p1/bad, p1/m1, p2/m2 -- measured there,
    next to a real doctrine pin. A unit test must not write the router's live gates."""
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))

def test_the_lookup_reads_turns_tokens_time_and_cost(gateway_db):
    st = rsrv._hermes_session_stats('api-abc123')
    assert st['turns'] == 7, 'turns IS the session api_call_count'
    assert st['input_tokens'] == 120000 and st['output_tokens'] == 3500
    assert st['cache_read_tokens'] == 90000 and st['reasoning_tokens'] == 1200
    assert st['tool_call_count'] == 9 and st['message_count'] == 42
    assert st['duration_s'] == 600.0, 'measured from the session own clock'
    assert st['cost_status'] == 'estimated'
    assert st['estimated_cost_usd'] == 0.42, 'a cost that is a STRING is silently unusable'
    assert st['source'] == 'state.db'


def test_the_lookup_reports_which_models_the_session_ran_on(gateway_db):
    st = rsrv._hermes_session_stats('api-abc123')
    models = st['models']
    assert [m['model'] for m in models] == ['deepseek-v4-flash', 'z-ai/glm-5.3-flash']
    assert models[0]['api_calls'] == 5 and models[0]['tokens_in'] == 100000


def test_an_unknown_session_is_none_not_a_fabricated_zero(gateway_db):
    assert rsrv._hermes_session_stats('api-does-not-exist') is None
    assert rsrv._hermes_session_stats(None) is None
    assert rsrv._hermes_session_stats('') is None


def test_an_unreadable_database_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTER_HERMES_STATE_DB', str(tmp_path / 'nope.db'))
    assert rsrv._hermes_session_stats('api-abc123') is None, 'absence must not raise'


def test_a_served_row_carries_its_session_accounting(ledger, gateway_db, monkeypatch):
    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'ok'}}],
                     'usage': {'prompt_tokens': 47030, 'completion_tokens': 2},
                     '_router_hermes_session_id': 'api-abc123'}
    status, _ = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                {'x-router-session': 's-1'}, upstream=upstream)
    assert status == 200
    row = [r for r in _rows(ledger) if r.get('route_outcome') == 'served'][0]
    # the per-request meters AND the session's own accounting, side by side
    assert row['tokens_in'] == 47030 and row['tokens_out'] == 2
    sess = row['session']
    assert isinstance(sess, dict), 'the session block is why this change exists'
    assert sess['turns'] == 7 and sess['api_call_count'] == 7
    assert sess['input_tokens'] == 120000, 'coerced to a number, not TEXT'
    assert row['turns'] is None or isinstance(row['turns'], int), (
        "the store's own `turns` keeps its meaning (accumulated turns of THIS row)")
    assert sess['duration_s'] == 600.0
    assert sess['models'][0]['model'] == 'deepseek-v4-flash'
    assert row['gateway_session_id'] == 'api-abc123'


def test_a_session_that_is_not_in_the_database_leaves_nulls(ledger, gateway_db, monkeypatch):
    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'ok'}}], 'usage': {},
                     '_router_hermes_session_id': 'api-unknown'}
    rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                    {'x-router-session': 's-2'}, upstream=upstream)
    row = [r for r in _rows(ledger) if r.get('route_outcome') == 'served'][0]
    assert row['session'] is None, 'a session it cannot read leaves no invented numbers'
    assert row['gateway_session_id'] == 'api-unknown', 'the id is still recorded'


def test_a_failed_hop_still_writes_its_normal_row(ledger, gateway_db, monkeypatch):
    def upstream(path, body, headers):
        raise TimeoutError('slow')
    rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                    {'x-router-session': 's-3'}, upstream=upstream)
    row = _rows(ledger)[0]
    assert row['route_outcome'] == 'failed' and row['session'] is None
