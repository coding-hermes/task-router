"""TR-120 tests: the mirror must serve REAL OpenAI clients.

A hermes agent turn exposed three mirror defects (live 2026-09-23, proxy :9397):
  1. the client's `stream: true` was forwarded verbatim, so the upstream
     answered SSE, which json.loads cannot parse — the proxy then returned a
     200 whose body had no choices and the client retried on "empty stream";
  2. a non-JSON 2xx body was wrapped as a payload and SERVED (green status,
     garbage answer) instead of advancing the ladder;
  3. a 2xx error envelope (error + no choices) counted as a served hop.
Hermetic: the upstream call is injected, no network.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402


@pytest.fixture()
def proxy_env(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)  # isolated
    return tmp_path


def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m, 'usd_1m': 0.1 * (i + 1),
                       'outcomes': {'stats_fallback': 'unconditioned'}}
                      for i, (p, m) in enumerate(pairs)],
            'sort': 'price'}


# ---------- the stream wish never reaches the upstream ----------

def test_stream_flag_is_stripped_from_the_forwarded_body(proxy_env, monkeypatch):
    """The mirror is buffered: `stream: true` is the CLIENT's wire wish, not an
    instruction the upstream may act on (it turns the hop into SSE the mirror
    cannot meter)."""
    seen = {}
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        seen['stream'] = body.get('stream')
        return 200, {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                                  'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}

    status, payload = rsrv.proxy_chat('/v1/chat/completions',
                                      {'messages': [{'role': 'user', 'content': 'x'}],
                                       'stream': True},
                                      {}, upstream=upstream)
    assert status == 200
    assert seen['stream'] is None  # stripped before the hop
    assert payload['choices'][0]['message']['content'] == 'ok'
    assert payload['_router']['served_by']['tokens_in'] == 10


def test_non_json_2xx_advances_the_ladder_instead_of_being_served(proxy_env, monkeypatch):
    """A 200 whose body is not JSON is NOT an answer. Before the fix the proxy
    wrapped it as `{'error': ...}` and SERVED it — the client reported an empty
    stream while the ladder sat green."""
    calls = {'n': 0}

    def upstream(path, body, headers):
        calls['n'] += 1
        if calls['n'] == 1:
            # simulate an SSE body arriving at json.loads
            raise TypeError  # never reached: injection point is (status, payload)
        return 200, {'choices': [{'message': {'role': 'assistant', 'content': 'b'}}]}

    # First hop returns the shaped non-JSON payload (as _proxy_upstream_default now does)
    def upstream2(path, body, headers):
        calls['n'] += 1
        if calls['n'] == 1:
            return 200, {'error': 'upstream returned non-JSON', 'raw': 'data: {...'}
        return 200, {'choices': [{'message': {'role': 'assistant', 'content': 'b'}}]}

    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1'), ('p2', 'm2')))
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=upstream2)
    assert status == 200
    assert payload['choices'][0]['message']['content'] == 'b'
    assert calls['n'] == 2
    first = payload['_router']['ladder'][0]
    assert first['outcome'] == 'unservable-2xx'


def test_2xx_error_envelope_is_not_a_served_hop(proxy_env, monkeypatch):
    """A 200 + error envelope + no choices must not count as the served hop."""
    seen = {'n': 0}

    def upstream(path, body, headers):
        seen['n'] += 1
        if seen['n'] == 1:
            return 200, {'error': {'message': 'Invalid gateway API key'}}
        return 200, {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}

    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1'), ('p2', 'm2')))
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=upstream)
    assert status == 200
    assert payload['choices'][0]['message']['content'] == 'ok'
    assert seen['n'] == 2
    assert payload['_router']['ladder'][0]['outcome'] == 'unservable-2xx'


def test_exhausted_ladder_after_unservable_hops_is_not_a_200(proxy_env, monkeypatch):
    """Every hop returns a green 2xx with no choices -> the request must NOT be
    reported as served (was: the garbage was returned with status 200)."""
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=lambda *a: (200, {'error': 'x'}))
    assert status >= 400
    assert payload['_router']['exhausted'] is True


# ---------- SSE synthesis for streaming clients ----------

class _FakeHandler(rsrv.RouterHandler):
    """Drive _send_sse_chat without a socket: capture what would be written."""
    def __init__(self):
        self.buf = b''
        self._headers = []

    def send_response(self, code):
        self._code = code

    def send_header(self, k, v):
        self._headers.append((k, v))

    def end_headers(self):
        pass


class _Sink:
    def __init__(self):
        self.buf = b''

    def write(self, b):
        self.buf += b


def _sse_frames(payload):
    """Run the synthesizer against a stub and return (status, parsed frames)."""
    h = _FakeHandler()
    h.wfile = _Sink()
    h._send_sse_chat(payload)
    text = h.wfile.buf.decode()
    status = h._code
    frames = []
    for line in text.split('\n'):
        line = line.strip()
        if line.startswith('data: '):
            data = line[6:]
            frames.append(data)
    return status, frames


def test_sse_synthesis_emits_role_content_finish_done():
    payload = {'id': 'chatcmpl-x', 'object': 'chat.completion', 'created': 5,
               'model': 'm1',
               'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ROUTER-OK'},
                            'finish_reason': 'stop'}],
               'usage': {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}}
    status, frames = _sse_frames(payload)
    assert status == 200
    assert frames[-1] == '[DONE]'
    chunks = [json.loads(f) for f in frames[:-1]]
    assert all(c['object'] == 'chat.completion.chunk' for c in chunks)
    assert chunks[0]['choices'][0]['delta'] == {'role': 'assistant'}
    assert chunks[1]['choices'][0]['delta']['content'] == 'ROUTER-OK'
    assert chunks[2]['choices'][0]['delta'] == {}
    assert chunks[2]['choices'][0]['finish_reason'] == 'stop'
    # the usage block rides through so the client sees the meters
    assert chunks[3]['usage']['total_tokens'] == 14


def test_sse_synthesis_of_an_error_keeps_the_reason_visible():
    status, frames = _sse_frames({'error': 'ladder exhausted', '_router': {'exhausted': True}})
    assert status == 200  # SSE wire shape; the reason is INSIDE the frame
    assert frames[-1] == '[DONE]'
    body = json.loads(frames[0])
    assert body['error'] == 'ladder exhausted'


# ---------- the streaming request still meters ----------

def test_streaming_request_writes_a_measured_row(proxy_env, monkeypatch):
    rows = []
    monkeypatch.setattr(rsrv, '_proxy_record',
                        lambda *a, **k: rows.append(k))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        assert body.get('stream') is None
        return 200, {'choices': [{'message': {'role': 'assistant', 'content': 'hi'}}],
                     'usage': {'prompt_tokens': 100, 'completion_tokens': 7}}

    status, payload = rsrv.proxy_chat('/v1/chat/completions',
                                      {'messages': [{'role': 'user', 'content': 'x'}],
                                       'stream': True},
                                      {'x-router-session': 'sess-1'}, upstream=upstream)
    assert status == 200
    assert payload['_router']['served_by']['tokens_in'] == 100
    assert payload['_router']['served_by']['cost_usd'] is not None
    assert rows and rows[0].get('tokens_in') == 100 and rows[0].get('cost_usd') is not None