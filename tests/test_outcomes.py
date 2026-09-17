"""Unit tests for router_outcomes (TR-049 phase 1): decay math, bucketing,
rolling averages, idempotent import. No network, no live DB."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro

NOW = 1_800_000_000.0


def _row(model='m1', provider='p1', complexity=None, cost=1.0, age_s=0, ts=None,
         success=True, source='test'):
    return {'source_system': source, 'session_id': f'{source}-{model}-{age_s}-{cost}',
            'complexity': complexity, 'provider': provider, 'model': model,
            'turns': 1, 'tokens_in': 100, 'tokens_out': 10, 'tokens_reasoning': 0,
            'cost_usd': cost, 'wall_time_s': 5.0, 'success': success,
            'ts': ts if ts is not None else NOW - age_s}


def test_decay_weight_half_life():
    assert ro.decay_weight(0, 24) == 1.0
    assert ro.decay_weight(24 * 3600, 24) == pytest.approx(0.5)
    assert ro.decay_weight(48 * 3600, 24) == pytest.approx(0.25)
    assert ro.decay_weight(24 * 3600, 72) == pytest.approx(0.7937, rel=1e-3)  # 1d old on 3d scale


def test_bucket_avg_weights_recent_samples():
    rows = [_row(cost=1.0, age_s=0), _row(cost=3.0, age_s=24 * 3600)]  # 1d scale
    avg = ro.bucket_avg(rows, 24, now_s=NOW)
    assert avg == pytest.approx((1.0 + 0.5 * 3.0) / 1.5)  # ~1.667, recent dominates


def test_bucket_avg_no_costs_returns_none():
    rows = [{**_row(), 'cost_usd': None}]
    assert ro.bucket_avg(rows, 24, now_s=NOW) is None  # never fabricate


def test_compute_averages_per_backend_isolation():
    rows = [_row(source='hermes', provider='p1', model='m1', complexity='P1', cost=2.0),
            _row(source='opencode', provider='p1', model='m1', complexity='P1', cost=8.0)]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW)
    assert len(out) == 2  # NOT merged: two buckets
    by_src = {e['source_system']: e for e in out}
    assert by_src['hermes']['avg_cost_task_24h'] == pytest.approx(2.0)
    assert by_src['opencode']['avg_cost_task_24h'] == pytest.approx(8.0)
    # field-position guard: labels must not shift (regression 2026-09-17)
    assert by_src['hermes']['provider'] == 'p1' and by_src['hermes']['model'] == 'm1'


def test_compute_averages_merge_opt_in():
    rows = [_row(source='hermes', provider='p1', model='m1', complexity='P1', cost=2.0),
            _row(source='opencode', provider='p1', model='m1', complexity='P1', cost=8.0)]
    out = ro.compute_averages(rows, scales_h=[24], merge_backends=True, now_s=NOW)
    assert len(out) == 1
    assert out[0]['avg_cost_task_24h'] == pytest.approx(5.0)
    assert 'source_system' not in out[0]
    assert out[0]['provider'] == 'p1' and out[0]['model'] == 'm1' and out[0]['complexity'] == 'P1'


def test_complexity_is_part_of_the_bucket():
    rows = [_row(complexity='P1', cost=1.0), _row(complexity='P4', cost=9.0)]
    out = ro.compute_averages(rows, scales_h=[24], merge_backends=True, now_s=NOW)
    assert len(out) == 2
    costs = {e['complexity']: e['avg_cost_task_24h'] for e in out}
    assert costs['P1'] == pytest.approx(1.0) and costs['P4'] == pytest.approx(9.0)
    assert all(e['provider'] == 'p1' and e['model'] == 'm1' for e in out)


def test_append_rows_idempotent(tmp_path):
    p = str(tmp_path / 'outcomes.jsonl')
    rows = [_row(), _row(model='m2')]
    assert ro.append_rows(p, rows) == 2
    assert ro.append_rows(p, rows) == 0  # same (source, session, model) skipped
    assert ro.append_rows(p, [_row(model='m3')]) == 1
    assert len([l for l in open(p) if l.strip()]) == 3


def test_outcomes_and_averages_gitignored():
    gi = os.path.join(ro.REPO, '.gitignore')
    text = open(gi).read() if os.path.exists(gi) else ''
    assert 'data/state/outcomes.jsonl' in text
    assert 'data/state/outcomes-averages.jsonl' in text
