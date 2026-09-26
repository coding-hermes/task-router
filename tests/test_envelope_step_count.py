"""TR-148: the step count the ledger records must be in the envelope too.

Measured 2026-09-26: a live proxied success returned the full per-hop ladder in its
envelope but no aggregate step count, while the ledger row for the same call recorded
`steps: 1`. A caller asking "did this take one attempt or three fallbacks?" had to count
the ladder itself; the two are now the same expression, on success and on failure.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import router_server  # noqa: E402
import router_spawn  # noqa: E402


def _open_state(monkeypatch, tmp_path):
    """Provision an OPEN router state dir for this test (TR-177).

    CI has no `~/.hermes/model-router/`, and the resolver fails CLOSED on absent state — every
    provider is treated as quota-gated, so the chain is empty and `steps` is 0. That is exactly
    what CI reported: `assert (True and 0 >= 1)`, reproduced locally by pointing
    ROUTER_STATE_DIR at a nonexistent path. A unit test must not depend on the machine's live
    state, and `router_spawn` reads ROUTER_STATE_DIR from the environment, so the provisioning
    also reaches the resolver subprocess the proxy spawns (`_subprocess_text` copies os.environ).
    """
    d = tmp_path / 'state'
    d.mkdir(exist_ok=True)
    tables = router_spawn._load_registry()
    provs = sorted({m.get('provider') for m in (tables.get('models') or []) if m.get('provider')})
    json.dump({'updated': 'test', 'providers': {p: {'status': 'open'} for p in provs}},
              open(d / 'quota-state.json', 'w'))
    json.dump({'providers': {}}, open(d / 'health-state.json', 'w'))
    json.dump({'pairs': {}}, open(d / 'circuit-state.json', 'w'))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(d))


def _stub_ok(payload=None):
    def upstream(path, body, headers):
        return 200, (payload or {'choices': [{'message': {'content': 'ok'}}]}), {'provider': 'p1', 'model': 'm1'}
    return upstream


def test_success_envelope_reports_the_step_count(monkeypatch, tmp_path):
    _open_state(monkeypatch, tmp_path)
    rec = []
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: rec.append(k))
    status, out = router_server.proxy_chat('/v1/chat/completions',
                                           {'messages': [{'role': 'user', 'content': 'hi'}]},
                                           {'x-router-profile': 'P1_CODING'},
                                           upstream=_stub_ok())
    r = out['_router']
    assert 'steps' in r, 'the envelope must carry the step count'
    assert isinstance(r['steps'], int) and r['steps'] >= 1
    assert r['steps'] == len(r['ladder']), 'steps must equal the hops the ladder shows'


def test_step_count_matches_the_ledger_row(monkeypatch, tmp_path):
    _open_state(monkeypatch, tmp_path)
    rec = []
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: rec.append(k))
    status, out = router_server.proxy_chat('/v1/chat/completions',
                                           {'messages': [{'role': 'user', 'content': 'hi'}]},
                                           {'x-router-profile': 'P1_CODING'},
                                           upstream=_stub_ok())
    assert rec, 'the row should have been written'
    assert rec[-1]['steps'] == out['_router']['steps'], 'envelope and row must agree'


def test_failure_envelope_also_reports_steps(monkeypatch, tmp_path):
    _open_state(monkeypatch, tmp_path)
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: None)

    def upstream(path, body, headers):
        return 500, {'error': 'boom'}, {'provider': 'p1', 'model': 'm1'}

    status, out = router_server.proxy_chat('/v1/chat/completions',
                                           {'messages': [{'role': 'user', 'content': 'hi'}]},
                                           {'x-router-profile': 'P1_CODING'},
                                           upstream=upstream)
    r = out['_router']
    assert status >= 400
    assert 'steps' in r, 'the failure path must not be poorer than the success path'
    assert r['steps'] == r['hops_attempted'], 'on failure every attempted hop is a step'
