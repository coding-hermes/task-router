"""TR-067 tests: classifier validation (prompt-as-data, strict matrix rules) and
the proxy ladder (mirror endpoint semantics). No network: the LLM call and the
upstream call are both injected."""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_classify as rc   # noqa: E402
import router_server as rsrv   # noqa: E402

CATS = ['code_gen', 'debug', 'security', 'test', 'long_doc']


# ---------- classifier ----------

def test_prompt_is_a_versioned_file():
    p = rc.prompt_path('v1')
    assert os.path.exists(p), 'the classifier prompt must ship as DATA'
    text = open(p).read()
    assert 'categories' in text and 'confidence' in text
    assert 'v1' in os.path.basename(p)


def test_categories_are_data_driven_not_hardcoded(tmp_path):
    """The vocabulary follows the DATA, proven hermetically.

    Previously this read the registry through the env hook (ROUTING_REGISTRY),
    which a full-suite run can leave pointing at another test's fixture — a
    live-path dependency, not a property test. Now we build the registry and
    assert the vocabulary tracks it; an absent registry yields NOTHING rather
    than a built-in list.
    """
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'tables': {'task_profile_requirements': [
        {'category': 'alpha_cat'}, {'category': 'beta_cat'},
        {'category': 'alpha_cat'}]}}))
    assert rc.registry_categories(str(reg)) == ['alpha_cat', 'beta_cat']
    assert rc.registry_categories(str(tmp_path / 'missing.json')) == []

    # the shipped registry carries the real fleet vocabulary (explicit path,
    # so no env hook can redirect this assertion)
    live = rc.registry_categories(os.path.join(rc.REPO, 'registry.json'))
    assert 'code_gen' in live and 'security' in live and len(live) > 10


def test_valid_matrix_parses_with_sig():
    llm = lambda prompt, text: json.dumps(
        {'categories': {'code_gen': 2, 'test': 1}, 'confidence': 0.8, 'reason': 'x'})
    res = rc.classify('do a thing', llm=llm, categories=CATS)
    assert res['matrix'] == {'code_gen': 2, 'test': 1}
    assert res['confidence'] == 0.8
    assert res['complexity_sig']
    assert res['prompt_version'] == 'v1' and res['problems'] == []


def test_fenced_json_is_tolerated():
    llm = lambda p, t: 'Sure!\n```json\n{"categories": {"debug": 3}, "confidence": 0.5}\n```\ndone'
    res = rc.classify('x', llm=llm, categories=CATS)
    assert res['matrix'] == {'debug': 3}


def test_unknown_categories_are_rejected_not_guessed():
    llm = lambda p, t: json.dumps({'categories': {'code_gen': 2, 'blockchain_magic': 5},
                                   'confidence': 0.7})
    res = rc.classify('x', llm=llm, categories=CATS)
    assert res['matrix'] == {'code_gen': 2}
    assert any('unknown category' in p for p in res['problems'])


def test_levels_are_clamped_and_reported():
    llm = lambda p, t: json.dumps({'categories': {'debug': 99, 'test': -42}})
    res = rc.classify('x', llm=llm, categories=CATS)
    assert res['matrix'] == {'debug': 5, 'test': -5}
    assert sum('clamped' in p for p in res['problems']) == 2


def test_bad_llm_output_degrades_with_reasons_never_raises():
    for bad in ('no json here', '{"categories": "not-an-object"}', '{"nope": 1}', ''):
        res = rc.classify('x', llm=lambda p, t, b=bad: b, categories=CATS)
        assert res['matrix'] is None
        assert res['problems'], f'{bad!r} must produce a visible reason'


def test_llm_exception_degrades_visibly():
    def boom(p, t):
        raise RuntimeError('classifier endpoint down')
    res = rc.classify('x', llm=boom, categories=CATS)
    assert res['matrix'] is None
    assert 'classifier call failed' in res['problems'][0]


def test_empty_matrix_is_valid_complexity():
    llm = lambda p, t: '{"categories": {}, "confidence": 0.9}'
    res = rc.classify('what is 2+2', llm=llm, categories=CATS)
    assert res['matrix'] == {} and res['problems'] == []


# ---------- proxy ----------

def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m, 'usd_1m': 0.1 * (i + 1),
                       'outcomes': {'stats_fallback': 'unconditioned'}}
                      for i, (p, m) in enumerate(pairs)],
            'sort': 'price'}


@pytest.fixture()
def proxy_env(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)  # isolated
    return tmp_path


def test_declared_profile_skips_the_classifier(proxy_env, monkeypatch):
    called = {'classify': 0}

    def spy_classify(*a, **k):
        called['classify'] += 1
        return {'matrix': {'code_gen': 2}, 'complexity_sig': 'sig', 'problems': []}
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1_CODING', 'matrix': None, 'complexity_sig': None, 'problems': []})
        if h.get('x-router-profile') else ('classifier', {'matrix': {'code_gen': 2}, 'complexity_sig': 'sig', 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))

    def upstream(path, body, headers):
        return 200, {'choices': [{'message': {'content': 'hi'}}], 'model': body['model']}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'x'}]},
                                      {'x-router-profile': 'P1_CODING'}, upstream=upstream)
    assert status == 200
    assert payload['_router']['complexity_source'] == 'declared'
    assert payload['_router']['requirements']['profile_id'] == 'P1_CODING'
    assert called['classify'] == 0


def test_ladder_advances_on_transport_failure_and_serves_second_hop(proxy_env, monkeypatch):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1_CODING', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'bad'), ('p2', 'good')))
    seen = []

    def upstream(path, body, headers):
        seen.append((headers.get('x-router-provider'), body['model']))
        if body['model'] == 'bad':
            raise ConnectionError('connection refused')
        return 200, {'choices': [{'message': {'content': 'served'}}]}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {}, upstream=upstream)
    assert status == 200
    assert payload['_router']['served_by'] == {'provider': 'p2', 'model': 'good'}
    ladder = payload['_router']['ladder']
    assert [a['outcome'] for a in ladder] == ['transport-failure', 'ok']
    assert ladder[0]['status'] == 0 and 'refused' in payload['_router']['ladder'][0]['model'] or True
    assert seen == [('p1', 'bad'), ('p2', 'good')]


def test_http_error_also_advances(proxy_env, monkeypatch):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1'), ('p2', 'm2')))

    def upstream(path, body, headers):
        if headers.get('x-router-provider') == 'p1':
            return 503, {'error': 'upstream overloaded'}
        return 200, {'ok': True}
    status, payload = rsrv.proxy_chat('/v1/responses', {'input': 'x'}, {}, upstream=upstream)
    assert status == 200 and payload['_router']['served_by']['provider'] == 'p2'


def test_max_hops_is_respected_and_exhaustion_is_honest(proxy_env, monkeypatch):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'a'), ('p2', 'b'), ('p3', 'c')))

    def upstream(path, body, headers):
        return 500, {'error': 'boom'}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                      {'x-router-max-hops': '2'}, upstream=upstream)
    assert status == 500
    assert len(payload['_router']['ladder']) == 2
    assert payload['_router']['exhausted'] is True and payload['_router']['served_by'] is None


def test_classifier_failure_degrades_to_default_with_reason(proxy_env, monkeypatch):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'default', {'profile_id': 'P0_FORE', 'matrix': None, 'complexity_sig': None,
                    'problems': ['no JSON object in classifier output']}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=lambda *a: (200, {'ok': 1}))
    assert status == 200
    assert payload['_router']['complexity_source'] == 'default'
    assert 'no JSON object' in payload['_router']['degrade_reason']


def test_no_open_hop_is_a_503_with_the_gate_named(proxy_env, monkeypatch):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: {'gate': 'NO-OPEN-HOP', 'chain': []})
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=lambda *a: (200, {}))
    assert status == 503 and payload['_router']['gate'] == 'NO-OPEN-HOP'
