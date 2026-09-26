"""TR-163: a row must say WHAT THE OPTION CHAIN WAS and WHY each option was skipped.

Origin: the 2026-09-26 incident. Rows read `route_outcome=no-hops, hops_attempted=0`
and could not explain themselves — the resolver HAD computed the exclusions and handed
them to the response envelope, but the ledger row dropped them. Diagnosing "why is
nothing eligible" needed a hand-run of the resolver against the live registry. The
ledger is the artefact that survives a restart, so it has to carry the same evidence the
envelope does.

Also pinned here: the row stores a prompt HASH and a length, never the prompt text. The
fleet repeats its prompts almost verbatim, so the hash is what makes grouping queries
possible without the ledger becoming a transcript of everything the fleet was asked.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402
import router_server as rsrv   # noqa: E402


def _resolved(pairs, exclusions=None, skipped=None, first_attempt=None, gate=None):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m, 'usd_1m': 0.1 * (i + 1)}
                      for i, (p, m) in enumerate(pairs)],
            'exclusions': exclusions or [],
            'skipped_hops': skipped,
            'first_attempt_hop': first_attempt,
            'gate': gate,
            'sort': 'price'}


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / 'outcomes.jsonl'
    path.write_text('')
    monkeypatch.setattr(ro, 'outcomes_path', lambda *a, **k: str(path))
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'classifier', {'matrix': {'code_gen': 3}, 'complexity_sig': 'sig123',
                       'profile_id': None, 'problems': [],
                       'model': 'deepseek-flash', 'prompt_version': 'v1'}))
    return path


def _rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def _serve(monkeypatch, upstream, resolved):
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: resolved)
    return rsrv.proxy_chat('/v1/chat/completions',
                           {'messages': [{'role': 'user', 'content': 'tick the fleet'}]},
                           {'x-router-session': 'sess-163'}, upstream=upstream)



@pytest.fixture(autouse=True)
def _isolated_router_state(tmp_path, monkeypatch):
    """TR-182: this file drives proxy_chat with fake upstreams that RAISE, and every failed
    hop is reported to the circuit. Without an isolated state dir those failures land in the
    LIVE ~/.hermes/model-router/circuit-state.json as p1/bad, p1/m1, p2/m2 -- measured there,
    next to a real doctrine pin. A unit test must not write the router's live gates."""
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'outcomes.jsonl'))

def test_a_served_row_carries_the_option_chain_it_chose_from(ledger, monkeypatch):
    resolved = _resolved([('xkiro', 'glm-5.3-flash'), ('clinepass', 'deepseek-v4-flash'),
                          ('9router', 'ocg/sonnet')], skipped=2, first_attempt=3)
    _serve(monkeypatch, lambda *a, **k: (200, {'choices': [{'message': {'content': 'ok'}}],
                                               'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}),
           resolved)
    row = [r for r in _rows(ledger) if r.get('route_outcome') == 'served'][0]
    chain = row.get('chain')
    assert isinstance(chain, list) and len(chain) == 3, chain
    assert [h['hop'] for h in chain] == [1, 2, 3]
    assert chain[0]['provider'] == 'xkiro' and chain[1]['model'] == 'deepseek-v4-flash'
    assert row.get('chain_length') == 3
    assert row.get('chain_truncated') is False
    assert row.get('skipped_hops') == 2, 'the skip evidence must survive to the row'
    assert row.get('first_attempt_hop') == 3


def test_a_no_hops_row_says_why_nothing_was_eligible(ledger, monkeypatch):
    """The blind exit must explain itself. This is the exact row that could not be read
    during the incident: `no-hops` with zero hops and no reason."""
    resolved = _resolved([], exclusions=[
        {'hop': 1, 'provider': 'xkiro', 'model': 'cohere/command-a-plus',
         'why': ['circuit OPEN (provider-level, api_down)'], 'codes': ['circuit-open']},
        {'hop': 2, 'provider': 'clinepass', 'model': 'deepseek-v4-flash',
         'why': ['quota GATED: blocked'], 'codes': ['quota-gated']},
    ], gate={'quota': 'blocked'})
    status, payload = _serve(monkeypatch, lambda *a, **k: {}, resolved)
    assert status == 503
    row = [r for r in _rows(ledger) if r.get('route_outcome') == 'no-hops'][0]
    assert row['hops_attempted'] == 0
    excl = row.get('exclusions')
    assert isinstance(excl, list) and len(excl) == 2, excl
    assert excl[0]['codes'] == ['circuit-open'] and 'api_down' in excl[0]['why'][0]
    assert excl[1]['codes'] == ['quota-gated']
    assert row.get('chain_length') == 0
    assert row.get('gate') == {'quota': 'blocked'}
    # the envelope and the row must not disagree about the same request
    assert payload['_router']['exclusions'][0]['codes'] == ['circuit-open']


def test_the_chain_cap_is_explicit_never_silent(ledger, monkeypatch):
    """A 160-lane chain cannot go in a row wholesale — but a capped list that does not
    SAY it was capped misrepresents the resolver's options."""
    big = _resolved([(f'p{i}', f'm{i}') for i in range(60)])
    _serve(monkeypatch, lambda *a, **k: (200, {'choices': [{'message': {'content': 'ok'}}]}), big)
    row = [r for r in _rows(ledger) if r.get('route_outcome') == 'served'][0]
    assert row['chain_length'] == 60, 'the TRUE length is recorded'
    assert len(row['chain']) == 20, 'the stored list is capped'
    assert row['chain_truncated'] is True, 'and the cap is declared'


def test_the_row_never_stores_the_prompt_text(ledger, monkeypatch):
    secret = 'TOP-SECRET-PROMPT-BODY-2f9c'
    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _resolved([('p', 'm')]))
    rsrv.proxy_chat('/v1/chat/completions',
                    {'messages': [{'role': 'user', 'content': f'tick {secret}'}]},
                    {'x-router-session': 'sess-163'},
                    upstream=lambda *a, **k: (200, {'choices': [{'message': {'content': 'ok'}}]}))
    raw = ledger.read_text()
    assert secret not in raw, 'the prompt text must never be persisted'
    row = _rows(ledger)[0]
    assert row['prompt_chars'] == len('tick ' + secret)
    assert isinstance(row['prompt_sha'], str) and len(row['prompt_sha']) == 64, row['prompt_sha']


@pytest.mark.parametrize('source,expected', [
    ('classifier', 'ok'), ('jev', 'ok'),
    ('classifier-empty', 'empty-matrix'),
    ('declared', 'declared'),
    ('default', 'no-json'),
])
def test_classifier_evidence_names_the_parse_state(source, expected):
    ev = rsrv._classifier_evidence({'model': 'deepseek-flash', 'problems': ['no JSON object']},
                                   source)
    assert ev['parse'] == expected
    assert ev['source'] == source
    assert ev['model'] == 'deepseek-flash'


def test_a_failed_row_records_the_attempts_it_walked(ledger, monkeypatch):
    calls = {'n': 0}

    def upstream(*a, **k):
        calls['n'] += 1
        raise RuntimeError('upstream down')

    resolved = _resolved([('p1', 'm1'), ('p2', 'm2')])
    _serve(monkeypatch, upstream, resolved)
    row = [r for r in _rows(ledger) if r.get('route_outcome') == 'failed'][0]
    assert isinstance(row.get('attempts'), list) and row['attempts'], row.get('attempts')
    assert row['attempts'][0].get('provider') == 'p1'
    assert row.get('chain_length') == 2
