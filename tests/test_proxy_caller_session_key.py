"""TR-173 tests: the caller's X-Hermes-Session-Key must land on every proxied row.

Why this exists (measured, not assumed): the scheduler sends its TICK id as
X-Hermes-Session-Key on /v1/responses. The proxy reads it, forwards it upstream
and then DISCARDS it — no ledger row carried any caller key, so a routed call
could not be tied to the tick it served and "what did this task cost" was
unanswerable per task.

Names on the row (do not confuse):
  session_id           — the ROUTER's row identity (x-router-session derived).
  gateway_session_id   — the UPSTREAM Hermes session id (response headers).
  caller_session_key   — THIS field: the caller's own X-Hermes-Session-Key
                         (the per-TICK join key), validated with the same rules
                         as the forwarded header. null = the caller sent no key
                         (or one the gateway itself would reject — never
                         persisted, reason visible in the envelope).

Hermetic: every test points ROUTING_OUTCOMES_FILE / ROUTER_STATE_DIR into
tmp_path; the live ledger and live gates are never touched.
"""
import json
import os
import sys
from contextlib import contextmanager

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv          # noqa: E402
import router_ui_data                 # noqa: E402

TICK_KEY = 'tg:-1003310984808:2'      # tick-key shaped, like the scheduler sends


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated router state + a deterministic one-hop chain. The REAL
    `_proxy_record` runs — the row must come from the live write path, not a
    stub."""
    ledger = tmp_path / 'outcomes.jsonl'
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(ledger))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {
        'chain': [{'hop': 1, 'provider': 'p1', 'model': 'm1', 'usd_1m': 0.25,
                   'in_per_m': 0.05, 'out_per_m': 0.2}],
        'exclusions': [], 'sort': 'price'})
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 3}, 'complexity_sig': 'sig-tr173',
                       'profile_id': None, 'problems': []}))
    return ledger


def _chat_upstream(path, body, headers):
    return 200, {'choices': [{'message': {'content': 'ok'}}],
                 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}


def _responses_upstream(session_id='gw-sess-tr173'):
    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'ok'}}],
                     'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}, session_id
    return upstream


def _rows(ledger):
    if not os.path.exists(ledger):
        return []
    return [json.loads(l) for l in open(ledger, encoding='utf-8') if l.strip()]


# ---------------------------------------------------------------------------
# AC1 + AC2: the key is persisted by the REAL code path and joins the row to
# the tick together with the task facts already on the row.
# ---------------------------------------------------------------------------
def test_hermes_path_row_carries_the_caller_key(env):
    """AC1: _hermes_proxy_chat -> proxy_chat -> _proxy_chat_inner ->
    _proxy_record -> router_outcomes, with the key on the row it wrote."""
    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hello'}, {'X-Hermes-Session-Key': TICK_KEY},
        upstream=_responses_upstream())
    assert status == 200, payload
    served = [r for r in _rows(env) if r.get('route_outcome') == 'served']
    assert served, f'no served row was written: {_rows(env)}'
    assert served[0]['caller_session_key'] == TICK_KEY, served[0]


def test_chat_path_row_carries_the_caller_key_and_joins_the_task(env):
    """AC2: ONE ledger line names the caller/tick key AND the project/task
    facts (parent session, lane, cost) — the join needs no guessing."""
    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'tr173 join probe'}]},
        {'x-hermes-session-key': TICK_KEY,
         'x-router-session': 'sched-tick-tr173'},
        upstream=_chat_upstream)
    assert status == 200, payload
    served = [r for r in _rows(env) if r.get('route_outcome') == 'served']
    assert served, f'no served row was written: {_rows(env)}'
    row = served[0]
    # the join key and the task facts, together on one line:
    assert row['caller_session_key'] == TICK_KEY
    assert row['parent_session_id'] == 'sched-tick-tr173'
    assert row['provider'] == 'p1' and row['model'] == 'm1'
    assert isinstance(row['cost_usd'], float) and row['cost_usd'] > 0
    assert row['source_system'] == 'router-proxy'


# ---------------------------------------------------------------------------
# AC3: no key -> the FIELD exists and is null. Never a placeholder, never ''.
# ---------------------------------------------------------------------------
def test_no_key_row_has_explicit_null_field(env):
    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'tr173 no-key probe'}]},
        {'x-router-session': 'sched-tick-tr173-nok'},
        upstream=_chat_upstream)
    assert status == 200, payload
    served = [r for r in _rows(env) if r.get('route_outcome') == 'served']
    assert served, f'no served row was written: {_rows(env)}'
    row = served[0]
    assert 'caller_session_key' in row, \
        f'the field must exist even when absent from the request: {sorted(row)}'
    assert row['caller_session_key'] is None, row['caller_session_key']


# ---------------------------------------------------------------------------
# AC4: the key is reachable from the web UI flow view — an operator can trace
# a routed call to the tick it served by searching the tick key itself.
# ---------------------------------------------------------------------------
def test_flow_exposes_the_caller_key(env):
    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hello'}, {'X-Hermes-Session-Key': TICK_KEY},
        upstream=_responses_upstream())
    assert status == 200, payload
    out = router_ui_data.flow(TICK_KEY, path=env)
    assert out.get('status') != 404, out
    assert out['caller_session_key'] == TICK_KEY, out.get('caller_session_key')
    assert out['raw']['caller_session_key'] == TICK_KEY


# ---------------------------------------------------------------------------
# AC5: validation parity with the forwarded header. An invalid key is never
# persisted, and the drop is loud.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('bad_key', ['x' * 257, 'bad\rkey', 'bad\nkey', 'bad\x00key'])
def test_invalid_key_is_rejected_on_the_hermes_path(env, bad_key):
    """SOURCE B contract: the gateway would 400 these — so does the proxy,
    BEFORE any row is written (nothing unvalidated ever reaches the ledger)."""
    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hello'}, {'X-Hermes-Session-Key': bad_key},
        upstream=_responses_upstream())
    assert status == 400, (status, payload)
    assert 'session key' in str(payload.get('error', '')).lower(), payload
    assert _rows(env) == [], f'no row may be written for a rejected key: {_rows(env)}'


@pytest.mark.parametrize('bad_key', ['x' * 257, 'bad\rkey'])
def test_invalid_key_on_chat_path_is_dropped_not_persisted_and_reason_visible(env, bad_key):
    """The chat path never validated this header before (fail-open stays), but
    the row must not carry a value the gateway would reject, and the envelope
    must say why."""
    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'tr173 bad-key probe'}]},
        {'x-hermes-session-key': bad_key,
         'x-router-session': 'sched-tick-tr173-bad'},
        upstream=_chat_upstream)
    assert status == 200, (status, payload)          # fail-open: still served
    served = [r for r in _rows(env) if r.get('route_outcome') == 'served']
    assert served and served[0]['caller_session_key'] is None, served
    problems = json.dumps((payload.get('_router') or {}).get('problems') or [])
    assert 'session key' in problems.lower(), payload.get('_router', {}).get('problems')


def test_no_hops_row_carries_the_caller_key(env, monkeypatch):
    """'Every proxied row' includes the 503 nothing-eligible exit."""
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {
        'chain': [],
        'exclusions': [{'hop': 1, 'provider': 'p9', 'model': 'm9',
                        'codes': ['circuit-open'], 'why': ['circuit OPEN']}],
        'sort': 'price'})

    def upstream(*a, **k):
        raise AssertionError('an empty chain must not reach any upstream')

    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hello'}, {'X-Hermes-Session-Key': TICK_KEY}, upstream=upstream)
    assert status == 503, (status, payload)
    no_hops = [r for r in _rows(env) if r.get('route_outcome') == 'no-hops']
    assert no_hops, f'no no-hops row was written: {_rows(env)}'
    assert no_hops[0]['caller_session_key'] == TICK_KEY, no_hops[0]


def test_admission_refusal_row_carries_the_caller_key(env, monkeypatch):
    """The refusal path in proxy_chat also writes a row — and headers ARE in
    scope there, so the key is honestly available and must be persisted."""
    @contextmanager
    def _full():
        raise rsrv._ProxyOverloaded('queue full (test)')
        yield  # pragma: no cover

    monkeypatch.setattr(rsrv, '_admission', _full)
    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'user', 'content': 'tr173 refusal probe'}]},
        {'x-hermes-session-key': TICK_KEY,
         'x-router-session': 'sched-tick-tr173-ref'},
        upstream=_chat_upstream)
    assert status == 429, (status, payload)
    rejected = [r for r in _rows(env) if r.get('route_outcome') == 'rejected']
    assert rejected, f'no refusal row was written: {_rows(env)}'
    assert rejected[0]['caller_session_key'] == TICK_KEY, rejected[0]
    assert (payload.get('_router') or {}).get('caller_session_key') == TICK_KEY
