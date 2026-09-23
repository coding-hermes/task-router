"""Cost-per-task accumulation (TR-120, 2026-09-23): one row = one finished task,
and a task is many steps.

The store's identity is (source_system, session_id, model). A proxied Hermes
session calls the model once per step, so the two naive shapes are both wrong:
appending a row per step gets steps 2..N silently DROPPED by the dedupe, and
inventing a unique id per step destroys the join to the session the engine exists
to measure. `accumulate_row` merges instead — tokens/cost/wall accumulate, `turns`
and `steps` count, and the association survives.

Hermetic: temp files only, no registry, no network.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_outcomes as ro   # noqa: E402


def _row(**kw):
    base = {'source_system': 'hermes', 'session_id': 'hermes:sess-1', 'provider': 'zai-glm',
            'model': 'glm-5.3-flash', 'tokens_in': 100, 'tokens_out': 10, 'cost_usd': 0.5,
            'wall_time_s': 2.0, 'success': True, 'ts': 1000.0, 'required_categories': None,
            'complexity_sig': None, 'profile_id': None, 'task_label': None}
    base.update(kw)
    return base


def _read(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def test_first_step_appends(tmp_path):
    p = str(tmp_path / 'outcomes.jsonl')
    mode, why = ro.accumulate_row(p, _row())
    assert mode == 'appended' and 'first row' in why
    rows = _read(p)
    assert len(rows) == 1 and rows[0]['steps'] == 1


def test_second_step_accumulates_into_the_same_task_row(tmp_path):
    """The whole point: the session's steps sum onto ONE row."""
    p = str(tmp_path / 'outcomes.jsonl')
    ro.accumulate_row(p, _row(tokens_in=100, tokens_out=10, cost_usd=0.5, wall_time_s=2.0))
    mode, why = ro.accumulate_row(p, _row(tokens_in=200, tokens_out=20, cost_usd=1.5, wall_time_s=3.0,
                                          ts=2000.0, task_label='step 2'))
    assert mode == 'accumulated'
    rows = _read(p)
    assert len(rows) == 1, 'one row per task, not one per step'
    r = rows[0]
    assert (r['tokens_in'], r['tokens_out'], r['cost_usd'], r['wall_time_s']) == (300, 30, 2.0, 5.0)
    assert r['steps'] == 2 and r['turns'] == 2
    assert r['ts'] == 2000.0 and r['task_label'] == 'step 2'


def test_success_is_sticky_and_blank_never_erases(tmp_path):
    p = str(tmp_path / 'outcomes.jsonl')
    ro.accumulate_row(p, _row(success=True, task_label='served', complexity_sig='abc'))
    ro.accumulate_row(p, _row(success=False, task_label=None, complexity_sig=None))
    r = _read(p)[0]
    assert r['success'] is True, 'a task that ever succeeded is a success'
    assert r['task_label'] == 'served' and r['complexity_sig'] == 'abc'


def test_unmeasured_stays_none_not_zero(tmp_path):
    """A step with no usage must not drag the task average toward zero."""
    p = str(tmp_path / 'outcomes.jsonl')
    ro.accumulate_row(p, _row(tokens_in=None, tokens_out=None, cost_usd=None))
    ro.accumulate_row(p, _row(tokens_in=None, tokens_out=None, cost_usd=None))
    r = _read(p)[0]
    assert r['cost_usd'] is None and r['tokens_in'] is None
    ro.accumulate_row(p, _row(tokens_in=5, tokens_out=None, cost_usd=0.25))
    r = _read(p)[0]
    assert r['cost_usd'] == 0.25 and r['tokens_in'] == 5


def test_distinct_model_or_source_is_a_distinct_task(tmp_path):
    p = str(tmp_path / 'outcomes.jsonl')
    ro.accumulate_row(p, _row())
    ro.accumulate_row(p, _row(model='other-model'))
    ro.accumulate_row(p, _row(source_system='opencode'))
    ro.accumulate_row(p, _row(session_id='hermes:sess-2'))
    assert len(_read(p)) == 4, 'the key is (source_system, session_id, model)'


def test_other_lines_are_byte_preserved(tmp_path):
    """Accumulation must rewrite ONLY the matched line."""
    p = str(tmp_path / 'outcomes.jsonl')
    before = _row(session_id='other', model='m-before')
    after = _row(session_id='other', model='m-after')
    open(p, 'w').write(json.dumps(before) + '\n' + json.dumps(_row()) + '\n' + json.dumps(after) + '\n')
    ro.accumulate_row(p, _row(tokens_in=1, tokens_out=1, cost_usd=0.25))
    rows = _read(p)
    assert [r['model'] for r in rows] == ['m-before', 'glm-5.3-flash', 'm-after']
    assert rows[0] == before and rows[2] == after
    assert rows[1]['tokens_in'] == 101


def test_rows_outside_the_tail_window_are_not_merged(tmp_path):
    """Documented bound: the scan is the tail, so a bump far back in a huge store
    appends a new row instead of rewriting a line it did not read. O(tail), never
    O(store) — the alternative is reading 90 MB on a live request path."""
    p = str(tmp_path / 'outcomes.jsonl')
    ro.accumulate_row(p, _row())
    filler = json.dumps(_row(session_id='filler', model='m')) + '\n'
    with open(p, 'a') as f:
        for i in range(200):
            f.write(json.dumps(_row(session_id=f'filler-{i}', model='m')) + '\n')
    mode, why = ro.accumulate_row(p, _row(), tail_bytes=2048)
    assert mode == 'appended' and 'first row' in why


def test_accumulated_row_feeds_the_averages_as_one_task(tmp_path):
    """Ties the merge to the consumer: the averages read avg_cost_task_* per
    (source_system, provider, model, sig) bucket, so one accumulated row means
    cost PER TASK, not cost per step."""
    p = str(tmp_path / 'outcomes.jsonl')
    ro.accumulate_row(p, _row(cost_usd=0.4, tokens_in=100, tokens_out=10))
    ro.accumulate_row(p, _row(cost_usd=0.6, tokens_in=100, tokens_out=10))
    rows = _read(p)
    avg = ro.compute_averages(rows, now_s=rows[0]['ts'])
    bucket = [a for a in avg if a['model'] == 'glm-5.3-flash'][0]
    assert bucket['avg_cost_task_24h'] == pytest.approx(1.0), 'the task cost 1.0 total, not 0.5 average'
    assert bucket['avg_turns_24h'] == pytest.approx(2.0)
    assert bucket['avg_tokens_in_24h'] == pytest.approx(200.0)
