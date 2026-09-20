"""TR-074/TR-075 proxy-scope compat — `role:developer` (SPEC-PROXY-DRIVERS §3, §6.4).

The spec's §3 wire table predicted this exact failure for pi and
deepseek-harness: a host whose config declares `supportsDeveloperRole=false`
still sends `role:developer` on reasoning models, and OpenAI-compatible servers
vary on whether they accept it.

Measured before the fix: the proxy forwarded the body UNCHANGED, the upstream
returned 400 ("Invalid value: 'developer'"), and the ladder burned every hop on
a payload that `role:system` (identical content) served with 200. That is a
proxy-scope defect, not a driver bug — five drivers would otherwise each carry
the same workaround and drift (spec §1).
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
sys.path.insert(0, os.path.join(REPO, 'scripts', 'drivers'))

import router_server as rs      # noqa: E402
import drivers                  # noqa: E402


def _strict_upstream(seen, reject_developer=True):
    """Stands in for a compatible server that rejects role:developer."""
    def call(path, body, headers):
        roles = [m.get('role') for m in (body.get('messages') or [])]
        seen.append(roles)
        if reject_developer and 'developer' in roles:
            return 400, {'error': {'message':
                "Invalid value: 'developer'. Supported values are: 'system', 'user', 'assistant'."}}
        return 200, {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}
    return call


@pytest.fixture
def outcomes(tmp_path, monkeypatch):
    p = tmp_path / 'outcomes.jsonl'
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(p))
    return p


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(rs, '_proxy_chain', lambda *a, **k: {
        'chain': [{'hop': 1, 'provider': 'strict', 'model': 'strict-1', 'usd_1m': 1.0}],
        'exclusions': [], 'gate_reasons': [], 'sort': 'price'})


def test_a_developer_role_reaches_upstream_as_system(outcomes, chain):
    """The failure the wire facts predict: without normalization the ladder
    exhausts on a payload the upstream would have served."""
    seen = []
    status, out = rs.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'developer', 'content': 'be concise'},
                      {'role': 'user', 'content': 'hi'}]},
        {'x-router-caller': 'pi'}, upstream=_strict_upstream(seen))
    assert status == 200, 'a developer role must not exhaust the ladder'
    assert seen == [['system', 'user']], 'upstream must receive role:system'
    assert out['_router']['served_by']['model'] == 'strict-1'


def test_the_rewrite_is_disclosed_never_silent(outcomes, chain):
    """R10: the degrade must be visible with its reason."""
    seen = []
    _, out = rs.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'developer', 'content': 'x'}]},
        {'x-router-caller': 'pi'}, upstream=_strict_upstream(seen))
    assert out['_router']['developer_role_rewrites'] == 1
    assert any('role:developer' in p for p in out['_router']['problems']), (
        'the caller must be able to see its payload was adjusted')


def test_preserve_opt_out_leaves_the_payload_alone(outcomes, chain):
    """A host that genuinely needs `developer` (a model that supports it) must
    be able to say so — the normalization is a default, not a policy."""
    seen = []
    status, out = rs.proxy_chat(
        '/v1/chat/completions',
        {'messages': [{'role': 'developer', 'content': 'x'}]},
        {'x-router-caller': 'pi', 'x-router-developer-role': 'preserve'},
        upstream=_strict_upstream(seen))
    assert status == 400, 'with preserve, the upstream verdict stands'
    assert seen == [['developer']]
    assert out['_router']['developer_role_rewrites'] == 0


def test_a_body_without_developer_is_untouched(outcomes, chain):
    """No false rewrites: system/user/assistant pass through byte-identical."""
    seen = []
    msgs = [{'role': 'system', 'content': 's'},
            {'role': 'user', 'content': 'u'},
            {'role': 'assistant', 'content': 'a'}]
    _, out = rs.proxy_chat('/v1/chat/completions', {'messages': msgs},
                           {'x-router-caller': 'pi'},
                           upstream=_strict_upstream(seen))
    assert seen == [['system', 'user', 'assistant']]
    assert out['_router']['developer_role_rewrites'] == 0
    assert not any('role:developer' in p for p in out['_router']['problems'])


def test_other_message_keys_survive_the_rewrite(outcomes, chain):
    """Only the role is rewritten — content and vendor extensions are preserved."""
    seen = []
    rs.proxy_chat('/v1/chat/completions',
                  {'messages': [{'role': 'developer', 'content': 'keep me',
                                 'cache_control': {'type': 'ephemeral'}}]},
                  {'x-router-caller': 'pi'}, upstream=_strict_upstream(seen))
    msgs = seen_msgs = None
    # capture the actual forwarded body
    def cap(path, body, headers):
        msgs = body.get('messages')
        assert msgs[0]['role'] == 'system'
        assert msgs[0]['content'] == 'keep me'
        assert msgs[0]['cache_control'] == {'type': 'ephemeral'}
        return 200, {'ok': True}
    rs.proxy_chat('/v1/chat/completions',
                  {'messages': [{'role': 'developer', 'content': 'keep me',
                                 'cache_control': {'type': 'ephemeral'}}]},
                  {'x-router-caller': 'pi'}, upstream=cap)


@pytest.mark.parametrize('body', [
    {}, {'messages': None}, {'messages': 'not-a-list'},
    {'messages': [None, 'string', 5]}, {'messages': [{'role': 'developer'}]},
])
def test_malformed_bodies_never_raise(body, outcomes, chain):
    """Fail-open: a live client must never get a 500 from normalization."""
    status, out = rs.proxy_chat('/v1/chat/completions', body,
                                {'x-router-caller': 'pi'},
                                upstream=lambda p, b, h: (200, {'ok': True}))
    assert status in (200, 503)


def test_a_driver_declares_developer_role_compat_not_implements_it():
    """The contract: a driver states the fact; the proxy does the work."""
    import inspect
    import hermes as h
    assert 'compat' in dir(h.HermesDriver), 'drivers declare compat as data'
    src = inspect.getsource(h.HermesDriver)
    assert 'developer' not in src.lower(), (
        'a driver must not implement the workaround — normalization is proxy scope')
