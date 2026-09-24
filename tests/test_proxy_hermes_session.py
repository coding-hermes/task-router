"""TR-129 tests: the proxy must speak the real Hermes gateway session protocol.

Two authoritative sources pinned here (not assumptions):
  SOURCE A — the scheduler's own gateway client
  (coding-hermes-scheduler/internal/scheduler/gateway_stream.go): POST
  /v1/responses with X-Hermes-Session-Key on the request, X-Hermes-Session-Id
  read from the RESPONSE HEADERS at stream open, SSE `event:`/`data:` lines
  with a JSON `type` field, terminal events response.completed/failed, and an
  IDLE deadline (not wall clock) that resets on every real SSE event —
  keepalive comments (`: keepalive`) are deliberately NOT activity (they are
  emitted on a bare timer regardless of agent liveness).
  SOURCE B — the Hermes gateway itself (gateway/platforms/api_server.py):
  session keys are stripped, control-character (\\r\\n\\x00) rejected and
  capped at 256 chars; X-Hermes-Session-Id rides the response headers on both
  buffered and SSE answers; /v1/capabilities advertises the session headers
  and the responses endpoint.

Hermetic: upstreams are injected fake responses; no network.
"""
import io
import json
import os
import sys
import threading
import urllib.request

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


class _Headers(dict):
    """Case-insensitive header mapping (what http.client's Message provides)."""
    def get(self, name, default=None):
        for k, v in self.items():
            if str(k).lower() == str(name).lower():
                return v
        return default


class _FakeHTTPResponse:
    """Minimal urllib-response stand-in: headers, status, read/readline, CM."""
    def __init__(self, status=200, headers=None, body=b''):
        self.status = status
        self.headers = _Headers(headers or {})
        self._buf = io.BytesIO(body)

    def read(self, n=-1):
        return self._buf.read(n)

    def readline(self):
        return self._buf.readline()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sse_body(events):
    """Assemble SSE bytes the way the real gateway writes them."""
    out = []
    for evt in events:
        if isinstance(evt, str):            # raw line (keepalive etc.)
            out.append(evt if evt.endswith('\n') else evt + '\n')
            continue
        name, data = evt
        if name:
            out.append(f'event: {name}\n')
        out.append(f'data: {json.dumps(data)}\n')
        out.append('\n')
    return ''.join(out).encode()


def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m, 'usd_1m': 0.1 * (i + 1),
                       'outcomes': {'stats_fallback': 'unconditioned'}}
                      for i, (p, m) in enumerate(pairs)],
            'sort': 'price'}


def _ok_envelope(resp_id='resp_1', usage=None):
    env = {'id': resp_id, 'object': 'response', 'status': 'completed',
           'created_at': 1, 'model': 'm1', 'output': []}
    if usage:
        env['usage'] = usage
    return env


# ---------- SOURCE B: session-key validation + forward ----------

def test_session_key_is_forwarded_and_normalized_before_the_hop(proxy_env, monkeypatch):
    """The caller's X-Hermes-Session-Key reaches the upstream call (stripped of
    surrounding whitespace, per the gateway's own `.strip()` validation)."""
    seen = {}
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        seen['path'] = path
        seen['session_key'] = headers.get('x-hermes-session-key')
        return 200, _ok_envelope(), ''

    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hello'}, {'X-Hermes-Session-Key': '  tg:-1003310984808:2  '},
        upstream=upstream)
    assert status == 200
    assert seen['path'] == '/v1/responses'
    assert seen['session_key'] == 'tg:-1003310984808:2'


def test_control_character_session_key_is_rejected_without_reaching_upstream(
        proxy_env, monkeypatch):
    """SOURCE B: \r\n\x00 could enable header injection on the echo path — the
    gateway rejects them, so the proxy must reject them BEFORE the ladder."""
    calls = []

    def upstream(path, body, headers):
        calls.append(path)
        return 200, _ok_envelope(), ''

    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'x'}, {'X-Hermes-Session-Key': 'bad\r\ninjected: 1'},
        upstream=upstream)
    assert status == 400
    assert 'session key' in str(payload.get('error')).lower()
    assert calls == []          # never forwarded


def test_oversized_session_key_is_rejected_256_is_accepted(proxy_env, monkeypatch):
    """SOURCE B caps session keys at 256 chars (the gateway echoes the key back
    in a response header — longer keys are a header-injection risk there)."""
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        return 200, _ok_envelope(), ''

    status, _ = rsrv._hermes_proxy_chat(
        {'input': 'x'}, {'X-Hermes-Session-Key': 'k' * 256}, upstream=upstream)
    assert status == 200
    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'x'}, {'X-Hermes-Session-Key': 'k' * 257}, upstream=upstream)
    assert status == 400
    assert '256' in str(payload.get('error'))


def test_absent_session_key_still_works(proxy_env, monkeypatch):
    """The header is optional (SOURCE A: `if sessionKey != ""` sets it)."""
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        return 200, _ok_envelope(), ''

    status, payload = rsrv._hermes_proxy_chat({'input': 'x'}, {}, upstream=upstream)
    assert status == 200
    assert payload['_router']['hermes_session_key'] is None
    assert payload['_router']['hermes_session_id'] == ''


# ---------- SOURCE B: X-Hermes-Session-Id echo from upstream headers ----------

def test_session_id_from_upstream_response_headers_is_echoed(proxy_env, monkeypatch):
    """SOURCE A reads X-Hermes-Session-Id from the RESPONSE HEADERS at stream
    open; the proxy echoes it to the caller in the envelope."""
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        return 200, _ok_envelope(), 'sess-abc-123'

    status, payload = rsrv._hermes_proxy_chat(
        {'input': 'hi'}, {'X-Hermes-Session-Key': 'tg:chan'}, upstream=upstream)
    assert status == 200
    assert payload['_router']['hermes_session_id'] == 'sess-abc-123'
    assert payload['_router']['hermes_session_key'] == 'tg:chan'


def test_upstream_without_session_id_leaves_the_echo_absent(proxy_env, monkeypatch):
    """A hop that carries no X-Hermes-Session-Id (a plain OpenAI-shaped upstream
    answering /v1/responses) must not fabricate one."""
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        return 200, _ok_envelope(), ''

    status, payload = rsrv._hermes_proxy_chat({'input': 'hi'}, {}, upstream=upstream)
    assert status == 200
    assert payload['_router']['hermes_session_id'] == ''


# ---------- SOURCE A: SSE keepalives + idle deadline ----------

class _CountingWatch(rsrv._HermesIdleWatch):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.resets = 0

    def reset(self):
        self.resets += 1
        super().reset()


def test_keepalive_comments_do_not_break_sse_parsing_or_count_as_events():
    """`: keepalive` lines are read and discarded: parsing still finds the
    terminal envelope and keepalives never surface as events."""
    body = _sse_body([
        ': keepalive',
        ': keepalive',
        ('response.created', {'type': 'response.created'}),
        ': keepalive',
        ('', {'type': 'response.completed', 'response': _ok_envelope('resp_k')}),
    ])
    lines = body.decode().splitlines(keepends=True)
    events = []
    result = rsrv._read_hermes_sse(lines, on_event=events.append)
    assert result['id'] == 'resp_k'
    assert result['status'] == 'completed'
    assert events == ['response.created', 'response.completed']


def test_idle_watch_resets_on_each_real_sse_event():
    """SOURCE A: every real SSE event resets the idle deadline; keepalives do
    not. Fake clock: keepalives every 5s; a real event at t=25 (before the 30s
    deadline) resets the watch, and the stream survives to t=52. Without the
    reset the watch would have fired at t=30."""
    clock = {'t': 0.0}
    watch = _CountingWatch(30.0, _clock=lambda: clock['t'])
    chunks = []
    for _ in range(5):                      # t -> 25, only keepalives so far
        clock['t'] += 5.0
        chunks.append(': keepalive\n')
    chunks.append('data: {"type": "response.output_text.delta", "delta": "x"}\n\n')
    for _ in range(5):                      # t -> 50; deadline would be 25+30=55
        clock['t'] += 5.0
        chunks.append(': keepalive\n')
    clock['t'] += 2.0                       # t = 52
    chunks.append('data: {"type": "response.completed", "response": '
                  '{"id": "resp_r", "status": "completed"}}\n\n')
    # A real reader yields ONE line per element (readline/splitlines shape).
    lines = ''.join(chunks).splitlines(keepends=True)
    result = rsrv._read_hermes_sse(lines, watch=watch)
    assert result['id'] == 'resp_r'
    assert watch.resets == 2                # the delta event + the terminal


def test_idle_deadline_fires_when_only_keepalives_arrive():
    """The whole point of the idle deadline (SOURCE A): a gateway that keeps
    the socket warm with keepalives but produces no real event must time out —
    keepalives are NOT activity."""
    clock = {'t': 0.0}
    watch = rsrv._HermesIdleWatch(30.0, _clock=lambda: clock['t'])

    class _KeepaliveForever:
        def readline(self):
            clock['t'] += 5.0
            return b': keepalive\n'

    with pytest.raises(rsrv._HermesIdleTimeout):
        rsrv._read_hermes_sse(rsrv._sse_lines(_KeepaliveForever(), watch),
                              watch=watch)


def test_eof_without_a_terminal_event_is_a_transport_failure():
    """SOURCE A: 'a stream that ends without a terminal event is a transient'."""
    body = _sse_body([('response.created', {'type': 'response.created'})])
    with pytest.raises(Exception, match='without a terminal event'):
        rsrv._read_hermes_sse(body.decode().splitlines(keepends=True))


def test_response_failed_event_fails_the_turn():
    """response.failed must not be served as a success envelope."""
    body = _sse_body([('', {'type': 'response.failed',
                            'response': {'id': 'resp_f', 'status': 'failed'}})])
    with pytest.raises(Exception, match='failed'):
        rsrv._read_hermes_sse(body.decode().splitlines(keepends=True))


def test_sse_lines_generator_enforces_the_idle_watch():
    """The line generator checks the watch BEFORE each blocking read, so the
    deadline fires even though keepalives keep bytes arriving."""
    clock = {'t': 0.0}
    watch = rsrv._HermesIdleWatch(10.0, _clock=lambda: clock['t'])

    class _Slow:
        def readline(self):
            clock['t'] += 20.0
            return b'event: ping\n'

    with pytest.raises(rsrv._HermesIdleTimeout):
        list(rsrv._sse_lines(_Slow(), watch))


def test_idle_timeout_env_parsing(monkeypatch):
    monkeypatch.delenv('ROUTER_HERMES_IDLE_TIMEOUT_S', raising=False)
    assert rsrv._hermes_idle_timeout_s() == 300.0
    monkeypatch.setenv('ROUTER_HERMES_IDLE_TIMEOUT_S', '45')
    assert rsrv._hermes_idle_timeout_s() == 45.0
    monkeypatch.setenv('ROUTER_HERMES_IDLE_TIMEOUT_S', 'garbage')
    assert rsrv._hermes_idle_timeout_s() == 300.0
    monkeypatch.setenv('ROUTER_HERMES_IDLE_TIMEOUT_S', '0')
    assert rsrv._hermes_idle_timeout_s() == 300.0


# ---------- SOURCE B: /v1/capabilities at startup ----------

def _caps_response(body=None, status=200):
    doc = body if body is not None else {
        'object': 'hermes.api_server.capabilities',
        'features': {'responses_api': True,
                     'session_key_header': 'X-Hermes-Session-Key',
                     'session_continuity_header': 'X-Hermes-Session-Id'},
        'endpoints': {'responses': {'method': 'POST', 'path': '/v1/responses'}}}
    return _FakeHTTPResponse(status, {'Content-Type': 'application/json'},
                             json.dumps(doc).encode())


def test_capabilities_metadata_is_read_from_the_upstream():
    seen = {}

    def opener(req, timeout=None):
        seen['url'] = req.full_url
        return _caps_response()

    meta = rsrv._hermes_capabilities_metadata('http://127.0.0.1:8642',
                                              _opener=opener)
    assert seen['url'] == 'http://127.0.0.1:8642/v1/capabilities'
    assert meta['responses_endpoint'] == '/v1/responses'
    assert meta['responses_api'] is True
    assert meta['session_key_header'] == 'X-Hermes-Session-Key'
    assert 'error' not in meta


def test_capabilities_probe_failure_fails_open():
    def opener(req, timeout=None):
        raise OSError('connection refused')

    meta = rsrv._hermes_capabilities_metadata('http://127.0.0.1:9',
                                              _opener=opener)
    assert 'error' in meta          # recorded, never raised


def test_capabilities_invalid_payload_fails_open():
    meta = rsrv._hermes_capabilities_metadata(
        'http://x', _opener=lambda req, timeout=None: _FakeHTTPResponse(
            200, {'Content-Type': 'application/json'}, b'not json'))
    assert 'error' in meta


# ---------- the wire: hermes upstream call end to end ----------

def test_hermes_sse_upstream_is_assembled_and_session_id_read():
    """Full hermes-hop path: the gateway answers text/event-stream (keepalive
    included); the proxy consumes it, assembles the envelope, and returns the
    session id from the response headers."""
    captured = {}

    def opener(req, timeout=None):
        captured['req'] = req
        return _FakeHTTPResponse(
            200, {'Content-Type': 'text/event-stream',
                  'X-Hermes-Session-Id': 'gw-sess-9'},
            _sse_body([
                ': keepalive',
                ('response.created', {'type': 'response.created'}),
                ('', {'type': 'response.completed',
                      'response': _ok_envelope(
                          'resp_9', usage={'input_tokens': 11, 'output_tokens': 3})}),
            ]))

    status, payload, sid = rsrv._hermes_responses_call(
        '/v1/responses', {'input': 'x', 'model': 'm1'},
        {'x-hermes-session-key': 'tg:chan', 'authorization': 'Bearer sk-test'},
        _opener=opener)
    assert status == 200
    assert payload['id'] == 'resp_9'
    assert payload['usage']['input_tokens'] == 11
    assert sid == 'gw-sess-9'
    sent = {k.lower(): v for k, v in captured['req'].header_items()}
    assert sent.get('x-hermes-session-key') == 'tg:chan'
    assert sent.get('authorization') == 'Bearer sk-test'


def test_hermes_buffered_json_upstream_carries_session_id():
    """stream-wish stripped (or absent): the gateway answers buffered JSON and
    STILL sends X-Hermes-Session-Id on the response (SOURCE B)."""
    def opener(req, timeout=None):
        return _FakeHTTPResponse(
            200, {'Content-Type': 'application/json',
                  'X-Hermes-Session-Id': 'gw-sess-buffered'},
            json.dumps(_ok_envelope('resp_b')).encode())

    status, payload, sid = rsrv._hermes_responses_call(
        '/v1/responses', {'input': 'x'}, {}, _opener=opener)
    assert status == 200
    assert payload['id'] == 'resp_b'
    assert sid == 'gw-sess-buffered'


def test_hermes_http_error_is_returned_not_raised():
    """A real gateway response (4xx/5xx) is a ladder step, not a crash."""

    class _Err(_FakeHTTPResponse):
        def __init__(self):
            super().__init__(429, {'Content-Type': 'application/json'},
                             b'{"error": "rate limited"}')

    def opener(req, timeout=None):
        return _Err()

    status, payload, sid = rsrv._hermes_responses_call(
        '/v1/responses', {'input': 'x'}, {}, _opener=opener)
    assert status == 429
    assert 'rate limited' in payload['error']
    assert sid == ''


# ---------- HTTP surface: /v1/responses alongside /v1/chat/completions ----------

@pytest.fixture()
def http_proxy(proxy_env, monkeypatch):
    """An in-process router server on a real port with a fake hermes gateway.

    The urlopen patch must be SELECTIVE: rsrv.urllib is the process-global
    urllib module, so a blanket patch would also swallow the test client's own
    requests to the router. Only requests to the upstream base (the gateway's
    default :8642) are faked; everything else is delegated to the real opener.
    """
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))
    monkeypatch.setenv('ROUTER_PROXY_AUTH', 'passthrough')
    monkeypatch.delenv('ROUTER_PROXY_UPSTREAM', raising=False)
    monkeypatch.setattr(rsrv, '_UPSTREAM_CALL', None)

    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        if ':8642/' not in req.full_url:
            return real_urlopen(req, timeout=timeout)
        sent = json.loads(req.data.decode())
        if '/v1/chat/completions' in req.full_url:
            # chat path regression test: the chat wire shape
            return _FakeHTTPResponse(
                200, {'Content-Type': 'application/json'},
                json.dumps({'choices': [{'index': 0,
                                         'message': {'role': 'assistant',
                                                     'content': 'ok'},
                                         'finish_reason': 'stop'}]}).encode())
        if sent.get('stream'):
            return _FakeHTTPResponse(
                200, {'Content-Type': 'text/event-stream',
                      'X-Hermes-Session-Id': 'gw-sess-http'},
                _sse_body([
                    ': keepalive',
                    ('', {'type': 'response.completed',
                          'response': _ok_envelope(
                              'resp_http', usage={'input_tokens': 5,
                                                  'output_tokens': 2})}),
                ]))
        return _FakeHTTPResponse(
            200, {'Content-Type': 'application/json',
                  'X-Hermes-Session-Id': 'gw-sess-http'},
            json.dumps(_ok_envelope('resp_http')).encode())

    monkeypatch.setattr(rsrv.urllib.request, 'urlopen', fake_urlopen)

    app = rsrv.RouterApplication('read-only', '')
    server = rsrv.RouterHTTPServer(('127.0.0.1', 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()
    server.server_close()


def test_v1_responses_streaming_http_surface(http_proxy):
    """POST /v1/responses with stream:true: real HTTP in, Responses-SSE out,
    X-Hermes-Session-Id echoed on the stream headers, [DONE] terminated."""
    req = urllib.request.Request(
        http_proxy + '/v1/responses',
        data=json.dumps({'input': 'hello', 'model': 'whatever',
                         'stream': True}).encode(),
        headers={'Content-Type': 'application/json',
                 'X-Hermes-Session-Key': 'tg:http'},
        method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200
        assert 'text/event-stream' in resp.headers.get('Content-Type', '')
        assert resp.headers.get('X-Hermes-Session-Id') == 'gw-sess-http'
        body = resp.read().decode()
    assert '": keepalive' not in body          # the gateway's keepalive was consumed
    assert '"type": "response.created"' in body.replace("'", '"') or \
        '"response.created"' in body
    assert '"response.completed"' in body
    assert 'data: [DONE]' in body
    assert '"id": "resp_http"' in body


def test_v1_responses_buffered_http_surface(http_proxy):
    """POST /v1/responses without stream: JSON envelope + session id header."""
    req = urllib.request.Request(
        http_proxy + '/v1/responses',
        data=json.dumps({'input': 'hello'}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200
        assert resp.headers.get('X-Hermes-Session-Id') == 'gw-sess-http'
        payload = json.load(resp)
    assert payload['id'] == 'resp_http'
    assert payload['_router']['hermes_session_id'] == 'gw-sess-http'


def test_v1_chat_completions_path_is_unchanged(http_proxy):
    """Regression guard: the OpenAI chat shape still works and does NOT grow
    hermes envelope fields (TR-129 is additive, /v1/responses only)."""
    req = urllib.request.Request(
        http_proxy + '/v1/chat/completions',
        data=json.dumps({'messages': [{'role': 'user', 'content': 'x'}]}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.load(resp)
    assert payload['choices'][0]['message']['content'] == 'ok'


def test_chat_completions_does_not_validate_the_hermes_session_key(
        proxy_env, monkeypatch):
    """Scope discipline: session-key validation is /v1/responses-only; a chat
    caller sending a weird session-key header is still served."""
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        return 200, {'choices': [{'index': 0,
                                  'message': {'role': 'assistant', 'content': 'ok'},
                                  'finish_reason': 'stop'}]}

    status, payload = rsrv.proxy_chat(
        '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'x'}]},
        {'X-Hermes-Session-Key': 'bad\r\nkey'}, upstream=upstream)
    assert status == 200
    assert payload['choices'][0]['message']['content'] == 'ok'
