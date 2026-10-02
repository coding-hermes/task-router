"""TR-174 — per-task value ledger: band-keyed expected cost, blending toward
list price, the completion term, the exploration floor, and win-reason
auditability.

Hermetic: scratch stats indexes / outcome stores only; the live fleet state is
never read or written. The fleet default stays `price` (pinned again at the
bottom), and exploration is OFF at share 0.0 — every probe test opts in
explicitly.
"""
import json
import os
import sys
import time
import zlib

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import router_spawn as rs  # noqa: E402


def _stats(*triples):
    """(provider, model, n_samples, avg_cost, [success_rate]) -> stats index."""
    index = {}
    for t in triples:
        prov, model, n, cost = t[0], t[1], t[2], t[3]
        row = {'n_samples': n, 'avg_cost_task_24h': cost,
               'complexity': None}
        if len(t) > 4:
            row['success_rate'] = t[4]
            row['n_completed'] = int(n * t[4])
            row['n_success_known'] = n
        index[(prov, model)] = [row]
    return index


LANES = [{'provider': 'p', 'model': 'm', 'normalized_price': 0.10},
         {'provider': 'q', 'model': 'n', 'normalized_price': 0.01}]


def _key(fn, m):
    return fn(m)


# ============================== feature 1: blending toward list price =======

def test_thin_lane_blends_toward_list_price():
    """n below the blend ceiling: the ranking value is the empirical-Bayes
    blend (n*measured + W*list)/(n+W), and the basis NAMES the blend."""
    ctx = {'index': _stats(('p', 'm', 4, 0.0005))}
    value, b = rs.lane_expected_value(LANES[0], ctx, 3, rs.BLEND_CEIL_SAMPLES,
                                      rs.BLEND_WEIGHT)
    expected = (4 * 0.0005 + 5 * 0.10) / (4 + 5)
    assert value == pytest.approx(expected)
    assert b['basis'] == 'blended-raw'
    assert b['blend_weight'] == 5.0 and b['blend_ceil'] == 9


def test_blend_disappears_at_the_ceiling():
    """n == ceiling: the measurement stands alone (raw mean, no shrinkage)."""
    ctx = {'index': _stats(('p', 'm', 9, 0.0005))}
    value, b = rs.lane_expected_value(LANES[0], ctx, 3, 9, 5.0)
    assert value == pytest.approx(0.0005)
    assert b['basis'] == 'measured-raw'
    ctx2 = {'index': _stats(('p', 'm', 30, 0.0005))}
    value2, b2 = rs.lane_expected_value(LANES[0], ctx2, 3, 9, 5.0)
    assert value2 == pytest.approx(0.0005) and b2['basis'] == 'measured-raw'


def test_a_thin_fluke_mean_no_longer_beats_a_solid_measurement():
    """THE ranking consequence: a 3-sample 0.0001 fluke blends to ~list price
    and loses to a 12-sample honest 0.02 — the head is decided by evidence,
    not by the smallest denominator in the room."""
    thin = {'provider': 'p', 'model': 'm', 'normalized_price': 0.10}
    solid = {'provider': 'q', 'model': 'n', 'normalized_price': 0.01}
    ctx = {'index': _stats(('p', 'm', 3, 0.0001), ('q', 'n', 12, 0.02))}
    key = rs._sort_predicted_cost_per_task(None, [thin, solid], ctx)
    assert key(solid) < key(thin)
    # and without blending (W=0) the fluke would have won — that is what changed
    key_raw = rs._sort_predicted_cost_per_task('3:0.5:9:0', [thin, solid], ctx)
    assert key_raw(thin) < key_raw(solid)


def test_blend_config_comes_from_the_spec_or_env_defaults():
    """3rd spec field = ceiling, 4th = weight; defaults are the env-derived
    module constants (ROUTER_SORT_BLEND_CEIL / ROUTER_SORT_BLEND_WEIGHT)."""
    assert rs.BLEND_CEIL_SAMPLES == 9 and rs.BLEND_WEIGHT == 5.0
    ctx = {'index': _stats(('p', 'm', 4, 0.0005))}
    _v, b = rs.lane_expected_value(LANES[0], ctx, 3, 2, 1.0)
    # n=4 >= ceil 2 -> raw despite the small n
    assert b['basis'] == 'measured-raw'
    ctx2 = {'index': _stats(('p', 'm', 4, 0.0005))}
    v2, b2 = rs.lane_expected_value(LANES[0], ctx2, 3, 9, 1.0)
    assert v2 == pytest.approx((4 * 0.0005 + 1 * 0.10) / 5)
    assert b2['blend_weight'] == 1.0
    # garbage spec fields fall back to the defaults instead of raising
    basis = {}
    rs._sort_predicted_cost_per_task('x:y:z:w:q', LANES, basis)
    assert basis['_sort_basis']['blend_ceiling_samples'] == 9
    assert basis['_sort_basis']['blend_weight'] == 5.0


def test_blend_weight_zero_is_the_raw_mean():
    """W=0 => (n*measured)/n = the mean: the pre-TR-174 value, reachable."""
    ctx = {'index': _stats(('p', 'm', 1, 0.0005))}
    value, b = rs.lane_expected_value(LANES[0], ctx, 0, 9, 0.0)
    assert value == pytest.approx(0.0005) and b['basis'] == 'blended-raw'


def test_below_the_floor_still_falls_back_to_price_with_the_reason():
    """The TR-183 contract is untouched: below the floor the lane gets (None,
    reason) and ranks on its list price — blending never rescues it."""
    ctx = {'index': _stats(('p', 'm', 1, 0.0005))}
    value, b = rs.lane_expected_value(LANES[0], ctx, 3, 9, 5.0)
    assert value is None and b['basis'] == 'below-floor'
    ctx2 = {'index': {}}
    value2, b2 = rs.lane_expected_value(LANES[0], ctx2, 3, 9, 5.0)
    assert value2 is None and b2['basis'] == 'no-sample'


# ============================== feature 2: completion as a cost term ========

def test_a_failing_cheap_lane_loses_to_an_honest_one():
    """'Cheap FAILURES never score as value': p/m is cheaper per attempt
    (0.05 < 0.1) but completes only 10% — cost per COMPLETED task puts it
    behind q/n (0.5 vs 0.1)."""
    p = {'provider': 'p', 'model': 'm', 'normalized_price': 0.10}
    q = {'provider': 'q', 'model': 'n', 'normalized_price': 0.01}
    # n=10 >= ceiling so the raw means are compared (blending out of the picture)
    ctx = {'index': _stats(('p', 'm', 10, 0.05, 0.1),
                           ('q', 'n', 10, 0.1, 1.0))}
    key = rs._sort_predicted_cost_per_task(None, [p, q], ctx)
    # per attempt p wins (0.05 < 0.1); per COMPLETED task q wins (0.1 < 0.5)
    assert key(q) < key(p)
    value, b = rs.lane_expected_value(p, ctx, 3, 9, 5.0)
    assert value == pytest.approx(0.05 / 0.1)
    assert b['basis'] == 'measured-completed'
    assert b['completion_term'] == 'success_rate'


def test_absent_success_rate_skips_the_division_and_says_so():
    """No completion data in the row: the mean is NOT divided by a guessed
    1.0 — the basis names 'no-completion-term' and the value stays the mean."""
    ctx = {'index': _stats(('p', 'm', 10, 0.37))}
    value, b = rs.lane_expected_value(LANES[0], ctx, 3, 9, 5.0)
    assert value == pytest.approx(0.37)
    assert b['completion_term'] == 'no-completion-term'
    assert b['basis'] == 'measured-raw'


def test_zero_completion_rate_is_floored_never_infinite():
    """A 0%-complete lane divides by the floor (0.1), not by zero — finite,
    terrible, and named."""
    ctx = {'index': _stats(('p', 'm', 10, 0.5, 0.0))}
    value, b = rs.lane_expected_value(LANES[0], ctx, 3, 9, 5.0)
    assert value == pytest.approx(0.5 / rs.COMPLETION_RATE_FLOOR)
    assert b['completion_term'] == 'success_rate'


def test_completion_blends_use_the_rate_too():
    """A thin lane with a bad rate blends (shrunk mean / rate) — both terms
    compose instead of one silencing the other."""
    ctx = {'index': _stats(('p', 'm', 4, 0.01, 0.5))}
    value, b = rs.lane_expected_value(LANES[0], ctx, 3, 9, 5.0)
    completed = 0.01 / 0.5
    assert value == pytest.approx((4 * completed + 5 * 0.10) / 9)
    assert b['basis'] == 'blended-completed'


def test_completion_floor_default_is_pinned():
    """ROUTER_SORT_COMPLETION_FLOOR default 0.1 — pinned so a silent change
    of the divisor floor is visible in review."""
    assert rs.COMPLETION_RATE_FLOOR == 0.1


# ============================== feature 3: exploration floor ================

@pytest.fixture()
def _outcomes_env(tmp_path, monkeypatch):
    """Point ROUTING_OUTCOMES_FILE at a scratch store per test."""
    path = os.path.join(str(tmp_path), 'outcomes.jsonl')
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', path)
    return path


def test_exploration_is_off_by_default(_outcomes_env):
    """share 0.0: no probe, no explore_lane, reason 'disabled' — the fleet
    chain order is untouched unless someone opts in."""
    ctx = {'index': _stats(('p', 'm', 9, 0.05), ('q', 'n', 9, 0.02)),
           'task_key': 'TP'}
    assert rs.EXPLORE_SHARE == 0.0
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    b = ctx['_sort_basis']
    assert b['explore_reason'] == 'disabled' and 'explore_lane' not in b
    assert key(LANES[1]) < key(LANES[0])          # pure value order


def test_probe_wins_the_head_and_is_auditable(tmp_path, _outcomes_env):
    """share 1.0: the probe is the lane with the FEWEST samples among those
    older than the age window — and it actually takes the head."""
    now = time.time()
    with open(_outcomes_env, 'w') as f:
        for prov, model, age_h in [('p', 'm', 100.0), ('q', 'n', 0.5),
                                   ('z', 'z', 200.0)]:
            f.write(json.dumps({'provider': prov, 'model': model,
                                'ts': now - age_h * 3600.0}) + '\n')
    lanes = LANES + [{'provider': 'z', 'model': 'z', 'normalized_price': 0.5}]
    ctx = {'index': _stats(('p', 'm', 2, 0.05), ('q', 'n', 50, 0.02),
                           ('z', 'z', 1, 0.9)),
           'task_key': 'TP'}
    key = rs._sort_predicted_cost_per_task('3:0:9:5:1.0', lanes, ctx)
    b = ctx['_sort_basis']
    # the stalest-and-thinnest lane is z/z (1 sample, 200h old): p/m is stale
    # too but has 2 samples; q/n is fresh
    assert b['explore_lane'] == 'z/z'
    assert key(lanes[2]) < key(lanes[1])          # the probe takes the head
    rec = b['win_reason']
    assert rec['mode'] == 'explore'
    assert rec['samples'] == 1
    assert rec['band'] == 'unknown'
    # the aging audit field: z/z's last outcome is ~200h old
    assert rec['age_h'] is not None and rec['age_h'] >= 24.0
    # the per-lane basis for the probe still carries its real ladder reason:
    # z/z HAS a row but its single sample sits below the floor
    v, pb = rs.lane_expected_value(lanes[2], ctx, 3, 9, 5.0)
    assert pb['basis'] == 'below-floor' and v is None


def test_fresh_lanes_are_never_probed(tmp_path, _outcomes_env):
    """Every lane fresh => no lane older than the window => no probe; the head
    is the ordinary ranking winner in exploit mode."""
    now = time.time()
    with open(_outcomes_env, 'w') as f:
        for prov, model in [('p', 'm'), ('q', 'n')]:
            f.write(json.dumps({'provider': prov, 'model': model,
                                'ts': now - 0.1 * 3600.0}) + '\n')
    ctx = {'index': _stats(('p', 'm', 9, 0.05), ('q', 'n', 9, 0.02)),
           'task_key': 'TP'}
    key = rs._sort_predicted_cost_per_task('3:0:9:5:1.0', LANES, ctx)
    b = ctx['_sort_basis']
    assert b['explore_reason'] == 'no-lane-older-than-window'
    assert 'explore_lane' not in b
    assert b['win_reason']['mode'] == 'exploit'
    assert key(LANES[1]) < key(LANES[0])          # cheap measured lane still wins


def test_hash_gate_is_deterministic_and_share_sized(_outcomes_env):
    """No RNG: the gate is crc32(task_key)%1000 < share*1000. A key inside the
    share explores; a key outside does not — both reproducible forever."""
    now = time.time()
    with open(_outcomes_env, 'w') as f:
        for prov, model in [('p', 'm'), ('q', 'n')]:
            f.write(json.dumps({'provider': prov, 'model': model,
                                'ts': now - 100 * 3600.0}) + '\n')
    stats = _stats(('p', 'm', 9, 0.05), ('q', 'n', 9, 0.02))
    in_key = out_key = None
    for i in range(100000):
        gate = zlib.crc32(str(i).encode('utf-8')) % 1000
        if in_key is None and gate < 500:
            in_key = str(i)
        elif out_key is None and gate >= 500:
            out_key = str(i)
        if in_key and out_key:
            break
    lanes = LANES
    ctx_in = {'index': stats, 'task_key': in_key}
    key_in = rs._sort_predicted_cost_per_task('3:0:9:5:0.5', lanes, ctx_in)
    # both lanes are stale with equal samples (9), so the price tie-break
    # decides: q/n (0.01) beats p/m (0.10) — deterministically
    assert ctx_in['_sort_basis']['explore_lane'] == 'q/n'
    assert key_in(LANES[1]) < key_in(LANES[0])    # the probe takes the head
    ctx_out = {'index': dict(stats), 'task_key': out_key}
    rs._sort_predicted_cost_per_task('3:0:9:5:0.5', lanes, ctx_out)
    assert ctx_out['_sort_basis']['explore_reason'] == 'hash-out-of-share'
    assert 'explore_lane' not in ctx_out['_sort_basis']
    # same key => same decision, again
    ctx_again = {'index': dict(stats), 'task_key': in_key}
    key_again = rs._sort_predicted_cost_per_task('3:0:9:5:0.5', lanes, ctx_again)
    assert ctx_again['_sort_basis']['explore_lane'] == 'q/n'
    assert key_again(LANES[1]) < key_again(LANES[0])


def test_exploration_without_a_task_key_stands_down(_outcomes_env):
    """Share set but the caller gave no task identity: no probe (stand down,
    say why) — never a crash and never a random pick."""
    ctx = {'index': _stats(('p', 'm', 9, 0.05), ('q', 'n', 9, 0.02))}
    rs._sort_predicted_cost_per_task('3:0:9:5:1.0', LANES, ctx)
    assert ctx['_sort_basis']['explore_reason'] == 'no-task-key'


def test_unreadable_outcome_store_is_fail_open(_outcomes_env):
    """Missing store => unknown ages => no probe; the resolve proceeds on the
    value ranking alone."""
    os.remove(_outcomes_env) if os.path.exists(_outcomes_env) else None
    ctx = {'index': _stats(('p', 'm', 9, 0.05), ('q', 'n', 9, 0.02)),
           'task_key': 'TP'}
    key = rs._sort_predicted_cost_per_task('3:0:9:5:1.0', LANES, ctx)
    assert ctx['_sort_basis']['explore_reason'] == 'no-lane-older-than-window'
    assert key(LANES[1]) < key(LANES[0])


def test_explore_age_threshold_default_is_pinned():
    """ROUTER_SORT_EXPLORE_MIN_AGE_H default 24h — pinned for review."""
    assert rs.EXPLORE_MIN_AGE_H == 24.0


def test_env_configured_explore_share_and_age_window(tmp_path, _outcomes_env,
                                                     monkeypatch):
    """The env path: ROUTER_SORT_EXPLORE_SHARE / _MIN_AGE_H land in the module
    constants the sorter consults (spec 5th field absent -> env wins)."""
    monkeypatch.setattr(rs, 'EXPLORE_SHARE', 1.0)
    monkeypatch.setattr(rs, 'EXPLORE_MIN_AGE_H', 48.0)
    now = time.time()
    with open(_outcomes_env, 'w') as f:
        # both lanes inside a 48h window -> nothing qualifies at the raised bar
        for prov, model in [('p', 'm'), ('q', 'n')]:
            f.write(json.dumps({'provider': prov, 'model': model,
                                'ts': now - 30 * 3600.0}) + '\n')
    ctx = {'index': _stats(('p', 'm', 9, 0.05), ('q', 'n', 9, 0.02)),
           'task_key': 'TP'}
    rs._sort_predicted_cost_per_task(None, LANES, ctx)
    assert ctx['_sort_basis']['explore_reason'] == 'no-lane-older-than-window'
    assert ctx['_sort_basis']['explore_share'] == 1.0
    # at the default 24h bar the same lanes DO qualify -> the probe fires
    monkeypatch.setattr(rs, 'EXPLORE_MIN_AGE_H', 24.0)
    ctx2 = {'index': _stats(('p', 'm', 9, 0.05), ('q', 'n', 9, 0.02)),
            'task_key': 'TP'}
    key2 = rs._sort_predicted_cost_per_task(None, LANES, ctx2)
    assert ctx2['_sort_basis']['explore_lane'] == 'q/n'   # price tie-break
    assert key2(LANES[1]) < key2(LANES[0])


# ============================== feature 4: win-reason auditability ==========

def test_exploit_head_records_its_full_basis():
    """A measured winner carries mode/band/expected_cost/basis/samples/blend
    weight/completion term — everything an auditor needs, on the record."""
    ctx = {'index': _stats(('p', 'm', 9, 0.0005), ('q', 'n', 9, 0.02))}
    rs._sort_predicted_cost_per_task(None, LANES, ctx)
    rec = ctx['_sort_basis']['win_reason']
    assert rec['mode'] == 'exploit'
    assert rec['band'] == 'unknown' and rec['band_source'] == 'no-band-data'
    assert rec['expected_cost'] == pytest.approx(0.0005)
    assert rec['basis'] == 'measured-raw'
    assert rec['samples'] == 9
    assert rec['blend_weight'] == 5.0
    assert rec['completion_term'] == 'no-completion-term'
    assert rec['age_h'] is None            # exploration never fired: no store scan
    assert ctx['_win_reason'][('p', 'm')] is rec


def test_price_fallback_head_is_named_when_nothing_is_measured():
    """Coverage gate fails => the head is the price winner in price-fallback
    mode, with expected_cost None and the per-lane reason kept."""
    index = _stats(('p', 'm', 1, 0.0001))
    lanes = LANES + [{'provider': 'z%d' % i, 'model': 'lane%d' % i}
                     for i in range(9)]
    ctx = {'index': index}
    rs._sort_predicted_cost_per_task(None, lanes, ctx)
    b = ctx['_sort_basis']
    assert b['effective'] == 'price' and b['reason'] == 'below-coverage-floor'
    rec = b['win_reason']
    assert rec['mode'] == 'price-fallback'
    assert rec['expected_cost'] is None
    assert rec['basis'] == 'no-sample'           # q/n has no stats row at all


def test_band_is_recorded_when_the_row_carries_categories():
    """Band = max required level of the matched row, source named; absent
    stays 'unknown' with its source."""
    row = {'n_samples': 9, 'avg_cost_task_24h': 0.01, 'complexity': None,
           'required_categories': {'coding': 3, 'vision': 1}}
    ctx = {'index': {('p', 'm'): [row]}}
    lanes = [LANES[0]]
    rs._sort_predicted_cost_per_task('3:0', lanes, ctx)
    rec = ctx['_sort_basis']['win_reason']
    assert rec['band'] == 3
    assert rec['band_source'] == 'required_categories-max-level'


def test_outcome_note_carries_the_selection_for_the_head_only():
    """The head hop's outcomes note gains `selection`; a non-head hop does not
    claim a win it did not have."""
    ctx = {'index': _stats(('p', 'm', 9, 0.0005), ('q', 'n', 9, 0.02)),
           'keys': None, 'window_h': 24}
    rs._sort_predicted_cost_per_task(None, LANES, ctx)
    head_note = rs.outcome_note(LANES[0], ctx)
    assert head_note['selection']['mode'] == 'exploit'
    assert head_note['selection']['expected_cost'] == pytest.approx(0.0005)
    loser_note = rs.outcome_note(LANES[1], ctx)
    assert 'selection' not in loser_note


def test_a_tr174_internal_error_degrades_to_price_and_names_itself():
    """Fail-open preserved: any error inside the new code returns the legacy
    price key and stamps the basis with a tr174-error reason — a resolve is
    never blocked by the ledger."""
    ctx = {'index': _stats(('p', 'm', 9, 0.01))}
    original = rs.lane_expected_value

    def boom(*a, **k):
        raise RuntimeError('ledger exploded')
    rs.lane_expected_value = boom
    try:
        key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    finally:
        rs.lane_expected_value = original
    assert ctx['_sort_basis']['effective'] == 'price'
    assert ctx['_sort_basis']['reason'].startswith('tr174-error:')
    # the returned key IS the legacy price key
    assert key(LANES[0]) == rs._legacy_sort_key(LANES[0])
    assert key(LANES[1]) == rs._legacy_sort_key(LANES[1])


# ============================== end-to-end through resolve() ================

CAT = 'agent_tick'
LANE_PRICES = [('prov-a', 'a-expensive', 9.0),
               ('prov-b', 'b-cheap', 1.0),
               ('prov-c', 'c-mid', 5.0)]


def _tables():
    return {
        'providers': [{'id': p} for p, _m, _pr in LANE_PRICES],
        'models': [{'provider': p, 'model': m, 'normalized_price': pr,
                    'data_class': 'open', 'context_limit': 200000}
                   for p, m, pr in LANE_PRICES],
        'model_tier': [{'model': m, 'category': CAT, 'tier': 5}
                       for _p, m, _pr in LANE_PRICES],
        'category_levels': [{'category': CAT, 'level': 5, 'label': 'q95',
                             'min_perf': 0.9}],
        'level_defs': [{'level': lvl, 'label': str(lvl)} for lvl in range(-5, 6)],
        'fallback_lanes': [],
        'projects': [{'id': 'proj', 'profile': 'TP'}],
        'task_profiles': [{'id': 'TP', 'title': 'test profile'}],
        'task_profile_requirements': [{'task_id': 'TP', 'category': CAT,
                                       'level': 5}],
    }


def _wire(tmp_path, monkeypatch, averages=(), outcomes=()):
    tables = _tables()
    reg = tmp_path / 'registry.json'
    reg.write_text(json.dumps({'version': 3, 'tables': tables}))
    monkeypatch.setattr(rs, 'REGISTRY', str(reg))
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    json.dump({'providers': {r['id']: {'status': 'open'}
                             for r in tables['providers']}},
              open(state / 'quota-state.json', 'w'))
    json.dump({'providers': {}}, open(state / 'health-state.json', 'w'))
    json.dump({'pairs': {}}, open(state / 'circuit-state.json', 'w'))
    monkeypatch.setattr(rs, 'MR', str(state))
    avg = tmp_path / 'averages.jsonl'
    with open(avg, 'w') as f:
        for r in averages:
            f.write(json.dumps(r) + '\n')
    monkeypatch.setenv('ROUTING_AVERAGES_FILE', str(avg))
    out = tmp_path / 'outcomes.jsonl'
    with open(out, 'w') as f:
        for r in outcomes:
            f.write(json.dumps(r) + '\n')
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(out))


def _avg_row(provider, model, cost, n=10, rate=None):
    row = {'source_system': 'hermes', 'provider': provider, 'model': model,
           'complexity': None, 'n_samples': n,
           'n_completed': int(n * rate) if rate is not None else 0,
           'n_success_known': n if rate is not None else 0,
           'success_rate': rate, 'avg_cost_task_24h': cost}
    return row


def test_resolve_response_is_auditable_end_to_end(tmp_path, monkeypatch):
    """The full chain: blend + completion + exploration + win reason all land
    in ONE resolve response — sort_stats.sufficiency for the decision,
    chain[0].outcomes.selection for the head."""
    now = time.time()
    _wire(tmp_path, monkeypatch,
          averages=[_avg_row('prov-a', 'a-expensive', 0.01, n=4),
                    _avg_row('prov-b', 'b-cheap', 0.5, n=10, rate=1.0),
                    _avg_row('prov-c', 'c-mid', 2.0, n=10)],
          outcomes=[{'provider': 'prov-c', 'model': 'c-mid',
                     'ts': now - 100 * 3600.0}])
    # spec: floor 3, coverage 0, ceil 9, weight 5, explore share 1.0
    r = rs.resolve(project='proj',
                   sort='predicted_cost_per_task:3:0:9:5:1.0')
    assert 'error' not in r
    sup = r['sort_stats']['sufficiency']
    assert sup['blend_ceiling_samples'] == 9 and sup['blend_weight'] == 5.0
    assert sup['explore_share'] == 1.0
    assert sup['explore_lane'] == 'prov-c/c-mid'
    # the probe takes the chain head, ahead of the blended/measured order
    assert r['chain'][0]['provider'] == 'prov-c'
    sel = r['chain'][0]['outcomes']['selection']
    assert sel['mode'] == 'explore'
    assert sel['basis'] == 'measured-raw'         # c-mid: n=10 clears floor+ceiling
    # non-head hops do not claim the win
    others = [e for e in r['chain'][1:] if 'outcomes' in e]
    assert all('selection' not in e['outcomes'] for e in others)


def test_resolve_default_stays_price_with_no_selection_claims(tmp_path, monkeypatch):
    """The doctrine guard, once more at the response level: the DEFAULT sort
    never emits a sufficiency claim or a selection record."""
    _wire(tmp_path, monkeypatch,
          averages=[_avg_row('prov-b', 'b-cheap', 0.001, n=10, rate=1.0)])
    r = rs.resolve(project='proj')
    assert r['sort'] == 'price'
    assert r['sort_stats']['sufficiency'] is None
    for e in r['chain']:
        if 'outcomes' in e:
            assert 'selection' not in e['outcomes']


def test_resolve_exploit_head_selection_record(tmp_path, monkeypatch):
    """Without exploration the head's selection reads exploit/measured, and
    the completion term shows up when the row carries a success rate."""
    _wire(tmp_path, monkeypatch,
          averages=[_avg_row('prov-a', 'a-expensive', 0.01, n=10, rate=0.2),
                    _avg_row('prov-b', 'b-cheap', 0.5, n=10, rate=1.0)])
    r = rs.resolve(project='proj', sort='predicted_cost_per_task:3:0')
    # per completed task: a = 0.05, b = 0.5 -> a wins DESPITE the 90% failure rate
    assert r['chain'][0]['provider'] == 'prov-a'
    sel = r['chain'][0]['outcomes']['selection']
    assert sel['mode'] == 'exploit'
    assert sel['expected_cost'] == pytest.approx(0.05)
    assert sel['basis'] == 'measured-completed'
    assert sel['completion_term'] == 'success_rate'
    assert sel['samples'] == 10
