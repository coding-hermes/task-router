"""Regression: untrusted model output must never crash classify()."""
import sys
sys.path.insert(0, '/home/kara/proj-wt-tr388/scripts')
import router_classify as rc


def _fake(answer: str):
    def llm(prompt, text):
        return answer
    return llm


def test_ranking_as_int_does_not_crash_and_keeps_the_ratings():
    answer = '{"ratings": {"code_gen": -2, "test": 0}, "ranking": 3, "confidence": 0.5}'
    out = rc.classify('do a thing', llm=_fake(answer))
    assert out['matrix'] == {'code_gen': -2, 'test': 0}, out.get('matrix')
    assert out['ranking'] == []
    assert any('unexpected type int' in p for p in out['problems']), out['problems']


def test_ranking_as_string_is_tolerated():
    answer = '{"ratings": {"debug": -3}, "ranking": "debug"}'
    out = rc.classify('do a thing', llm=_fake(answer))
    assert out['matrix'] == {'debug': -3}
    assert out['ranking'] == ['debug']


def test_rating_value_as_list_does_not_crash():
    answer = '{"ratings": {"code_gen": [-2]}, "ranking": ["code_gen"]}'
    out = rc.classify('do a thing', llm=_fake(answer))
    assert isinstance(out['matrix'], dict)


def test_garbage_answer_degrades_with_a_problem():
    out = rc.classify('do a thing', llm=_fake('not json at all'))
    assert out['matrix'] in (None, {})
    assert out['problems'], 'a failed parse must be recorded, never silent'


def test_the_schema_states_the_prompt_contract_not_a_flat_matrix():
    """The response_format schema must describe the envelope prompt-v3 asks for.

    A flat {category: level} schema produced matrix={} AND the "ranking": 3 crash:
    a schema-compliant flat answer has no "ratings" key, and the flat
    additionalProperties coerced every envelope field to an integer.
    """
    import router_classify as rc
    schema = rc._matrix_schema(['code_gen', 'debug'])
    assert schema['type'] == 'object'
    props = schema['properties']
    assert 'ratings' in props, 'the prompt returns ratings; the schema must declare it'
    assert props['ratings']['type'] == 'object'
    assert props['ratings']['additionalProperties'] == {'type': 'integer', 'minimum': -5, 'maximum': 5}
    assert props['ranking']['type'] == 'array'
    assert props['ranking']['items'] == {'type': 'string'}, 'ranking is a list, never an integer'
    assert props['confidence']['type'] == 'number'
    assert schema['required'] == ['ratings']
    # the envelope must NOT be described as "all values are integers"
    assert 'additionalProperties' not in schema


def test_the_schema_does_not_enumerate_categories():
    """Naming the registry's categories in the schema corrupts the answer.

    Measured live 2026-11-04 (same prompt, same lane, only the schema differing):
      24 named categories  44,091 ms  '{"debug": 2, "code_gen: 1, "  : 0, ...' (padded junk)
      open ratings object     776 ms  '{"debug": 1, "code_gen": 1, "test": 1}'
    The prompt lists the vocabulary; validate_matrix() gates what is admissible.
    """
    import router_classify as rc
    cats = ['agent_tick', 'code_gen', 'creative', 'debug', 'delegation', 'review']
    schema = rc._matrix_schema(cats)
    assert 'properties' not in schema['properties']['ratings'], \
        'a named category property makes the model pad the object'
    assert schema['properties']['ratings']['additionalProperties']['maximum'] == 5


def test_a_schema_conforming_answer_yields_a_populated_matrix():
    """The positive case the schema was failing: envelope in -> non-empty matrix."""
    answer = ('{"ratings": {"code_gen": -2, "test": 0}, "ranking": ["code_gen", "test"], '
              '"confidence": 0.8, "reason": "small code change with a test"}')
    out = rc.classify('fix the off-by-one and add a test', llm=_fake(answer))
    assert out['matrix'] == {'code_gen': -2, 'test': 0}, out['matrix']
    assert out['ranking'] == ['code_gen', 'test']
    assert out['confidence'] == 0.8
