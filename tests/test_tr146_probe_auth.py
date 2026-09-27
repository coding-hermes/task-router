"""TR-146: the upstream capabilities probe must authenticate like the fleet.

Measured (base): `_hermes_capabilities_metadata` builds its request with only a
User-Agent — the gateway's /v1/capabilities requires a Bearer gateway
credential (the fleet's own tools read GATEWAY_API_KEY / HERMES_API_KEY /
API_SERVER_KEY from the environment or ~/.hermes/.env), so the probe answered
401 at every boot and capability discovery has been empty since day one
(docs/proxy-readiness-report-2026-09-26.html, TR-146 open decision).

Rules under test:
- the credential is resolved at runtime from the configured sources (env first,
  then ~/.hermes/.env), never hardcoded, and never logged or echoed into a
  response body or error string;
- where the credential is absent the probe records EXACTLY "no gateway
  credential configured" and the proxy keeps serving (fail-open, advisory);
- a successful probe populates the discovery payload (source: live on
  GET /v1/capabilities; is_hermes_gateway / session_key_header /
  responses_endpoint non-null);
- the token never appears in a response body, log line, or error field, even
  after a forced failure.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv  # noqa: E402

SECRET = 'tr146-secret-token-abc123'

CAPS_DOC = {
    'object': 'hermes.api_server.capabilities',
    'features': {'responses_api': True,
                 'session_key_header': 'X-Hermes-Session-Key',
                 'session_continuity_header': 'X-Hermes-Session-Id'},
    'endpoints': {'responses': {'method': 'POST', 'path': '/v1/responses'}},
}


@pytest.fixture()
def clean_credential_env(monkeypatch, tmp_path):
    """No credential anywhere: the credential env names cleared and the hermes
    .env file pointed at a nonexistent path so no test reads the operator's
    real secret. (getattr so the suite still RUNS — and fails on behavior —
    against the unfixed base, proving the RED.)"""
    for name in getattr(rsrv, 'GATEWAY_CREDENTIAL_ENV_VARS', ()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(rsrv, '_GATEWAY_ENV_FILE',
                        str(tmp_path / 'does-not-exist.env'), raising=False)
    yield


@pytest.fixture()
def credential_from_env(clean_credential_env, monkeypatch):
    monkeypatch.setenv(rsrv.GATEWAY_CREDENTIAL_ENV_VARS[0], SECRET)
    yield


@pytest.fixture()
def credential_from_env_file(clean_credential_env, tmp_path, monkeypatch):
    env_file = tmp_path / 'hermes.env'
    env_file.write_text('# comment\nHERMES_API_KEY="{}"\n'.format(SECRET))
    monkeypatch.setattr(rsrv, '_GATEWAY_ENV_FILE', str(env_file))
    yield


@pytest.fixture()
def ro_server():
    return rsrv.RouterApplication(mode='read-only', edit_key='k')


class _Resp:
    """Context-manager response like urllib's — the probe uses `with`."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _capturing_opener(captured, status=200, body=None):
    def opener(req, timeout=None):
        captured['url'] = req.full_url
        captured['auth'] = req.get_header('Authorization')
        captured['headers'] = {k.lower(): v for k, v in req.header_items()}
        return _Resp(CAPS_DOC if body is None else body)

    return opener


# ---------- the credential is sent ----------

def test_probe_sends_bearer_from_env(credential_from_env):
    captured = {}
    meta = rsrv._hermes_capabilities_metadata(
        'http://127.0.0.1:8642', _opener=_capturing_opener(captured))
    assert captured['url'].endswith('/v1/capabilities')
    assert captured['auth'] == 'Bearer ' + SECRET
    assert 'error' not in meta
    assert meta['is_hermes_gateway'] is True
    assert meta['session_key_header'] == 'X-Hermes-Session-Key'
    assert meta['responses_endpoint'] == '/v1/responses'


def test_probe_credential_falls_back_to_hermes_env_file(credential_from_env_file):
    captured = {}
    meta = rsrv._hermes_capabilities_metadata(
        'http://127.0.0.1:8642', _opener=_capturing_opener(captured))
    assert captured['auth'] == 'Bearer ' + SECRET
    assert 'error' not in meta


def test_probe_prefers_env_over_env_file(credential_from_env_file, monkeypatch):
    monkeypatch.setenv(rsrv.GATEWAY_CREDENTIAL_ENV_VARS[0], 'env-wins')
    captured = {}
    rsrv._hermes_capabilities_metadata(
        'http://127.0.0.1:8642', _opener=_capturing_opener(captured))
    assert captured['auth'] == 'Bearer env-wins'


# ---------- absent credential: exact sentinel, fail-open ----------

def test_probe_without_credential_records_the_exact_sentinel(clean_credential_env):
    captured = {}
    meta = rsrv._hermes_capabilities_metadata(
        'http://127.0.0.1:8642', _opener=_capturing_opener(captured))
    assert meta == {'error': 'no gateway credential configured'}
    assert captured == {}          # the doomed unauthenticated call is not fired


def test_absent_credential_response_body_is_still_served(ro_server, clean_credential_env,
                                                         monkeypatch):
    """No credential = advisory degrade, the proxy KEEPS SERVING the endpoint."""
    monkeypatch.setattr(rsrv, '_hermes_capabilities_metadata',
                        lambda base, _opener=None:
                        {'error': 'no gateway credential configured'})
    status, payload = ro_server.dispatch('GET', '/v1/capabilities')
    assert status == 200
    assert payload['source'] == 'unavailable'
    assert payload['error'] == 'no gateway credential configured'


# ---------- the 401 arm (injected opener, no live gateway) ----------

def test_probe_401_is_reported_without_the_token(clean_credential_env, monkeypatch):
    monkeypatch.setenv(rsrv.GATEWAY_CREDENTIAL_ENV_VARS[0], SECRET)

    def opener(req, timeout=None):
        raise rsrv.urllib.error.HTTPError(
            req.full_url, 401, 'Unauthorized',
            hdrs=None, fp=None)  # type: ignore[arg-type]

    meta = rsrv._hermes_capabilities_metadata(
        'http://127.0.0.1:8642', _opener=opener)
    assert 'error' in meta
    assert SECRET not in str(meta)


def test_probe_401_error_field_carries_no_token(clean_credential_env, monkeypatch):
    """A 401 from the gateway (injected opener) is reported as an error whose
    ONLY content is the status/class — never the request's credential. main()
    logs exactly this error string at boot, so a token-free error string IS a
    token-free boot log line."""
    monkeypatch.setenv(rsrv.GATEWAY_CREDENTIAL_ENV_VARS[0], SECRET)

    def opener(req, timeout=None):
        raise rsrv.urllib.error.HTTPError(
            req.full_url, 401, 'Unauthorized', hdrs=None, fp=None)  # type: ignore[arg-type]

    meta = rsrv._hermes_capabilities_metadata(
        'http://127.0.0.1:8642', _opener=opener)
    err = meta.get('error') or ''
    assert err
    assert SECRET not in err
    assert 'Bearer' not in err
    # the 401 must read as an auth problem, not a transport failure
    assert '401' in err


# ---------- discovery payload population (board criterion 3) ----------

def test_successful_probe_serves_source_live_with_non_null_fields(
        ro_server, credential_from_env, monkeypatch):
    live = {'object': 'hermes.api_server.capabilities',
            'is_hermes_gateway': True,
            'responses_api': True,
            'session_key_header': 'X-Hermes-Session-Key',
            'session_continuity_header': 'X-Hermes-Session-Id',
            'responses_endpoint': '/v1/responses',
            'responses_method': 'POST'}
    monkeypatch.setattr(rsrv, '_hermes_capabilities_metadata',
                        lambda base, _opener=None: dict(live))
    status, payload = ro_server.dispatch('GET', '/v1/capabilities')
    assert status == 200
    assert payload['source'] == 'live'          # NOT startup-probe
    up = payload['upstream']
    assert up['is_hermes_gateway'] is True
    assert up['session_key_header'] == 'X-Hermes-Session-Key'
    assert up['responses_endpoint'] == '/v1/responses'
    assert up['responses_method'] == 'POST'


def test_startup_probe_populates_the_discovery_payload(credential_from_env):
    meta = rsrv._hermes_capabilities_metadata(
        'http://127.0.0.1:8642', _opener=_capturing_opener({}))
    assert meta['is_hermes_gateway'] is True
    assert meta['session_key_header']
    assert meta['responses_endpoint']
    assert meta['responses_method'] == 'POST'


# ---------- leak assertions (board criterion 4) ----------

def test_failed_probe_leaks_no_token_into_error_or_payload(
        credential_from_env, monkeypatch):
    """Forced failure WITH a credential configured: neither the returned
    metadata nor any error field may contain the token."""
    monkeypatch.setenv(rsrv.GATEWAY_CREDENTIAL_ENV_VARS[0], SECRET)

    def opener(req, timeout=None):
        raise OSError('connection refused to gateway')

    meta = rsrv._hermes_capabilities_metadata(
        'http://127.0.0.1:8642', _opener=opener)
    blob = json.dumps(meta) + repr(meta)
    assert SECRET not in blob
    assert 'Bearer' not in blob


def test_resolver_output_never_contains_the_token(clean_credential_env,
                                                  monkeypatch, tmp_path):
    """Even a malformed .env line must not leak into any error the resolver
    can produce."""
    env_file = tmp_path / 'broken.env'
    env_file.write_text('GARBAGE LINE WITHOUT EQUALS ' + SECRET + '\n')
    monkeypatch.setattr(rsrv, '_GATEWAY_ENV_FILE', str(env_file))
    key = rsrv._hermes_gateway_credential()
    assert key == ''            # unparseable line is not a credential
