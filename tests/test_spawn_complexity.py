"""The router must look at the TASK, not just its profile (TR-124, Bane 2026-09-23).

Bane's worry, verbatim: "the model was saying P2_agentic and it was always a fixed
list and we were never getting it to actually look at complexity directly".

Verified before this change: `router_spawn.py` had ZERO references to the
classifier, its only inputs were `--profile <id>` / hand-typed `--profile-req`,
and the live fleet calls it as `router_spawn.py "$PROJECT" --format json` — so
the spawn path could not consult complexity at all. Complexity existed only on
the proxy path.

After this change a task's own text is scored and its signed matrix becomes the
requirement list (the same 1:1 mapping the proxy uses). These contracts pin the
conversion, the fail-open behaviour, and the wiring into main().
"""
import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_spawn as rs   # noqa: E402


# ---------- conversion: matrix -> requirement levels (1:1, like the proxy) ----------

def test_matrix_becomes_the_requirement_list():
    adhoc, meta = rs.complexity_requirements(
        'refactor the scheduler',
        classify_fn=lambda t: {'matrix': {'code_gen': 4, 'test': 2, 'debug': -1},
                               'confidence': 0.9, 'model': 'deepseek-v4.1-flash',
                               'complexity_sig': 'abc'})
    assert adhoc == ['code_gen=4', 'debug=-1', 'test=2']
    assert meta['source'] == 'stub' and meta['degraded'] is False
    assert meta['matrix'] == {'code_gen': 4, 'test': 2, 'debug': -1}
    assert meta['confidence'] == 0.9 and meta['complexity_sig'] == 'abc'


def test_jev_shaped_result_carries_band_and_score():
    adhoc, meta = rs.complexity_requirements(
        'hard task',
        classify_fn=lambda t: {'matrix': {'reasoning': 3}, 'score': 0.81,
                               'band': 'mid', 'model': 'jev', 'confidence': 0.78},
        classify_name='jev')
    assert adhoc == ['reasoning=3']
    assert meta['source'] == 'jev' and meta['band'] == 'mid' and meta['score'] == 0.81


def test_empty_text_degrades_and_never_calls_the_scorer():
    called = []
    adhoc, meta = rs.complexity_requirements('   ', classify_fn=lambda t: called.append(t))
    assert adhoc is None and meta['degraded'] is True
    assert meta['degrade_reason'] == 'empty task text'
    assert called == []


def test_a_scorer_that_raises_is_fail_open():
    def boom(text):
        raise RuntimeError('connection refused')
    adhoc, meta = rs.complexity_requirements('task', classify_fn=boom)
    assert adhoc is None, 'no matrix means the caller keeps its profile'
    assert meta['degraded'] is True
    assert 'connection refused' in meta['degrade_reason']


def test_no_matrix_is_reported_with_the_scorers_own_problems():
    adhoc, meta = rs.complexity_requirements(
        'task', classify_fn=lambda t: {'matrix': None,
                                       'problems': ['no JSON object in classifier output']})
    assert adhoc is None and meta['degraded'] is True
    assert 'no JSON object' in meta['degrade_reason']


def test_non_numeric_levels_are_ignored_and_all_garbage_degrades():
    adhoc, meta = rs.complexity_requirements(
        'task', classify_fn=lambda t: {'matrix': {'code_gen': 4, 'junk': 'high'}})
    assert adhoc == ['code_gen=4'], 'a junk level must not silently become a requirement'
    adhoc2, meta2 = rs.complexity_requirements(
        'task', classify_fn=lambda t: {'matrix': {'junk': 'high'}})
    assert adhoc2 is None and meta2['degraded'] is True


# ---------- the board row IS the complexity input ----------

def _board(tmp_path, rows):
    d = tmp_path / '.coding-hermes' / 'board'
    d.mkdir(parents=True)
    p = d / 'tasks.jsonl'
    p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return str(p)


def test_task_text_comes_from_the_board_row(tmp_path):
    p = _board(tmp_path, [{'id': 'TR-1', 'title': 'Fix the flaky judge',
                           'description': 'It times out on big diffs.',
                           'tags': ['judge', 'flaky']}])
    text, src = rs.task_text_for(task_id='TR-1', board=p)
    assert 'Fix the flaky judge' in text and 'times out' in text and 'judge flaky' in text
    assert src == p


def test_a_missing_task_says_so(tmp_path):
    p = _board(tmp_path, [{'id': 'TR-1', 'title': 'x'}])
    text, why = rs.task_text_for(task_id='TR-404', board=p)
    assert text is None and 'not found' in why


# ---------- main() wiring ----------

def test_main_uses_complexity_and_says_which_scorer(monkeypatch, capsys):
    """Complexity beats --profile-req, and the derivation is in the JSON."""
    monkeypatch.setattr(rs, 'complexity_requirements',
                        lambda text, scorer='auto': (['reasoning=5', 'code_gen=4'],
                                                     {'source': 'jev', 'degraded': False,
                                                      'matrix': {'reasoning': 5, 'code_gen': 4},
                                                      'confidence': 0.8}))
    monkeypatch.setattr(sys, 'argv', ['router_spawn.py', '--profile-req', 'code_gen=0',
                                      '--prompt', 'a very hard task', '--format', 'json'])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    assert out['complexity']['source'] == 'jev'
    assert out['complexity']['degraded'] is False
    assert out['complexity']['text_source'] == '--prompt'
    assert out['complexity']['text_chars'] == len('a very hard task')
    assert out['resolved_as'] == 'adhoc', 'the complexity levels are the requirements'


def test_main_degrades_to_the_profile_when_scoring_fails(monkeypatch, capsys):
    monkeypatch.setattr(rs, 'complexity_requirements',
                        lambda text, scorer='auto': (None, {'source': None, 'degraded': True,
                                                            'degrade_reason': 'classifier down'}))
    monkeypatch.setattr(sys, 'argv', ['router_spawn.py', '--prompt', 'task',
                                      '--profile', 'P1_CODING', '--format', 'json'])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    assert out['complexity']['degraded'] is True
    assert out['complexity']['degrade_reason'] == 'classifier down'
    assert out.get('chain'), 'fail-open: the caller still gets a chain'
    assert out['resolved_as'] != 'adhoc'


def test_prompt_alone_is_a_valid_input(monkeypatch, capsys):
    """No project, no profile — the task text is enough to resolve."""
    monkeypatch.setattr(rs, 'complexity_requirements',
                        lambda text, scorer='auto': (['code_gen=2'], {'source': 'classifier',
                                                                      'degraded': False}))
    monkeypatch.setattr(sys, 'argv', ['router_spawn.py', '--prompt', 'write a test',
                                      '--format', 'json'])
    rs.main()
    out = json.loads(capsys.readouterr().out)
    assert 'error' not in out or out.get('error') is None
    assert out['complexity']['source'] == 'classifier'
