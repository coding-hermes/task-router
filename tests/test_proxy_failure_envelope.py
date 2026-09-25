"""TR-136 / TR-137: an honest failure path for the proxy.

Two measured defects, both from a real 2026-09-24 run of a long prompt through
:9391 (the router's own evidence base, docs/proxy-test-plan-2026-09-25.html):

TR-137 — every hop killed by the 180s budget reported `status=0` /
`transport-failure`, so a slow-but-ALIVE model was indistinguishable from a dead
one. The ladder now carries a reason code, and a deadline hit is its own outcome.

TR-136 — on exhaustion the caller got `served_by/usage/cost/session_id/
wall_time_s = null` and no hop reasons, while the ledger knew the chain, the hops
and their latencies. The failure envelope must be at least as informative as the
ledger it wrote.

No network: the upstream call is injected exactly as in test_proxy_classify.py.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv   # noqa: E402


def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m, 'usd_1m': 0.1 * (i + 1),
                       'outcomes': {'stats_fallback': 'unconditioned'}}
                      for i, (p, m) in enumerate(pairs)],
            'sort': 'price'}


@pytest.fixture()
def proxy_env(tmp_path, monkeypatch):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1_CODING', 'matrix': None,
                     'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)
    return tmp_path


def _run(monkeypatch, upstream, pairs=(('p1', 'm1'), ('p2', 'm2'))):
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(*pairs))
    return rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {}, upstream=upstream)


# ---------- TR-137: the reason vocabulary ----------

def test_the_reason_vocabulary_is_single_sourced():
    """A new code cannot drift in: the classifier may only emit these."""
    assert 'idle-timeout' in rsrv.HOP_FAILURE_REASONS
    assert 'transport-error' in rsrv.HOP_FAILURE_REASONS
    assert len(set(rsrv.HOP_FAILURE_REASONS)) == len(rsrv.HOP_FAILURE_REASONS)


def test_the_classifier_only_emits_declared_reasons():
    cases = [
        dict(exc=ConnectionError('connection refused')),
        dict(exc=TimeoutError('timed out')),
        dict(exc=RuntimeError('no real SSE event inside the idle budget')),
        dict(status=0), dict(status=404), dict(status=503),
        dict(status=200, unservable=True),
    ]
    for kw in cases:
        reason, detail = rsrv._classify_hop_failure(**kw)
        assert reason in rsrv.HOP_FAILURE_REASONS, (kw, reason)
        assert isinstance(detail, str)


def test_a_connection_refusal_is_a_transport_error():
    reason, detail = rsrv._classify_hop_failure(exc=ConnectionError('connection refused'))
    assert reason == 'transport-error' and 'ConnectionError' in detail


def test_the_idle_deadline_is_NOT_a_transport_failure():
    """The defect: a hop that was still working read as a dead one."""
    reason, _ = rsrv._classify_hop_failure(exc=RuntimeError('no real SSE event inside the idle budget'))
    assert reason == 'idle-timeout'


def test_the_transport_wall_is_distinct_from_the_idle_deadline():
    assert rsrv._classify_hop_failure(exc=TimeoutError('timed out'))[0] == 'hop-wall-timeout'
    assert rsrv._classify_hop_failure(status=503)[0] == 'upstream-5xx'
    assert rsrv._classify_hop_failure(status=404)[0] == 'upstream-4xx'
    assert rsrv._classify_hop_failure(status=200, unservable=True)[0] == 'unservable-2xx'


def test_a_deadline_hit_reports_outcome_timeout_not_transport_failure(proxy_env, monkeypatch):
    def upstream(path, body, headers):
        raise TimeoutError('timed out')
    status, payload = _run(monkeypatch, upstream)
    ladder = payload['_router']['ladder']
    assert [a['outcome'] for a in ladder] == ['timeout', 'timeout']
    assert [a['reason'] for a in ladder] == ['hop-wall-timeout', 'hop-wall-timeout']
    assert all(a['timeout_kind'] == 'hop-wall-timeout' for a in ladder)


def test_an_idle_deadline_is_its_own_outcome(proxy_env, monkeypatch):
    def upstream(path, body, headers):
        raise RuntimeError('no real SSE event inside the idle budget')
    _, payload = _run(monkeypatch, upstream)
    ladder = payload['_router']['ladder']
    assert [a['outcome'] for a in ladder] == ['timeout', 'timeout']
    assert [a['reason'] for a in ladder] == ['idle-timeout', 'idle-timeout']


def test_genuine_transport_failures_keep_the_legacy_vocabulary(proxy_env, monkeypatch):
    """Existing consumers read 'transport-failure'; the fix must not rename it."""
    def upstream(path, body, headers):
        raise ConnectionError('connection refused')
    _, payload = _run(monkeypatch, upstream)
    ladder = payload['_router']['ladder']
    assert [a['outcome'] for a in ladder] == ['transport-failure', 'transport-failure']
    assert [a['reason'] for a in ladder] == ['transport-error', 'transport-error']


def test_an_upstream_5xx_is_labelled_as_such(proxy_env, monkeypatch):
    def upstream(path, body, headers):
        return 503, {'error': 'upstream exploded'}
    _, payload = _run(monkeypatch, upstream)
    assert [a['reason'] for a in payload['_router']['ladder']] == ['upstream-5xx', 'upstream-5xx']


# ---------- TR-136: the failure envelope ----------

def test_exhaustion_envelope_is_as_informative_as_the_ledger(proxy_env, monkeypatch):
    def upstream(path, body, headers):
        raise TimeoutError('timed out')
    status, payload = _run(monkeypatch, upstream)
    r = payload['_router']
    assert status == 502 and r['exhausted'] is True
    assert r['served_by'] is None and r['served_by_reason']
    assert r['terminal_reason'] == 'hop-wall-timeout'
    assert r['hops_attempted'] == 2
    assert isinstance(r['wall_time_s'], (int, float)) and r['wall_time_s'] >= 0
    assert r['session_id'], 'the caller must learn which session this belonged to'
    assert r['outcome_row'] == {'source_system': 'router-proxy',
                                'session_id': r['session_id'],
                                'parent_session_id': None}
    # per-hop reasons are the audit trail
    assert [a['reason'] for a in r['ladder']] == ['hop-wall-timeout'] * 2
    assert all('latency_s' in a and 'status' in a for a in r['ladder'])


def test_unmeasured_failure_values_are_null_WITH_a_reason_never_a_fake_zero(proxy_env, monkeypatch):
    def upstream(path, body, headers):
        raise ConnectionError('refused')
    _, payload = _run(monkeypatch, upstream)
    r = payload['_router']
    assert r['usage'] is None and r['usage_reason']
    assert r['cost_usd'] is None and r['cost_reason']


def test_the_failure_envelope_supersets_the_success_contract(proxy_env, monkeypatch):
    """Every key the success path promises must exist on the failure path too."""
    def ok(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'served'}}],
                     'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}
    _, good = _run(monkeypatch, ok)

    def bad(path, body, headers):
        raise TimeoutError('timed out')
    _, broken = _run(monkeypatch, bad)

    success_keys = set(good['_router'])
    failure_keys = set(broken['_router'])
    missing = success_keys - failure_keys
    assert not missing, f'the failure envelope dropped {sorted(missing)}'


def test_no_eligible_hop_still_explains_itself(monkeypatch):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1_CODING', 'matrix': None,
                     'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {'chain': []})
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=lambda *a: (200, {}))
    r = payload['_router']
    # 502 (nothing attempted) or 503 (no open hop) — both are "nothing ran", and
    # both must explain themselves rather than return a bare error body.
    assert status in (502, 503), status
    assert r['terminal_reason'] == 'no-hops' and r['hops_attempted'] == 0
    assert r['session_id'] and r['wall_time_s'] is not None
