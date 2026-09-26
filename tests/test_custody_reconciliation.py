"""TR-145: the chain of custody — envelope, ledger row and Hermes session must agree.

The router is the middle of the loop: a caller sends ONE prompt, the router picks a
lane, spends tokens and a gateway session runs. Three artefacts describe that same
request — the response envelope, the outcome row, and the Hermes session record —
and until now the cheapest of the three (the call path every harness uses,
chat-completions) never captured the gateway's session id at all, so the row
carried `gateway_session_id: null` and "which Hermes session did this pay for" was
unanswerable for the traffic that matters.

These contracts pin both halves: the id is CAPTURED off the response headers, and
the three artefacts RECONCILE — with a hermetic session store, so the join logic is
tested without depending on the live state.db.
"""
import io
import json
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402
import router_server as rsrv   # noqa: E402


class _Resp(io.BytesIO):
    def __init__(self, body, ctype='application/json', session='hermes-sess-42'):
        super().__init__(body)
        self.status = 200
        h = {'Content-Type': ctype}
        if session:
            h['X-Hermes-Session-Id'] = session
        self.headers = h

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sse_body():
    frames = [
        'data: ' + json.dumps({'model': 'm', 'choices': [{'delta': {'content': 'hi'}}]}) + '\n\n',
        'data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': 11, 'completion_tokens': 3}}) + '\n\n',
        'data: [DONE]\n\n',
    ]
    return ''.join(frames).encode()


def _patch_opener(monkeypatch, resp):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout=None: resp)


# ---------- capture ----------


@pytest.fixture(autouse=True)
def _isolated_router_state(tmp_path, monkeypatch):
    """TR-182: this file drives proxy_chat with fake upstreams that RAISE, and every failed
    hop is reported to the circuit. Without an isolated state dir those failures land in the
    LIVE ~/.hermes/model-router/circuit-state.json as p1/bad, p1/m1, p2/m2 -- measured there,
    next to a real doctrine pin. A unit test must not write the router's live gates."""
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))

def test_the_buffered_path_captures_the_gateway_session(monkeypatch):
    _patch_opener(monkeypatch, _Resp(json.dumps({'choices': [{'message': {'content': 'x'}}]}).encode()))
    monkeypatch.setenv('ROUTER_PROXY_STREAM_HOPS', '0')
    status, payload = rsrv._proxy_upstream_default('/v1/chat/completions', {'model': 'm'}, {})
    assert status == 200
    assert payload['_router_hermes_session_id'] == 'hermes-sess-42'


def test_the_streamed_path_captures_the_gateway_session(monkeypatch):
    _patch_opener(monkeypatch, _Resp(_sse_body(), ctype='text/event-stream'))
    monkeypatch.setenv('ROUTER_PROXY_IDLE_TIMEOUT_S', '30')
    status, payload = rsrv._proxy_upstream_default('/v1/chat/completions', {'model': 'm'}, {})
    assert status == 200
    assert payload['choices'][0]['message']['content'] == 'hi'
    assert payload['_router_hermes_session_id'] == 'hermes-sess-42'


def test_an_absent_header_leaves_no_fabricated_session(monkeypatch):
    _patch_opener(monkeypatch, _Resp(json.dumps({'ok': True}).encode(), session=None))
    monkeypatch.setenv('ROUTER_PROXY_STREAM_HOPS', '0')
    _, payload = rsrv._proxy_upstream_default('/v1/chat/completions', {'model': 'm'}, {})
    assert '_router_hermes_session_id' not in payload


# ---------- the envelope names it, the body does not ----------

def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m, 'usd_1m': 0.1}
                      for i, (p, m) in enumerate(pairs)], 'sort': 'price'}


def test_the_row_and_the_envelope_both_name_the_session_and_the_body_does_not(monkeypatch, tmp_path):
    ledger = tmp_path / 'outcomes.jsonl'
    ledger.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(ledger))
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 3}, 'complexity_sig': 'sig', 'profile_id': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'ok'}}],
                     '_router_hermes_session_id': 'hermes-sess-42',
                     'usage': {'prompt_tokens': 100, 'completion_tokens': 5}}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {}, upstream=upstream)

    # the ladder keeps the marker (in-process consumers read it) ...
    assert payload.get('_router_hermes_session_id') == 'hermes-sess-42'
    # ... and the CLIENT surface drops it
    assert '_router_hermes_session_id' not in rsrv._client_body(payload)
    assert '_router' in rsrv._client_body(payload)
    # the envelope names the session
    assert payload['_router']['gateway_session_id'] == 'hermes-sess-42'
    assert payload['_router']['served_by']['model'] == 'm1'
    # the row names it too
    row = [json.loads(l) for l in open(ledger) if l.strip()][0]
    assert row['gateway_session_id'] == 'hermes-sess-42'
    assert row['route_outcome'] == 'served'


def test_a_failed_hop_records_no_invented_session(monkeypatch, tmp_path):
    ledger = tmp_path / 'outcomes.jsonl'
    ledger.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(ledger))
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 3}, 'complexity_sig': 'sig', 'profile_id': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        raise ConnectionError('refused')
    _, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {}, upstream=upstream)
    assert payload['_router']['gateway_session_id'] is None
    row = [json.loads(l) for l in open(ledger) if l.strip()][0]
    assert row['gateway_session_id'] is None and row['failure_reason'] == 'transport-error'


# ---------- the three-way reconciliation ----------

@pytest.fixture()
def session_store(tmp_path):
    """A hermetic stand-in for state.db's sessions table (the shape is the point)."""
    path = tmp_path / 'state.db'
    con = sqlite3.connect(path)
    con.execute('create table sessions (id text primary key, source text, chat_id text, model text)')
    con.execute("insert into sessions values ('hermes-sess-42','telegram','-100','glm-5.3-flash')")
    con.commit()
    con.close()
    return str(path)


def _reconcile(envelope_session, ledger_row, db_path):
    """The custody check: envelope names a session, the row names the same one, and
    the session store knows it. Any disagreement is a broken chain, not a warning."""
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        found = con.execute('select model from sessions where id = ?', (envelope_session,)).fetchone()
    finally:
        con.close()
    return {
        'envelope_has_session': bool(envelope_session),
        'row_session_matches': ledger_row.get('gateway_session_id') == envelope_session,
        'session_store_knows_it': found is not None,
        'row_session_model': found[0] if found else None,
    }


def test_the_three_artefacts_reconcile(session_store):
    envelope_session = 'hermes-sess-42'
    row = {'gateway_session_id': 'hermes-sess-42', 'tokens_in': 100, 'cost_usd': 0.01,
           'route_outcome': 'served', 'steps': 1}
    got = _reconcile(envelope_session, row, session_store)
    assert got == {'envelope_has_session': True, 'row_session_matches': True,
                   'session_store_knows_it': True, 'row_session_model': 'glm-5.3-flash'}


def test_a_row_naming_a_DIFFERENT_session_is_a_broken_chain(session_store):
    got = _reconcile('hermes-sess-42', {'gateway_session_id': 'someone-else'}, session_store)
    assert got['row_session_matches'] is False


def test_an_unknown_session_is_reported_not_assumed(session_store):
    got = _reconcile('never-seen', {'gateway_session_id': 'never-seen'}, session_store)
    assert got['session_store_knows_it'] is False
    assert got['row_session_model'] is None


def test_a_missing_envelope_session_is_visible(session_store):
    """The pre-fix state: the row was written with a null session, and the
    reconciliation must SAY so rather than quietly pass."""
    got = _reconcile(None, {'gateway_session_id': None}, session_store)
    assert got['envelope_has_session'] is False


def test_the_success_envelope_names_BOTH_sessions(monkeypatch, tmp_path):
    """The row carried both ids while the envelope reported session_id: null —
    a caller could not reconcile without parsing the ledger."""
    ledger = tmp_path / 'outcomes.jsonl'
    ledger.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(ledger))
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 3}, 'complexity_sig': 'sig', 'profile_id': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'ok'}}],
                     '_router_hermes_session_id': 'gw-1'}
    _, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                 {'x-router-session': 'caller-9'}, upstream=upstream)
    router_session = payload['_router']['session_id']
    assert router_session and 'caller-9' in router_session
    assert payload['_router']['gateway_session_id'] == 'gw-1'
    row = [json.loads(l) for l in open(ledger) if l.strip()][0]
    assert row['session_id'] == router_session, 'envelope and row must agree on the session'
    assert row['parent_session_id'] == payload['_router']['parent_session_id']
