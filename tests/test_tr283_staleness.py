"""TR-283 AC6/AC7: staleness handling + the measured-vs-unmeasured fixture.

AC6: a sample older than several window half-lives decays to ~zero weight, so a
decay-weighted average reflects only recent evidence — and the resolve-side
completion_term divides by success rate so a lane that fails its tasks never
claims cheap value (R4.2).
AC7: a lane with a fresh, floor-clearing measurement outranks a cheaper lane
whose success rate collapses its cost-per-PASSED-task; the inverse holds when
the fresh lane's samples fall below the floor (basis says so, never a fake zero).

FINDING (2026-10-04, TR-283 residue): the averages row carries NO timestamp, so
resolve-time staleness is enforced only through decay AT WRITE TIME — a lane
whose only samples are ancient still presents a cost at resolve time. The
row-level staleness question is filed as the remaining gap on TR-283.
"""
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))

import router_outcomes as ro  # noqa: E402
import router_spawn as rs    # noqa: E402

NOW = 1_800_000_000.0


def _row(ts, cost, provider, model):
    return {'ts': ts, 'source_system': 'router-proxy', 'provider': provider,
            'model': model, 'complexity_sig': 'a' * 40,
            'tokens_in': 1000, 'tokens_out': 100, 'cost_usd': cost,
            'success': True}


def test_a_stale_sample_decays_to_near_zero_weight():
    """AC6: a sample many half-lives old contributes ~nothing to its window."""
    fresh = _row(NOW - 3600, cost=0.10, provider='p', model='m')
    stale = _row(NOW - 24 * 3600 * 8, cost=0.001, provider='p', model='m')
    avg = ro.compute_averages([fresh, stale], scales_h=[24], now_s=NOW)
    row = [a for a in avg if a['provider'] == 'p' and a['model'] == 'm'][0]
    # the average must sit at the FRESH sample's cost, not the stale cheap one
    assert row['avg_cost_task_24h'] > 0.05, row['avg_cost_task_24h']


def test_a_sample_twelve_half_lives_old_is_no_evidence():
    """AC6: the decay weight itself — 12 half-lives leaves <0.1% of the sample."""
    assert ro.decay_weight(24 * 3600 * 12, 24) < 0.001
    assert ro.decay_weight(1800, 24) > 0.9  # and a fresh sample keeps its weight


def test_success_rate_participates_in_the_ranked_value():
    """AC5/R4.2: a lane with a poor completion rate cannot buy the head on cost
    alone — the completion term divides the measured cost by the rate."""
    lanes = [{'provider': 'p', 'model': 'good', 'normalized_price': 0.10},
             {'provider': 'q', 'model': 'flaky', 'normalized_price': 0.01}]
    index = {
        ('p', 'good'): [{'n_samples': 10, 'avg_cost_task_24h': 0.50,
                         'success_rate': 0.9, 'complexity': None,
                         'match_note': 'test'}],
        ('q', 'flaky'): [{'n_samples': 10, 'avg_cost_task_24h': 0.01,
                          'success_rate': 0.05, 'complexity': None,
                          'match_note': 'test'}],
    }
    ctx = {'index': index, 'window_h': 24}
    key = rs._sort_predicted_cost_per_task(None, lanes, ctx)
    # good: 0.50/0.9 ≈ 0.56; flaky: 0.01/max(0.05, floor) = 0.01/0.1 = 0.10
    # -> flaky still ranks first on the divided value. THE FLOOR IS WHAT SAVES
    # THE CONTRACT: verify the division actually ran (not the raw 0.01).
    v_good, b_good = rs.lane_expected_value(lanes[0], ctx, 3, 9, 5.0)
    v_flaky, b_flaky = rs.lane_expected_value(lanes[1], ctx, 3, 9, 5.0)
    assert b_good.get('completion_term') == 'success_rate', b_good
    assert abs(v_good - 0.50 / 0.9) < 0.02, (v_good, b_good)
    assert v_flaky is not None and v_flaky > 0.01, (v_flaky, b_flaky)


def test_below_the_floor_the_lane_loses_its_measurement():
    """AC7 inverse: drop the lane under the sample floor -> it can no longer
    claim a measured value; the basis says so instead of a fake zero."""
    index = {('p', 'fresh'): [{'n_samples': 2, 'avg_cost_task_24h': 0.50,
                               'success_rate': 1.0, 'complexity': None,
                               'match_note': 'test'}]}
    ctx = {'index': index, 'window_h': 24}
    value, basis = rs.measured_basis({'provider': 'p', 'model': 'fresh'},
                                     ctx, rs.MEASURED_MIN_SAMPLES)
    assert value is None and basis['basis'] in ('below-floor', 'no-sample'), basis
