"""TR-354 correction: the outcome floor must not charge a model for the router's refusals.

Live evidence (2026-11-04, 456k-row store): 12,759 of ~57k counted failures were produced by the
router itself — `hop-wall-timeout` (its own per-hop ceiling), `no-hops` (empty ladder) and
`unservable-rating`. Each was written `success=False` against the lane that never got to answer,
so agents that were merely slow on fleet-sized ticks fell under the 0.35 floor and were excluded
from every ladder. That refusal then produced more `no-hops` rows, deepening the spiral.

After the correction the same ledger reads: gpt-6.1-sol 0.19 -> 1.00, deepseek-flash 0.17 -> 0.77,
gpt-6-luna 0.33 -> 0.75, while a lane that genuinely fails keeps its low rate
(deepseek-v4-pro 56/37,643 = 0.00).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import router_outcomes as RO  # noqa: E402


def _row(model, success, reason=None, provider='xkiro'):
    return {
        'source_system': 'router-proxy',
        'provider': provider,
        'model': model,
        'success': success,
        'failure_reason': reason,
        'wall_time_s': 600.0,
        'ts': 1_800_000_000.0,
    }


def _bucket(rows, model):
    avgs = RO.compute_averages(rows, merge_backends=True)
    return [a for a in avgs if a['model'] == model][0]


def test_router_side_failures_do_not_lower_the_rate():
    rows = ([_row('m1', True)] * 8
            + [_row('m1', False, 'hop-wall-timeout')] * 20)
    b = _bucket(rows, 'm1')
    assert b['n_router_side'] == 20
    assert b['n_verdicts_raw'] == 28
    assert b['n_success_known'] == 8          # attributable verdicts only
    assert b['success_rate'] == 1.0


def test_genuine_failures_still_lower_the_rate():
    rows = [_row('m2', True)] * 2 + [_row('m2', False, 'upstream-5xx')] * 8
    b = _bucket(rows, 'm2')
    assert b['n_router_side'] == 0
    assert b['n_success_known'] == 10
    assert b['success_rate'] == 0.2           # stays under the 0.35 floor


def test_unattributed_failure_stays_in_the_denominator():
    """A failure nobody can explain is still a failure — never silently excused."""
    rows = [_row('m3', True)] * 5 + [_row('m3', False, None)] * 5
    b = _bucket(rows, 'm3')
    assert b['n_unattributed'] == 5
    assert b['n_success_known'] == 10
    assert b['success_rate'] == 0.5


def test_null_verdicts_are_excluded_like_before():
    rows = [_row('m4', True)] * 4 + [_row('m4', None)] * 40
    b = _bucket(rows, 'm4')
    assert b['n_success_known'] == 4
    assert b['success_rate'] == 1.0


def test_all_four_router_side_reasons_are_recognised():
    for reason in ('hop-wall-timeout', 'no-hops', 'unservable-rating', 'gateway-draining'):
        rows = [_row('m5', True)] * 3 + [_row('m5', False, reason)] * 9
        b = _bucket(rows, 'm5')
        assert b['n_router_side'] == 9, reason
        assert b['success_rate'] == 1.0, reason


def test_bucket_with_only_router_side_failures_has_no_rate():
    rows = [_row('m6', False, 'no-hops')] * 12
    b = _bucket(rows, 'm6')
    assert b['n_success_known'] == 0
    assert b['success_rate'] is None          # never a fabricated 0
