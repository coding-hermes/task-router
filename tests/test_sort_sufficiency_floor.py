"""TR-183 precondition: a measured cost must be EARNED (the sample floor)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_spawn as rs  # noqa: E402


def _stats(*triples):
    """(provider, model, n_samples, avg_cost) -> the lane_stats index shape."""
    index = {}
    for prov, model, n, cost in triples:
        index[(prov, model)] = [{'n_samples': n, 'avg_cost_task_24h': cost,
                                 'complexity': None, 'match_note': 'test'}]
    return index


LANES = [{'provider': 'p', 'model': 'm', 'normalized_price': 0.10},
         {'provider': 'q', 'model': 'n', 'normalized_price': 0.01}]


def _key(fn, m):
    return fn(m)


def test_one_sample_is_not_evidence():
    ctx = {'index': _stats(('p', 'm', 1, 0.0001), ('q', 'n', 1, 0.9))}
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    for m in LANES:
        key(m)
    assert ctx['_sort_basis']['ranked_on_measurement'] == 0
    assert ctx['_sort_basis']['fell_back_to_price'] == 2
    # The proof that the sample was ignored: the 1-sample data says p/m is cheaper per task
    # (0.0001 vs 0.9) while PRICE says q/n is cheaper (0.01 vs 0.10). Price wins, so the
    # single-task average did not decide anything.
    assert key(LANES[1]) < key(LANES[0])
    assert key(LANES[0]) == (1, 0.10) and key(LANES[1]) == (1, 0.01)


def test_enough_samples_earn_a_measurement():
    ctx = {'index': _stats(('p', 'm', 4, 0.0005), ('q', 'n', 4, 0.02))}
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    assert key(LANES[0]) < key(LANES[1])          # measured: p/m is genuinely cheaper per task
    assert ctx['_sort_basis']['ranked_on_measurement'] == 2
    assert ctx['_sort_basis']['fell_back_to_price'] == 0


def test_the_floor_is_configurable_and_zero_restores_the_old_behaviour():
    ctx = {'index': _stats(('p', 'm', 1, 0.0005), ('q', 'n', 1, 0.02))}
    key = rs._sort_predicted_cost_per_task(0, LANES, ctx)
    assert key(LANES[0]) < key(LANES[1])          # floor 0: the single sample decides again
    assert ctx['_sort_basis']['floor_samples'] == 0
    ctx2 = {'index': _stats(('p', 'm', 5, 0.0005))}
    key2 = rs._sort_predicted_cost_per_task(9, LANES, ctx2)
    assert key2(LANES[0]) == (1, 0.10)            # raised floor: even 5 samples is not enough
    assert ctx2['_sort_basis']['floor_samples'] == 9


def test_a_measured_lane_sorts_ahead_of_an_unmeasured_one():
    ctx = {'index': _stats(('p', 'm', 5, 0.05))}
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    assert key(LANES[0]) < key(LANES[1])
    assert key(LANES[1])[0] == 1                  # unknown sinks, it is never the cheapest


def test_a_zero_cost_measurement_is_kept_distinct_from_no_measurement():
    ctx = {'index': _stats(('p', 'm', 3, 0.0))}
    value, b = rs.measured_basis(LANES[0], ctx, None)
    assert value == 0.0 and b['basis'] == 'measured'
    value2, b2 = rs.measured_basis(LANES[1], ctx, None)
    assert value2 is None and b2['basis'] == 'no-sample'


def test_a_lane_with_no_stats_row_reports_no_sample_not_below_floor():
    ctx = {'index': {}}
    _value, b = rs.measured_basis(LANES[0], ctx, None)
    assert b['basis'] == 'no-sample' and b['n_samples'] is None


def test_an_under_measured_chain_degrades_to_price_and_names_the_reason():
    """The finding this gate exists for: 2 of 65 live lanes cleared the floor and those two
    decided the head. Coverage is a precondition, so an ordering that would rest on 3% of the
    chain does not get applied — it degrades to price and says why."""
    index = _stats(('p', 'm', 5, 0.0001))
    for i in range(9):
        index[('z%d' % i, 'lane%d' % i)] = [{'n_samples': 5, 'avg_cost_task_24h': None,
                                             'complexity': None}]
    lanes = LANES + [{'provider': 'z%d' % i, 'model': 'lane%d' % i} for i in range(9)]
    ctx = {'index': index}
    key = rs._sort_predicted_cost_per_task(None, lanes, ctx)
    b = ctx['_sort_basis']
    assert b['effective'] == 'price' and b['reason'] == 'below-coverage-floor'
    assert b['ranked_on_measurement'] == 1 and b['lanes'] == 11
    assert b['coverage'] == round(1 / 11, 4)
    # and the measured lane did NOT get promoted: this is the price order
    assert key(lanes[0]) == (1, 0.10) and key(lanes[1]) == (1, 0.01)


def test_enough_coverage_applies_the_measured_ordering():
    index = _stats(('p', 'm', 5, 0.0005), ('q', 'n', 5, 0.02))
    ctx = {'index': index}
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    b = ctx['_sort_basis']
    assert b['effective'] == 'measured' and 'reason' not in b and b['coverage'] == 1.0
    assert key(LANES[0]) < key(LANES[1])


def test_the_coverage_gate_can_be_disabled_for_an_experiment():
    index = _stats(('p', 'm', 5, 0.0005))
    lanes = LANES + [{'provider': 'z', 'model': 'other'}]
    ctx = {'index': index}
    rs._sort_predicted_cost_per_task('3:0', lanes, ctx)
    assert ctx['_sort_basis']['effective'] == 'measured'
    assert ctx['_sort_basis']['min_coverage'] == 0.0


def test_an_empty_lane_list_reports_no_lanes_rather_than_zero_coverage():
    ctx = {'index': {}}
    rs._sort_predicted_cost_per_task(None, [], ctx)
    b = ctx['_sort_basis']
    assert b['effective'] == 'price' and b['reason'] == 'no-lanes' and b['coverage'] == 0.0


def test_the_default_sort_is_still_price_so_nothing_flips_silently():
    """The floor is the PRECONDITION for the TR-183 re-rank, not the re-rank."""
    assert rs.DEFAULT_SORT == 'price'
