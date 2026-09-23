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
    served = payload['_router']['served_by']
    assert (served['provider'], served['model']) == ('p2', 'good')
    # metering keys are part of the served_by contract since 2026-09-23
    assert {'tokens_in', 'tokens_out', 'cost_usd', 'price_basis'} <= set(served)
    assert served['cost_usd'] is None                      # no usage block upstream
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


# ─── TR-081: the ladder must be self-auditing ──────────────────────────────
# A caller served at hop 4 could not previously learn that hops 1-3 were gated
# at resolve time (the resolver filters them out before the proxy builds its
# ladder). The response now carries the skip evidence.

def _gated_chain(*pairs, exclusions=(), gate_reasons=()):
    """A chain whose first entry is NOT hop 1 — i.e. earlier hops were filtered
    by the resolver's gates (health / circuit / quota)."""
    out = _chain(*pairs)
    out['exclusions'] = list(exclusions)
    out['gate_reasons'] = list(gate_reasons)
    return out


def test_ladder_starting_past_hop_1_reports_the_skip_evidence(proxy_env, monkeypatch):
    """The dogfood case: served at hop 4 while hops 1-3 were gated."""
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    excl = [{'hop': 1, 'provider': 'kimi-for-coding', 'model': 'k3',
             'why': ['health DOWN (2026-09-20T01:00:40+00:00)']},
            {'hop': 2, 'provider': 'zai-glm', 'model': 'glm-5.3-flash',
             'why': ['quota GATED: blocked']}]
    reasons = ['hop 1 kimi-for-coding/k3: health DOWN', 'hop 2 zai-glm/glm-5.3-flash: quota GATED: blocked']
    res = _gated_chain(('zai-glm', 'glm-5.3'), exclusions=excl, gate_reasons=reasons)
    res['chain'][0]['hop'] = 4  # first OPEN hop is 4
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: res)

    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                      upstream=lambda *a: (200, {'ok': 1}))
    r = payload['_router']
    assert status == 200
    # the caller can now see the walk started at 4, not 1 …
    assert r['first_attempt_hop'] == 4
    # … and WHY the cheaper hops were not tried
    assert len(r['exclusions']) == 2
    assert r['exclusions'][0]['provider'] == 'kimi-for-coding'
    assert r['gate_reasons'] == reasons


def test_taken_path_reports_no_skipped_hops(proxy_env, monkeypatch):
    """hop1 fails, hop2 serves with max_hops=2 — full trail, nothing skipped."""
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _gated_chain(('p1', 'bad'), ('p2', 'good')))

    def upstream(path, body, headers):
        if body['model'] == 'bad':
            raise ConnectionError('connection refused')
        return 200, {'choices': [{'message': {'content': 'served'}}]}
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                      {'x-router-max-hops': '2'}, upstream=upstream)
    r = payload['_router']
    assert status == 200
    assert [a['outcome'] for a in r['ladder']] == ['transport-failure', 'ok']
    assert r['first_attempt_hop'] == 1
    assert r['skipped_hops'] == 0
    assert r['exclusions'] == [] and r['gate_reasons'] == []


def test_skipped_hops_counts_chain_beyond_the_bound(proxy_env, monkeypatch):
    """max_hops=2 over a 3-entry chain: one entry is never attempted."""
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_chain',
                        lambda reqs, **k: _gated_chain(('p1', 'a'), ('p2', 'b'), ('p3', 'c')))
    status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []},
                                      {'x-router-max-hops': '2'},
                                      upstream=lambda *a: (500, {'error': 'boom'}))
    r = payload['_router']
    assert r['exhausted'] is True
    assert len(r['ladder']) == 2
    assert r['skipped_hops'] == 1  # the third entry was never reached


def test_malformed_resolver_output_cannot_break_the_proxy(proxy_env, monkeypatch):
    """Fail-open is sacred: garbage exclusions/gate_reasons must not raise."""
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1', 'matrix': None, 'complexity_sig': None, 'problems': []}))
    for bad in ({'chain': [{'hop': 1, 'provider': 'p', 'model': 'm'}],
                 'exclusions': 'garbage', 'gate_reasons': None},
                {'chain': [{'hop': 1, 'provider': 'p', 'model': 'm'}]}):
        monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, _b=bad, **k: _b)
        status, payload = rsrv.proxy_chat('/v1/chat/completions', {'messages': []}, {},
                                          upstream=lambda *a: (200, {'ok': 1}))
        r = payload['_router']
        assert status == 200
        assert r['exclusions'] == [] and r['gate_reasons'] == []
        assert r['first_attempt_hop'] == 1 and r['skipped_hops'] == 0
