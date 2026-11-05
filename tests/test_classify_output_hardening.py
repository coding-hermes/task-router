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
