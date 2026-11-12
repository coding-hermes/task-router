"""TR-400 — cost per (provider, model, reasoning_effort).

Pins the three things the row's acceptance names:
  * reasoning tokens are billed as OUTPUT (the cost formula),
  * a cell with no measurement at that effort is an EXPLAINED NULL, never a
    guessed multiplier and never 0,
  * the same model at a higher effort is never priced below its lower-effort
    cost (the monotonicity spot check, proven to FAIL on a violation).
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))


def _load():
    spec = importlib.util.spec_from_file_location(
        'cost_effort', os.path.join(REPO, 'scripts', 'cost_effort.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ce = _load()


def _lane(in_pm=1.0, out_pm=10.0, **kw):
    row = {'provider': 'p', 'model': 'm', 'public_in_per_m': in_pm,
           'public_out_per_m': out_pm}
    row.update(kw)
    return row


def _measured(n=5, tin=1000.0, tout=500.0, trea=0.0):
    return {'n': n, 'in': [tin] * n, 'out': [tout] * n, 'reasoning': [trea] * n}


def test_reasoning_tokens_billed_as_output():
    """cost = (in*in_pm + (out+reasoning)*out_pm)/1e6 — reasoning is output."""
    rows = ce.build_rows(lanes={('p', 'm'): _lane()},
                         efforts={'m': {'default'}},
                         measured={('p', 'm', 'default'): _measured(trea=2000.0)})
    assert len(rows) == 1
    r = rows[0]
    assert r['basis'] == 'measured'
    # 1000*1.0 + (500+2000)*10.0 = 1000 + 25000 = 26000 -> /1e6
    assert r['est_cost_per_task'] == pytest.approx(0.026, rel=1e-9)
    assert r['tokens_reasoning'] == 2000.0


def test_reasoning_tokens_raise_cost_above_no_reasoning():
    base = ce.build_rows(lanes={('p', 'm'): _lane()}, efforts={'m': {'default'}},
                         measured={('p', 'm', 'default'): _measured(trea=0.0)})[0]
    think = ce.build_rows(lanes={('p', 'm'): _lane()}, efforts={'m': {'default'}},
                          measured={('p', 'm', 'default'): _measured(trea=1000.0)})[0]
    assert think['est_cost_per_task'] > base['est_cost_per_task']


def test_unpriced_lane_is_explained_null_not_zero():
    rows = ce.build_rows(lanes={('p', 'm'): _lane(in_pm=None, out_pm=None)},
                         efforts={'m': {'default'}},
                         measured={('p', 'm', 'default'): _measured()})
    r = rows[0]
    assert r['est_cost_per_task'] is None
    assert r['basis'] == 'unpriced'
    assert 'no published price split' in r['null_reason']


def test_effort_without_outcomes_is_explained_null():
    """A supported effort with no stamped rows is NULL + reason, never priced."""
    rows = ce.build_rows(lanes={('p', 'm'): _lane()},
                         efforts={'m': {'default', 'high'}},
                         measured={('p', 'm', 'default'): _measured()})
    by_eff = {r['reasoning_effort']: r for r in rows}
    assert by_eff['default']['est_cost_per_task'] is not None
    assert by_eff['high']['est_cost_per_task'] is None
    assert by_eff['high']['basis'] == 'unmeasured'
    assert 'no outcome rows at effort=' in by_eff['high']['null_reason']


def test_every_row_has_basis_and_null_reason_or_cost():
    """House data rule: every field filled, or an explicit NULL with a reason."""
    rows = ce.build_rows(lanes={('p', 'm'): _lane()},
                         efforts={'m': {'default', 'high'}},
                         measured={('p', 'm', 'default'): _measured()})
    for r in rows:
        assert r['basis']
        if r['est_cost_per_task'] is None:
            assert r['null_reason']
        else:
            assert r['source_ref']


def test_check_catches_high_priced_below_low():
    """The spot check must FAIL when a higher effort is cheaper (the defect)."""
    rows = [
        {'provider': 'p', 'model': 'm', 'reasoning_effort': 'low',
         'est_cost_per_task': 0.02},
        {'provider': 'p', 'model': 'm', 'reasoning_effort': 'high',
         'est_cost_per_task': 0.01},
    ]
    ok, problems, stats = ce.check(rows)
    assert not ok
    assert stats['comparable_pairs'] == 1
    assert 'priced BELOW' in problems[0]


def test_check_passes_on_monotone_costs():
    rows = [
        {'provider': 'p', 'model': 'm', 'reasoning_effort': 'low',
         'est_cost_per_task': 0.01},
        {'provider': 'p', 'model': 'm', 'reasoning_effort': 'high',
         'est_cost_per_task': 0.03},
    ]
    ok, problems, _stats = ce.check(rows)
    assert ok and not problems


def test_check_ignores_default_effort_in_ranking():
    """'default' is a pointer, not a level — it never forms a comparable pair."""
    rows = [
        {'provider': 'p', 'model': 'm', 'reasoning_effort': 'default',
         'est_cost_per_task': 0.05},
        {'provider': 'p', 'model': 'm', 'reasoning_effort': 'high',
         'est_cost_per_task': 0.01},
    ]
    ok, problems, stats = ce.check(rows)
    assert ok and not problems
    assert stats['comparable_pairs'] == 0


def test_seed_declares_cost_effort_table_and_view():
    """The registry must carry the table AND the cost@effort view."""
    src = open(os.path.join(REPO, 'scripts', 'router_seed.py')).read()
    assert "'cost_effort': [('provider', 'VARCHAR')" in src
    assert "'task_profile_requirements', 'cost_effort']" in src
    assert 'CREATE VIEW v_cost_effort AS' in src
    assert 'score_sum' in src


def test_committed_table_matches_generator_columns():
    """The committed data file carries exactly the declared columns."""
    path = os.path.join(REPO, 'data', 'tables', 'cost_effort.jsonl')
    if not os.path.exists(path):
        pytest.skip('cost_effort.jsonl not generated in this checkout')
    import json
    with open(path) as f:
        first = json.loads(f.readline())
    assert tuple(first.keys()) == ce.COLUMNS
