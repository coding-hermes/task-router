"""TR-065 tests: complexity-SET signatures, signature-keyed buckets, metric
coverage, merge-by-signature, and ratio mixes over the new metrics."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro

NOW = 1_800_000_000.0


def _row(model='m1', provider='p1', sig=None, req=None, cost=1.0, age_s=0,
         turns=2, tin=100, tout=10, success=True, source='hermes'):
    r = {'source_system': source, 'session_id': f'{model}-{age_s}-{cost}-{turns}',
         'provider': provider, 'model': model, 'cost_usd': cost,
         'wall_time_s': 5.0, 'turns': turns, 'tokens_in': tin, 'tokens_out': tout,
         'tokens_reasoning': 0, 'success': success, 'ts': NOW - age_s}
    if sig:
        r['complexity_sig'] = sig
    if req:
        r['required_categories'] = req
    return r


# ---------- signature ----------

def test_signature_is_order_independent_and_level_sensitive():
    a = ro.complexity_sig({'code_gen': 2, 'test': 1})
    b = ro.complexity_sig({'test': 1, 'code_gen': 2})
    c = ro.complexity_sig({'code_gen': 3, 'test': 1})
    assert a == b, 'dict order must not change the bucket'
    assert a != c, 'level changes must change the bucket'


def test_signature_accepts_the_three_declaration_shapes():
    want = ro.complexity_sig({'code_gen': 2})
    assert ro.complexity_sig([{'category': 'code_gen', 'level': 2}]) == want
    assert ro.complexity_sig(['code_gen=2']) == want
    assert ro.complexity_sig([{'category': 'code_gen'}]) == ro.complexity_sig({'code_gen': 0})


def test_signature_none_when_unusable():
    assert ro.complexity_sig(None) is None
    assert ro.complexity_sig('P1_CODING') is None      # a profile id is not a set
    assert ro.complexity_sig({'x': 'not-a-level'}) is None


def test_row_signature_precedence():
    assert ro.row_complexity_sig({'complexity_sig': 'abc'}) == 'abc'
    assert ro.row_complexity_sig({'required_categories': {'a': 1}}) == ro.complexity_sig({'a': 1})
    assert ro.row_complexity_sig({'complexity': {'a': 1}}) == ro.complexity_sig({'a': 1})
    assert ro.row_complexity_sig({'profile_id': 'P4_SECURITY'}) == 'profile:P4_SECURITY'
    assert ro.row_complexity_sig({}) is None


# ---------- signature-keyed buckets ----------

def test_one_average_per_complexity_set_not_per_model():
    """The core requirement: same model, two different complexity SETS -> two
    independent buckets (that is how 'cost per task conditioned on complexity'
    is stored)."""
    sig_a = ro.complexity_sig({'code_gen': 2, 'test': 1})
    sig_b = ro.complexity_sig({'security': 2, 'review': 0})
    rows = [_row(sig=sig_a, req={'code_gen': 2, 'test': 1}, cost=1.0),
            _row(sig=sig_b, req={'security': 2, 'review': 0}, cost=9.0),
            _row(sig=None, cost=5.0)]  # undeclared -> its own (None) bucket
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW)
    assert len(out) == 3, 'one bucket per signature + the undeclared bucket'
    by_sig = {r['complexity_sig']: r for r in out}
    assert by_sig[sig_a]['avg_cost_task_24h'] == pytest.approx(1.0)
    assert by_sig[sig_b]['avg_cost_task_24h'] == pytest.approx(9.0)
    assert by_sig[None]['avg_cost_task_24h'] == pytest.approx(5.0)
    assert by_sig[sig_a]['required_categories'] == {'code_gen': 2, 'test': 1}


def test_bucket_carries_every_sortable_metric():
    sig = ro.complexity_sig({'code_gen': 2})
    rows = [_row(sig=sig, cost=2.0, turns=4, tin=300, tout=50),
            _row(sig=sig, cost=4.0, turns=6, tin=500, tout=150)]
    b = ro.compute_averages(rows, scales_h=[24], now_s=NOW)[0]
    assert b['avg_cost_task_24h'] == pytest.approx(3.0)
    assert b['avg_turns_24h'] == pytest.approx(5.0)
    assert b['avg_tokens_in_24h'] == pytest.approx(400.0)
    assert b['avg_tokens_out_24h'] == pytest.approx(100.0)
    assert b['avg_tokens_total_24h'] == pytest.approx(500.0)
    assert b['avg_wall_time_24h'] == pytest.approx(5.0)


def test_metrics_stay_null_without_samples():
    sig = ro.complexity_sig({'code_gen': 2})
    b = ro.compute_averages([_row(sig=sig, cost=None, turns=None, tin=None, tout=None,
                                  success=None)],
                            scales_h=[24], now_s=NOW)[0]
    for f in ('avg_cost_task_24h', 'avg_turns_24h', 'avg_tokens_in_24h',
              'avg_tokens_total_24h', 'success_rate'):
        assert b[f] is None, f'{f} must be None, never 0'


def test_merge_is_signature_aware():
    sig_a = ro.complexity_sig({'code_gen': 2})
    sig_b = ro.complexity_sig({'security': 2})
    rows = [
        {'provider': 'p', 'model': 'm', 'complexity_sig': sig_a, 'n_samples': 10,
         'avg_cost_task_24h': 1.0, 'n_completed': 0, 'n_success_known': 0},
        {'provider': 'p', 'model': 'm', 'complexity_sig': sig_b, 'n_samples': 10,
         'avg_cost_task_24h': 5.0, 'n_completed': 0, 'n_success_known': 0},
        {'provider': 'p', 'model': 'm', 'complexity_sig': sig_a, 'n_samples': 10,
         'avg_cost_task_24h': 3.0, 'n_completed': 0, 'n_success_known': 0},
    ]
    merged = {r['complexity_sig']: r for r in ro.merge_average_rows(rows)}
    assert merged[sig_a]['avg_cost_task_24h'] == pytest.approx(2.0)
    assert merged[sig_b]['avg_cost_task_24h'] == pytest.approx(5.0)
    assert 'profile:' not in json.dumps(merged)
