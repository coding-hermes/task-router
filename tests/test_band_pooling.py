"""TR-289: versioned coarse band pooling for the rolling averages.

The measured blocker (2026-10-03): 242 rated requests produced 137 distinct
exact level-maps, 148 bands, 85% seen exactly once, 0 of 400 live chains
bindable. The exact complexity_sig stays for REPORTING; the rolling averages
pool under a versioned coarse band (b1) so samples accumulate.

These tests pin:
  1. the band function's contract (same band for non-dominant level deltas,
     different bands for dominant task classes, versioned key space),
  2. band-keyed aggregates (source_system + provider + model + band, never
     cross-provider), carrying n/success/cost and the exact map for reporting,
  3. version namespacing (old exact-keyed averages are never reinterpreted),
  4. resolve-time band join with the sample floor intact (thin data falls
     back to price with an explicit basis, never measured zero).

No network, no live DB, no live store writes.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro  # noqa: E402
import router_spawn as rs  # noqa: E402

NOW = 1_800_000_000.0


def _row(model='m1', provider='p1', cost=1.0, age_s=0, success=True, source='test',
         sig=None, req=None, band=None):
    row = {'source_system': source, 'session_id': f'{source}-{model}-{age_s}-{cost}',
           'complexity': None, 'provider': provider, 'model': model,
           'turns': 1, 'tokens_in': 100, 'tokens_out': 10, 'tokens_reasoning': 0,
           'cost_usd': cost, 'wall_time_s': 5.0, 'success': success,
           'ts': NOW - age_s}
    if sig:
        row['complexity_sig'] = sig
    if req is not None:
        row['required_categories'] = req
    if band is not None:
        row['complexity_band'] = band
    return row


# --- 1. the band function's contract ----------------------------------------

def test_band_documented_and_versioned():
    assert ro.BAND_VERSION == 'b1'
    assert ro.band_key({'code_gen': 2}).startswith('b1:')
    # the version rides INSIDE the key, so a future bump cannot collide
    assert ro.band_key({'code_gen': 2}) != ro.band_key({'code_gen': 2}).replace('b1', 'b2', 1)


def test_non_dominant_level_delta_shares_the_band():
    """Criterion 1: two task maps differing ONLY in a non-dominant category's
    level share one band. The dominant category (highest level) sets the tier
    and head of the key; a side category moving 1->2 must not split it."""
    a = {'code_gen': 3, 'debug': 1}
    b = {'code_gen': 3, 'debug': 2}          # debug rises but stays non-dominant
    assert ro.band_key(a) == ro.band_key(b)
    c = {'code_gen': 3, 'debug': 1, 'review': 1}
    assert ro.band_key(a) == ro.band_key(c)   # an extra side category too


def test_dominant_class_change_changes_the_band():
    """Criterion 1, other direction: distinct dominant task classes differ."""
    hard = ro.band_key({'code_gen': 4, 'debug': 2})
    easy = ro.band_key({'mechanical': 1})
    assert hard != easy
    # and two different dominant CATEGORIES at the same level differ too
    assert ro.band_key({'security': 3}) != ro.band_key({'code_gen': 3})


def test_band_key_is_deterministic_across_key_order():
    assert ro.band_key({'a': 2, 'b': 3}) == ro.band_key({'b': 3, 'a': 2})
    assert ro.band_key({}) is None
    assert ro.band_key(None) is None


# --- 2. band-keyed aggregates ------------------------------------------------

def test_banded_averages_key_on_source_provider_model_band():
    req = {'code_gen': 3}
    band = ro.band_key(req)
    rows = [_row(provider='p1', req=req, cost=1.0),
            _row(provider='p1', req=req, cost=3.0),
            _row(provider='p1', req=req, cost=2.0)]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW, banding=True)
    assert len(out) == 1
    e = out[0]
    assert e['complexity_band'] == band
    # the exact map stays for reporting alongside the coarse key
    assert e['complexity_sig'] == ro.complexity_sig(req)
    assert e['n_samples'] == 3
    assert e['n_completed'] == 3 and e['n_success_known'] == 3
    assert e['success_rate'] == 1.0
    assert e['avg_cost_task_24h'] == pytest.approx(2.0)
    # every sortable metric the price-ordering consumes is present
    for f in ('avg_wall_time_24h', 'avg_turns_24h', 'avg_tokens_in_24h',
              'avg_tokens_out_24h', 'avg_tokens_total_24h'):
        assert e[f] is not None


def test_banded_averages_never_combine_providers():
    """Criterion 3: the bucket key is source_system + provider + model + band —
    two providers with the same band are different rows, always."""
    req = {'code_gen': 3}
    rows = [_row(provider='p1', req=req, cost=1.0),
            _row(provider='p2', req=req, cost=9.0)]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW, banding=True)
    assert len(out) == 2
    costs = sorted(e['avg_cost_task_24h'] for e in out)
    assert costs == [1.0, 9.0]
    assert {e['provider'] for e in out} == {'p1', 'p2'}


def test_banded_averages_keep_all_pooled_exact_maps_for_reporting():
    req_a = {'code_gen': 3, 'debug': 1}
    req_b = {'code_gen': 3, 'debug': 2}
    assert ro.band_key(req_a) == ro.band_key(req_b)
    rows = [_row(req=req_a, cost=1.0), _row(req=req_b, cost=3.0)]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW, banding=True)
    assert len(out) == 1
    assert out[0]['complexity_band'] == ro.band_key(req_a)
    assert out[0]['complexity_sig'] is None
    assert set(out[0]['complexity_sigs']) == {
        ro.complexity_sig(req_a), ro.complexity_sig(req_b)}
    assert out[0]['required_categories'] is None
    assert len(out[0]['required_category_maps']) == 2


def test_banded_averages_keep_backend_isolation_and_merge_opt_in():
    req = {'code_gen': 3}
    rows = [_row(source='hermes', req=req, cost=2.0),
            _row(source='opencode', req=req, cost=8.0)]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW, banding=True)
    assert len(out) == 2  # per-backend isolation preserved
    merged = ro.compute_averages(rows, scales_h=[24], now_s=NOW,
                                 banding=True, merge_backends=True)
    assert len(merged) == 1
    assert merged[0]['avg_cost_task_24h'] == pytest.approx(5.0)
    assert merged[0]['complexity_band'] == ro.band_key(req)


def test_different_bands_never_share_a_bucket():
    rows = [_row(req={'code_gen': 3}, cost=1.0),
            _row(req={'mechanical': 1}, cost=9.0)]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW, banding=True)
    assert len(out) == 2
    assert {e['complexity_band'] for e in out} == {
        ro.band_key({'code_gen': 3}), ro.band_key({'mechanical': 1})}


# --- 3. version namespacing (old exact averages untouched) -------------------

def test_default_compute_averages_is_unchanged_exact_keying():
    """The pre-TR-289 behaviour is preserved exactly: without banding=True the
    buckets still key on the exact complexity signature (R3.3 — nothing is
    silently reinterpreted)."""
    sig_a = ro.complexity_sig({'code_gen': 3, 'debug': 1})
    sig_b = ro.complexity_sig({'code_gen': 3, 'debug': 2})   # band-equal, sig-distinct
    rows = [_row(sig=sig_a, req={'code_gen': 3, 'debug': 1}, cost=1.0),
            _row(sig=sig_b, req={'code_gen': 3, 'debug': 2}, cost=9.0)]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW)
    assert len(out) == 2                                  # exact keys: two buckets
    assert 'complexity_band' not in json_keys(out[0])
    banded = ro.compute_averages(rows, scales_h=[24], now_s=NOW, banding=True)
    assert len(banded) == 1                               # band: pooled


def json_keys(d):
    return set(d)


def test_banded_rows_carry_the_version_in_the_key():
    out = ro.compute_averages([_row(req={'code_gen': 3})],
                              scales_h=[24], now_s=NOW, banding=True)
    assert out[0]['complexity_band'].startswith(f'{ro.BAND_VERSION}:')


def test_merge_average_rows_handles_legacy_rows_without_band():
    """Schema compatibility: pre-TR-289 average rows (no complexity_band) merge
    on complexity_sig exactly as before."""
    sig = ro.complexity_sig({'code_gen': 2})
    rows = [{'provider': 'p', 'model': 'm', 'complexity_sig': sig, 'n_samples': 4,
             'avg_cost_task_24h': 2.0, 'n_completed': 2, 'n_success_known': 4}]
    merged = ro.merge_average_rows(rows)
    assert merged[0]['complexity_sig'] == sig
    assert merged[0]['avg_cost_task_24h'] == pytest.approx(2.0)


def test_merge_average_rows_pools_by_band_when_band_present():
    """Rows carrying a band pool by the BAND (the exact sigs inside one band
    differ and must not fragment the merge), while band-less rows keep the
    exact-key behaviour."""
    req_a = {'code_gen': 3, 'debug': 1}
    req_b = {'code_gen': 3, 'debug': 2}
    band = ro.band_key(req_a)
    assert ro.band_key(req_b) == band
    rows = [
        {'provider': 'p', 'model': 'm', 'complexity_sig': ro.complexity_sig(req_a),
         'complexity_band': band, 'n_samples': 2, 'avg_cost_task_24h': 1.0,
         'n_completed': 2, 'n_success_known': 2},
        {'provider': 'p', 'model': 'm', 'complexity_sig': ro.complexity_sig(req_b),
         'complexity_band': band, 'n_samples': 2, 'avg_cost_task_24h': 3.0,
         'n_completed': 2, 'n_success_known': 2},
    ]
    merged = ro.merge_average_rows(rows)
    assert len(merged) == 1, 'one band -> one pooled row'
    assert merged[0]['n_samples'] == 4
    assert merged[0]['avg_cost_task_24h'] == pytest.approx(2.0)


def test_merge_average_rows_preserves_each_band_and_all_exact_signatures():
    req_a = {'code_gen': 3, 'debug': 1}
    req_b = {'code_gen': 3, 'debug': 2}
    req_c = {'mechanical': 1}
    band_a, band_c = ro.band_key(req_a), ro.band_key(req_c)
    rows = [
        {'provider': 'p', 'model': 'm', 'complexity_sig': ro.complexity_sig(req_a),
         'complexity_band': band_a, 'required_categories': req_a,
         'n_samples': 2, 'avg_cost_task_24h': 1.0},
        {'provider': 'p', 'model': 'm', 'complexity_sig': ro.complexity_sig(req_b),
         'complexity_band': band_a, 'required_categories': req_b,
         'n_samples': 2, 'avg_cost_task_24h': 3.0},
        {'provider': 'p', 'model': 'm', 'complexity_sig': ro.complexity_sig(req_c),
         'complexity_band': band_c, 'required_categories': req_c,
         'n_samples': 1, 'avg_cost_task_24h': 8.0},
    ]
    merged = ro.merge_average_rows(rows)
    by_band = {row['complexity_band']: row for row in merged}
    assert set(by_band) == {band_a, band_c}
    pooled = by_band[band_a]
    assert pooled['complexity_sig'] is None
    assert set(pooled['complexity_sigs']) == {
        ro.complexity_sig(req_a), ro.complexity_sig(req_b)}
    assert pooled['required_categories'] is None
    assert len(pooled['required_category_maps']) == 2


def test_row_band_key_reads_a_store_row():
    req = {'code_gen': 3}
    row = _row(req=req, sig=ro.complexity_sig(req))
    assert ro.row_band_key(row) == ro.band_key(req)
    # an explicit band on the row wins (what post-deploy writer rows carry)
    assert ro.row_band_key(_row(req=req, band='b1:hard:code_gen')) == 'b1:hard:code_gen'
    assert ro.row_band_key({}) is None


# --- 4. resolve-time band join, floor intact ---------------------------------

def _stats_index(*triples, band=None):
    """(provider, model, n_samples, avg_cost) -> lane_stats index shape."""
    index = {}
    for prov, model, n, cost in triples:
        row = {'n_samples': n, 'avg_cost_task_24h': cost, 'complexity': None}
        if band:
            row['complexity_band'] = band
        index[(prov, model)] = [row]
    return index


LANES = [{'provider': 'p', 'model': 'm', 'normalized_price': 0.10},
         {'provider': 'q', 'model': 'n', 'normalized_price': 0.01}]


def test_lane_stats_matches_by_band():
    req = {'code_gen': 3}
    band = ro.band_key(req)
    ctx = {'index': _stats_index(('p', 'm', 5, 0.02), ('q', 'n', 5, 0.9), band=band),
           'band_keys': {band}}
    row, match = rs.lane_stats(ctx['index'], 'p', 'm', (), band_keys=ctx['band_keys'])
    assert match == 'band'
    assert row['n_samples'] == 5


def test_lane_stats_exact_match_still_outranks_band():
    """The exact signature join stays first; the band join is the WIDER net,
    not a replacement."""
    req = {'code_gen': 3}
    sig = ro.complexity_sig(req)
    band = ro.band_key(req)
    ctx_index = {('p', 'm'): [
        {'n_samples': 2, 'avg_cost_task_24h': 0.5, 'complexity_sig': sig},
        {'n_samples': 8, 'avg_cost_task_24h': 4.0, 'complexity_band': band},
    ]}
    row, match = rs.lane_stats(ctx_index, 'p', 'm', (sig,), band_keys={band})
    assert match == 'complexity'
    assert row['avg_cost_task_24h'] == pytest.approx(0.5)


def test_thin_band_falls_back_to_price_with_explicit_basis():
    """Criterion 4: thin data (below the sample floor) falls back to declared
    price with an explicit basis — never a measured zero."""
    req = {'code_gen': 3}
    band = ro.band_key(req)
    ctx = {'index': _stats_index(('p', 'm', 1, 0.0001), ('q', 'n', 1, 0.9), band=band),
           'band_keys': {band}}
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    for m in LANES:
        key(m)
    basis = ctx['_sort_basis']
    assert basis['ranked_on_measurement'] == 0
    assert basis['fell_back_to_price'] == 2
    assert basis['effective'] == 'price'
    # the proof the sample was ignored: measured says p/m cheaper, price says q/n
    assert key(LANES[1]) < key(LANES[0])


def test_banded_samples_earn_a_measurement_when_floor_cleared():
    req = {'code_gen': 3}
    band = ro.band_key(req)
    ctx = {'index': _stats_index(('p', 'm', 9, 0.0005), ('q', 'n', 9, 0.02), band=band),
           'band_keys': {band}}
    key = rs._sort_predicted_cost_per_task(None, LANES, ctx)
    assert key(LANES[0]) < key(LANES[1])   # measured: p/m genuinely cheaper per task
    assert ctx['_sort_basis']['ranked_on_measurement'] == 2
    assert ctx['_sort_basis']['effective'] == 'measured'


def test_outcome_note_names_the_band_join():
    req = {'code_gen': 3}
    band = ro.band_key(req)
    ctx = {'index': _stats_index(('p', 'm', 4, 0.03), band=band),
           'band_keys': {band}, 'meta': {'source': 'merged'}}
    note = rs.outcome_note(LANES[0], ctx)
    assert note['matched'] == 'band'
    assert note['complexity_band'] == band
    assert note['n_samples'] == 4


# --- write-path stamping ------------------------------------------------------

def test_accumulate_row_stamps_the_band(tmp_path):
    """Post-deploy writer rows carry the versioned band, so the next averages
    run can pool them without re-deriving from the exact map."""
    p = str(tmp_path / 'outcomes.jsonl')
    row = _row(req={'code_gen': 3})
    row.pop('complexity_band', None)
    mode, _reason = ro.accumulate_row(p, row)
    assert mode in ('appended', 'accumulated')
    import json
    stored = json.loads(open(p).read().strip())
    assert stored['complexity_band'] == ro.band_key({'code_gen': 3})
    # the exact map is preserved untouched beside the band
    assert stored.get('required_categories') == {'code_gen': 3}
    # the exact map is NOT overwritten by the band
    assert 'complexity_band' in stored and stored['complexity_band'] != stored.get('complexity_sig')
