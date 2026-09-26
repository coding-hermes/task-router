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


def _stub_ok(payload=None):
    def upstream(path, body, headers):
        return 200, (payload or {'choices': [{'message': {'content': 'ok'}}]}), {'provider': 'p1', 'model': 'm1'}
    return upstream


def test_success_envelope_reports_the_step_count(monkeypatch, tmp_path):
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


def test_step_count_matches_the_ledger_row(monkeypatch):
    rec = []
    monkeypatch.setattr(router_server, '_proxy_record', lambda *a, **k: rec.append(k))
    status, out = router_server.proxy_chat('/v1/chat/completions',
                                           {'messages': [{'role': 'user', 'content': 'hi'}]},
                                           {'x-router-profile': 'P1_CODING'},
                                           upstream=_stub_ok())
    assert rec, 'the row should have been written'
    assert rec[-1]['steps'] == out['_router']['steps'], 'envelope and row must agree'


def test_failure_envelope_also_reports_steps(monkeypatch):
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
