"""Structured output + a budget that survives reasoning (owner 2026-10-03).

Why these exist: the classifier ran on live traffic and 502 of 747 unrated rows
said 'no JSON object in classifier output' - prose the tolerant parser could not
read, because nothing was guaranteeing the shape. And the budget was 700 tokens
against a reasoning model, so an empty completion (no error) was reported as a
rating failure. These tests pin the contract, not the implementation: a
structured rung is requested and stepped down when the endpoint rejects it, the
budget never returns to a reasoning-starving value, an empty completion names
its own cause, and the category vocabulary falls back to where the artefact
actually lives.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_classify as rc  # noqa: E402


def test_budget_default_survives_a_reasoning_preamble(monkeypatch):
    """A reasoning model must never be given a budget that guarantees an empty
    completion. The observed reasoning burn is hundreds of tokens on a trivial
    prompt and thousands on a real one."""
    monkeypatch.delenv('ROUTER_CLASSIFY_MAX_TOKENS', raising=False)
    assert rc._classify_budget() >= 4096


def test_budget_env_override(monkeypatch):
    monkeypatch.setenv('ROUTER_CLASSIFY_MAX_TOKENS', '9000')
    assert rc._classify_budget() == 9000
    monkeypatch.setenv('ROUTER_CLASSIFY_MAX_TOKENS', 'nonsense')
    assert rc._classify_budget() == rc.CLASSIFY_MAX_TOKENS_DEFAULT


def test_structured_mode_starts_at_the_best_rung_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv('ROUTER_CLASSIFY_STRUCTURED', raising=False)
    monkeypatch.delenv('ROUTER_CLASSIFY_STRUCTURED_MODE', raising=False)
    assert rc._structured_mode() == 'json_schema'
    monkeypatch.setenv('ROUTER_CLASSIFY_STRUCTURED', 'off')
    assert rc._structured_mode() == 'none'
    monkeypatch.setenv('ROUTER_CLASSIFY_STRUCTURED', 'json_object')
    assert rc._structured_mode() == 'json_object'


def test_matrix_schema_restricts_levels_to_the_signed_scale():
    schema = rc._matrix_schema(['code_gen', 'reasoning'])
    assert schema['properties']['code_gen'] == {'type': 'integer', 'minimum': -5, 'maximum': 5}
    assert schema['additionalProperties']['maximum'] == 5
    # no vocabulary known: stay open rather than invent one
    assert 'properties' not in rc._matrix_schema([])


def test_raw_marks_an_empty_completion_instead_of_returning_a_silent_blank():
    """The silent-failure shape: an empty completion is not 'the model said no
    category', and must be distinguishable from one."""
    r = rc.Raw('', empty=True, truncated=True, max_tokens=700, finish_reason='length')
    assert str(r) == ''
    assert r.meta['truncated'] is True
    assert r.meta['max_tokens'] == 700
    assert rc.Raw('{"a": 1}').meta == {}


def test_a_rejected_rung_steps_down_and_is_remembered(monkeypatch):
    """DeepSeek rejects json_schema (400) but accepts json_object. The step-down
    must (a) still return an answer, (b) not lose the rating - the expensive
    outcome is a missing rating, not a missing format - and (c) not pay the 400
    again on every subsequent call."""
    seen = []

    def fake_call_lane(lane, body):
        payload = json.loads(body)
        seen.append(payload.get('response_format'))
        if (payload.get('response_format') or {}).get('type') == 'json_schema':
            raise RuntimeError('HTTP Error 400: Bad Request')
        return rc.Raw('{"categories": {"code_gen": 2}}')

    monkeypatch.delenv('ROUTER_CLASSIFY_STRUCTURED_MODE', raising=False)
    monkeypatch.setenv('ROUTER_CLASSIFY_STRUCTURED', 'auto')
    monkeypatch.setattr(rc, '_call_lane', fake_call_lane)
    monkeypatch.setattr(rc, '_classifier_lanes',
                        lambda: [{'base': 'http://x', 'model': 'm', 'key_env': '', 'key_value': 'k',
                                  'timeout': 5.0}])
    out = rc.default_llm('sys prompt', 'task text')
    assert 'code_gen' in str(out)
    assert seen[0]['type'] == 'json_schema'
    assert seen[0]['json_schema']['schema']['additionalProperties']['maximum'] == 5
    assert seen[-1] == {'type': 'json_object'}
    assert os.environ['ROUTER_CLASSIFY_STRUCTURED_MODE'] == 'json_object'


def test_an_empty_completion_names_its_cause_in_problems(monkeypatch):
    """'no JSON object' with no cause is what let the dominant live failure stay
    invisible: 502 rows carried exactly that string and nothing else."""
    monkeypatch.setattr(rc, '_call_lane',
                        lambda lane, body: rc.Raw('', empty=True, truncated=True,
                                                  reasoning_tokens=1560, finish_reason='length'))
    monkeypatch.setattr(rc, '_classifier_lanes',
                        lambda: [{'base': 'http://x', 'model': 'm', 'key_env': '', 'key_value': 'k',
                                  'timeout': 5.0}])
    out = rc.classify('some task text')
    assert out['matrix'] is None
    joined = ' '.join(out['problems'])
    assert 'no JSON object' in joined
    assert 'EMPTY completion' in joined
    assert 'reasoning_tokens=1560' in joined
    assert out['call_meta'].get('truncated') is True


def test_registry_categories_falls_back_to_where_the_artefact_lives(monkeypatch, tmp_path):
    """The lookup was <repo>/registry.json while the artefact is written to
    <repo>/data/registry.json, so the vocabulary silently came back empty - which
    makes validation permissive and a bare {category: level} answer unreadable."""
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'registry.json').write_text(json.dumps(
        {'tables': {'task_profile_requirements': [{'category': 'code_gen'}, {'category': 'reasoning'}]}}))
    monkeypatch.setattr(rc, 'REPO', str(tmp_path))
    monkeypatch.delenv('ROUTING_REGISTRY', raising=False)
    assert rc.registry_categories() == ['code_gen', 'reasoning']


def test_registry_categories_returns_empty_rather_than_inventing_a_vocabulary(monkeypatch, tmp_path):
    monkeypatch.setattr(rc, 'REPO', str(tmp_path))
    monkeypatch.delenv('ROUTING_REGISTRY', raising=False)
    assert rc.registry_categories() == []


def test_thinking_is_off_by_default(monkeypatch):
    """The classifier is a small extraction; thinking is overhead here. Measured
    on the live endpoint with the same prompt: production spent 122 of 187
    completion tokens on reasoning, while reasoning_effort='none' answered in 61
    with zero reasoning and 35% less wall time."""
    monkeypatch.delenv('ROUTER_CLASSIFY_THINKING', raising=False)
    monkeypatch.delenv('ROUTER_CLASSIFY_THINKING_MODE', raising=False)
    assert rc._thinking_off() == {'reasoning_effort': 'none'}


def test_thinking_can_be_turned_back_on_and_a_rung_forced(monkeypatch):
    monkeypatch.setenv('ROUTER_CLASSIFY_THINKING', 'on')
    assert rc._thinking_off() == {}
    monkeypatch.setenv('ROUTER_CLASSIFY_THINKING', 'off')
    monkeypatch.setenv('ROUTER_CLASSIFY_THINKING_MODE', 'disabled')
    assert rc._thinking_off() == {'thinking': {'type': 'disabled'}}


@pytest.mark.parametrize('value', ['off', 'false', '0', 'no', 'disabled'])
def test_falsey_thinking_values_disable_reasoning(monkeypatch, value):
    monkeypatch.setenv('ROUTER_CLASSIFY_THINKING', value)
    monkeypatch.delenv('ROUTER_CLASSIFY_THINKING_MODE', raising=False)
    assert rc._thinking_off() == {'reasoning_effort': 'none'}


def test_the_thinking_mode_is_recorded_on_the_call_meta(monkeypatch):
    """A provider change must be visible in the ledger, not inferred from latency."""
    monkeypatch.setattr(rc, '_call_lane',
                        lambda lane, body: rc.Raw('{"categories": {"code_gen": 2}}',
                                                  thinking=('none' if json.loads(body).get('reasoning_effort')
                                                            == 'none' else 'default'),
                                                  chars_reasoning=0, reasoning_tokens=None))
    monkeypatch.setattr(rc, '_classifier_lanes',
                        lambda: [{'base': 'http://x', 'model': 'm', 'key_env': '', 'key_value': 'k',
                                  'timeout': 5.0}])
    monkeypatch.setenv('ROUTER_CLASSIFY_STRUCTURED', 'off')
    monkeypatch.delenv('ROUTER_CLASSIFY_THINKING', raising=False)
    out = rc.classify('task text')
    assert out['call_meta']['thinking'] == 'none'


def test_a_rejected_thinking_param_steps_down_without_losing_the_rating(monkeypatch):
    """reasoning_effort is accepted on this endpoint, but a lane that rejects it
    must not cost the rating: step to thinking={type:disabled}, then to nothing."""
    calls = []

    def fake(lane, body):
        m = json.loads(body)
        calls.append(m.get('reasoning_effort') or m.get('thinking') or 'plain')
        if m.get('reasoning_effort'):
            raise RuntimeError('HTTP Error 400: Bad Request')
        return rc.Raw('{"categories": {"code_gen": 1}}')

    monkeypatch.setattr(rc, '_call_lane', fake)
    monkeypatch.setattr(rc, '_classifier_lanes',
                        lambda: [{'base': 'http://x', 'model': 'm', 'key_env': '', 'key_value': 'k',
                                  'timeout': 5.0}])
    monkeypatch.setenv('ROUTER_CLASSIFY_STRUCTURED', 'off')
    monkeypatch.delenv('ROUTER_CLASSIFY_THINKING', raising=False)
    monkeypatch.delenv('ROUTER_CLASSIFY_THINKING_MODE', raising=False)
    out = rc.default_llm('sys', 'text')
    assert 'code_gen' in str(out)
    assert calls[0] == 'none' and calls[-1] == {'type': 'disabled'}
