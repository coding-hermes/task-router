"""TR-138: a long-running turn must survive on an IDLE budget, proven on the wire.

Measured 2026-09-24: a real long prompt through :9391 died at exactly 180.1s x 3
hops (502 after 553s) because the hop budget was a WALL clock. Each hop was alive
— the gateway was emitting tool progress and `: keepalive` every 10s — and was
killed for being slow. "Slow but alive" and "dead" are different facts and the
ladder must be able to tell them apart.

These contracts pin the mechanism: the hop asks the gateway to stream, progress
(including keepalives) resets the idle watch, a hop may run far longer than the
old wall budget as long as it keeps producing events, and a hop that goes quiet
dies with an idle reason code rather than a transport one.
"""
import io
import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402


def _sse(*chunks):
    """Build an SSE body from dicts (each becomes one data: frame + blank line)."""
    out = []
    for c in chunks:
        out.append('data: ' + json.dumps(c) + '\n\n')
    return ''.join(out).encode()


def _stream_body(*texts, keepalives=0):
    frames = []
    for i, t in enumerate(texts):
        for _ in range(keepalives):
            frames.append(': keepalive\n\n')
        frames.append('data: ' + json.dumps(
            {'model': 'gw-model', 'choices': [{'index': 0, 'delta': {'content': t}}]}) + '\n\n')
    frames.append('data: ' + json.dumps(
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
         'usage': {'prompt_tokens': 11, 'completion_tokens': 3}}) + '\n\n')
    frames.append('data: [DONE]\n\n')
    return ''.join(frames).encode()


class _StreamResp(io.BytesIO):
    """A urlopen's response double that answers readline() for the SSE reader."""

    def __init__(self, body, ctype='text/event-stream'):
        super().__init__(body)
        self.status = 200
        self._ctype = ctype
        self.headers = {'Content-Type': self._ctype}

        class _H(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)
        self.headers = _H(self.headers)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------- the assembler ----------

def test_a_stream_is_assembled_into_the_buffered_answer_the_client_asked_for():
    body = _stream_body('Hel', 'lo ', 'world')
    payload = rsrv._collect_openai_stream(io.StringIO(body.decode()))
    assert payload['choices'][0]['message']['content'] == 'Hello world'   # deltas concatenate
    assert payload['choices'][0]['message']['role'] == 'assistant'
    assert payload['choices'][0]['finish_reason'] == 'stop'
    assert payload['usage'] == {'prompt_tokens': 11, 'completion_tokens': 3}
    assert payload['model'] == 'gw-model'
    assert payload['_router_stream']['streamed'] is True
    assert payload['_router_stream']['events'] >= 3


def test_keepalives_are_activity_not_content():
    payload = rsrv._collect_openai_stream(
        io.StringIO(_stream_body('a', keepalives=3).decode()))
    assert payload['choices'][0]['message']['content'] == 'a'
    assert 'keepalive' not in payload['choices'][0]['message']['content']


def test_reasoning_deltas_are_kept_separate_from_the_answer():
    frames = ('data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': 'think'}}]}) + '\n\n'
              'data: ' + json.dumps({'choices': [{'delta': {'content': 'answer'}}]}) + '\n\n'
              'data: [DONE]\n\n')
    payload = rsrv._collect_openai_stream(io.StringIO(frames))
    msg = payload['choices'][0]['message']
    assert msg['content'] == 'answer' and msg['reasoning_content'] == 'think'


def test_a_responses_shape_stream_also_assembles():
    frames = ('data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': 'x'}) + '\n\n'
              'data: ' + json.dumps({'type': 'response.completed', 'response': {
                  'model': 'm', 'usage': {'input_tokens': 5, 'output_tokens': 1},
                  'output': [{'content': [{'text': 'final'}]}]}}) + '\n\n')
    payload = rsrv._collect_openai_stream(io.StringIO(frames), path='/v1/responses')
    assert payload['choices'][0]['message']['content'] == 'final'
    assert payload['usage'] == {'input_tokens': 5, 'output_tokens': 1}


# ---------- the budget rule ----------

def test_the_idle_budget_is_separate_from_the_wall_ceiling(monkeypatch):
    monkeypatch.delenv('ROUTER_PROXY_IDLE_TIMEOUT_S', raising=False)
    monkeypatch.delenv('ROUTER_PROXY_HOP_WALL_S', raising=False)
    assert rsrv._proxy_idle_budget_s() == rsrv._hermes_idle_timeout_s()
    assert rsrv._proxy_wall_ceiling_s() > rsrv._proxy_idle_budget_s(), \
        'the wall is a backstop, never the primary budget'
    monkeypatch.setenv('ROUTER_PROXY_IDLE_TIMEOUT_S', '900')
    monkeypatch.setenv('ROUTER_PROXY_HOP_WALL_S', '9999')
    assert rsrv._proxy_idle_budget_s() == 900.0
    assert rsrv._proxy_wall_ceiling_s() == 9999.0
    monkeypatch.setenv('ROUTER_PROXY_IDLE_TIMEOUT_S', 'garbage')
    assert rsrv._proxy_idle_budget_s() == rsrv._hermes_idle_timeout_s()


def test_a_turn_that_runs_LONGER_than_the_old_wall_budget_still_completes(monkeypatch):
    """The defect, inverted: total duration no longer decides death.

    Scale: idle budget 0.4s, an event every ~0.08s for ~0.8s total. Under the old
    180s wall semantics this is the same mechanism at 1/200th scale — the turn
    stays alive because each event RESETS the watch, even though its total runtime
    is twice the budget.
    """
    monkeypatch.setenv('ROUTER_PROXY_IDLE_TIMEOUT_S', '0.4')
    frames = []
    for i in range(10):
        frames.append('data: ' + json.dumps({'choices': [{'delta': {'content': str(i)}}]}) + '\n\n')
    frames.append('data: [DONE]\n\n')

    class _Slow(io.StringIO):
        def __init__(self, text):
            super().__init__(text)
            self._delay = 0.08

        def readline(self, *a):
            time.sleep(self._delay)
            return super().readline(*a)

    watch = rsrv._HermesIdleWatch(0.4)   # exactly as production wires it
    started = time.monotonic()
    payload = rsrv._collect_openai_stream(rsrv._sse_lines(_Slow(''.join(frames)), watch),
                                          watch=watch)
    elapsed = time.monotonic() - started
    assert elapsed > 0.4, 'the turn really did outlast the budget'
    assert payload['choices'][0]['message']['content'] == '0123456789'


def test_a_stalled_hop_dies_with_an_IDLE_reason_not_a_transport_one(monkeypatch):
    """And the TR-137 classifier must read it as idle-timeout."""
    monkeypatch.setenv('ROUTER_PROXY_IDLE_TIMEOUT_S', '0.15')

    class _Stall(io.StringIO):
        def readline(self, *a):
            time.sleep(0.4)          # silence beyond the budget
            return super().readline(*a)

    watch = rsrv._HermesIdleWatch(0.15)
    with pytest.raises(rsrv._HermesIdleTimeout) as err:
        rsrv._collect_openai_stream(rsrv._sse_lines(_Stall('data: {"choices":[]}\n\n'), watch),
                                    watch=watch)
    reason, _ = rsrv._classify_hop_failure(exc=err.value)
    assert reason == 'idle-timeout'


# ---------- the upstream path ----------

def test_the_upstream_streams_when_the_hop_asked_for_it(monkeypatch):
    monkeypatch.setenv('ROUTER_PROXY_IDLE_TIMEOUT_S', '30')
    monkeypatch.setenv('ROUTER_PROXY_UPSTREAM', 'http://127.0.0.1:9/')  # never used
    seen = {}

    def fake_opener(req, timeout=None):
        seen['body'] = json.loads(req.data.decode())
        seen['timeout'] = timeout
        return _StreamResp(_stream_body('streamed answer'))

    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', fake_opener)
    status, payload = rsrv._proxy_upstream_default(
        '/v1/chat/completions', {'model': 'm', 'messages': [], 'stream': True}, {})
    assert status == 200
    assert seen['body']['stream'] is True, 'the hop must ask the upstream to stream'
    assert payload['choices'][0]['message']['content'] == 'streamed answer'
    assert payload['_router_stream']['streamed'] is True
    assert seen['timeout'] >= rsrv._proxy_wall_ceiling_s() or seen['timeout'] >= 30


def test_an_upstream_that_answers_JSON_is_never_treated_as_a_stream(monkeypatch):
    """Asking for a stream does not guarantee one. Treating a JSON body as SSE
    produced an EMPTY answer — the chat-shape regression guard caught it."""
    monkeypatch.setenv('ROUTER_PROXY_HOP_TIMEOUT_S', '180')
    monkeypatch.setenv('ROUTER_PROXY_STREAM_HOPS', '0')
    seen = {}

    def fake_opener(req, timeout=None):
        seen['timeout'] = timeout
        return _StreamResp(json.dumps({'choices': [{'message': {'content': 'buffered'}}]}).encode(),
                          ctype='application/json')

    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', fake_opener)
    status, payload = rsrv._proxy_upstream_default('/v1/chat/completions', {'model': 'm'}, {})
    assert status == 200 and payload['choices'][0]['message']['content'] == 'buffered'
    assert seen['timeout'] == 180.0, 'a buffered hop keeps its bounded wall'
    assert '_router_stream' not in payload


def test_a_non_chat_path_keeps_the_buffered_wall(monkeypatch):
    """The stream request is chat-completions-only: other paths are untouched."""
    monkeypatch.setenv('ROUTER_PROXY_HOP_TIMEOUT_S', '180')
    seen = {}

    def fake_opener(req, timeout=None):
        seen['timeout'] = timeout
        seen['body'] = json.loads(req.data.decode())
        return _StreamResp(json.dumps({'ok': True}).encode(), ctype='application/json')

    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', fake_opener)
    rsrv._proxy_upstream_default('/v1/embeddings', {'model': 'm'}, {})
    assert seen['timeout'] == 180.0
    assert seen['body'].get('stream') is None


# ---------- the ladder ----------

def test_the_ladder_body_is_UNCHANGED_and_the_stream_facts_land_in_the_envelope(monkeypatch):
    """TR-120's contract survives TR-138: the ladder still strips the client's
    stream wish, so an injected/provider upstream sees exactly today's body. The
    gateway CALLER is what asks to stream, and the live-stream facts are routing
    evidence — they go in the ladder, never in the model's answer body."""
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1_CODING', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)
    monkeypatch.setattr(rsrv, '_load_provider_routing', lambda: {})
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {
        'chain': [{'hop': 1, 'provider': 'gw', 'model': 'm1', 'usd_1m': 0.1}], 'sort': 'price'})
    bodies = []

    def upstream(path, body, headers):
        bodies.append(dict(body))
        return 200, {'choices': [{'message': {'content': 'ok'}}],
                     '_router_stream': {'events': 4, 'streamed': True,
                                        'idle_budget_s': 300.0, 'wall_ceiling_s': 3600.0}}

    _, payload = rsrv.proxy_chat('/v1/chat/completions',
                                 {'messages': [], 'stream': True}, {}, upstream=upstream)
    assert bodies[0].get('stream') is None, "TR-120: the client's wish is stripped, not forwarded"
    assert payload['_router']['ladder'][0]['stream']['events'] == 4
    assert '_router_stream' not in payload, 'never leak routing evidence into the answer body'
    assert payload['_router']['stream_hops'] is True
    assert payload['_router']['idle_budget_s'] and payload['_router']['wall_ceiling_s']


def test_streaming_hops_can_be_switched_off(monkeypatch):
    monkeypatch.setenv('ROUTER_PROXY_STREAM_HOPS', '0')
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)
    monkeypatch.setattr(rsrv, '_load_provider_routing', lambda: {})
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {
        'chain': [{'hop': 1, 'provider': 'gw', 'model': 'm1', 'usd_1m': 0.1}], 'sort': 'price'})
    bodies = []

    def upstream(path, body, headers):
        bodies.append(dict(body))
        return 200, {'choices': [{'message': {'content': 'ok'}}]}

    _, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {}, upstream=upstream)
    assert bodies[0].get('stream') is None
    assert payload['_router']['stream_hops'] is False   # disclosed, so the mode is visible
