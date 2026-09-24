"""Unit tests for router_provider_import: normalize, diff, apply_lanes, probe row.
No network in tests — catalogs are fixture dicts."""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_provider_import as rpi


PRESET = {
    'id': 'provtest',
    'catalog_format': 'openai_models_list',
    'field_map': {'model': 'id', 'price_in': 'pricing.input', 'price_out': 'pricing.output',
                  'context': 'context_length', 'vision': 'capabilities.vision',
                  'thinking': 'capabilities.reasoning'},
    'blend': [0.96, 0.04],
    'probe': {'base_url': 'https://x.test/v1', 'key_env': 'PROVTEST_KEY',
              'default_model': 'prov/alpha'},
}


def _cat():
    return {'data': [
        {'id': 'prov/alpha', 'pricing': {'input': 1.0, 'output': 2.0},
         'context_length': 100000,
         'capabilities': {'vision': False, 'reasoning': True}},
        {'id': 'prov/beta', 'pricing': {'input': 0.0, 'output': 0.0},
         'context_length': 200000,
         'capabilities': {'vision': True, 'reasoning': False}},
    ]}


def test_normalize_blend_and_caps():
    lanes = rpi.normalize(_cat(), PRESET)
    assert set(lanes) == {'prov/alpha', 'prov/beta'}
    a = lanes['prov/alpha']
    assert a['normalized_price'] == pytest.approx(0.96 * 1.0 + 0.04 * 2.0)
    assert a['public_in_per_m'] == 1.0 and a['public_out_per_m'] == 2.0
    assert a['context_limit'] == 100000
    assert a['vision'] is False and a['thinking'] is True
    b = lanes['prov/beta']
    assert b['normalized_price'] == 0.0  # $0 is a price, not absence of price
    assert b['vision'] is True and b['thinking'] is False


def test_diff_added_changed_removed():
    existing = {('provtest', 'prov/alpha'): {
        'provider': 'provtest', 'model': 'prov/alpha',
        'normalized_price': 9.9, 'public_in_per_m': None, 'public_out_per_m': None,
        'context_limit': None, 'vision': None, 'thinking': None}}
    d = rpi.diff(existing, rpi.normalize(_cat(), PRESET))
    assert d['added'] == ['prov/beta']
    assert d['removed'] == []
    changed = dict(d['changed'])
    assert 'prov/alpha' in changed
    assert 'normalized_price' in changed['prov/alpha']


def test_diff_reports_removal_without_touching():
    existing = {('provtest', m): {'provider': 'provtest', 'model': m} for m in
                ('prov/alpha', 'prov/gamma')}
    d = rpi.diff(existing, rpi.normalize(_cat(), PRESET))
    assert d['removed'] == ['prov/gamma']


def test_apply_lanes_update_preserves_order_and_evidence(tmp_path):
    p = tmp_path / 'models.jsonl'
    prior = [
        {'provider': 'other', 'model': 'keep-me', 'normalized_price': 1.0},
        {'provider': 'provtest', 'model': 'prov/alpha', 'normalized_price': 9.9,
         'perf_reasoning': 0.77, 'valid_from': '2026-01-01', 'plan_tier': None},
    ]
    with open(p, 'w') as f:
        for r in prior:
            f.write(json.dumps(r) + '\n')
    lanes = rpi.normalize(_cat(), PRESET)
    updated, appended = rpi.apply_lanes(str(p), 'provtest', lanes, plan_tier=0,
                                        price_evidence='test')
    assert (updated, appended) == (1, 1)  # alpha updated; beta appended; other untouched
    rows = [json.loads(l) for l in open(p) if l.strip()]
    assert rows[0]['model'] == 'keep-me'          # order preserved
    assert rows[1]['model'] == 'prov/alpha'
    assert rows[1]['normalized_price'] == pytest.approx(1.04)
    assert rows[1]['perf_reasoning'] == 0.77       # evidence columns preserved on update
    # An UPDATE must not restamp plan_tier on a live row. Measured 2026-09-24: a
    # catalog refresh that stamped the preset's 0 onto 376 openrouter rows
    # (all of which carry None) re-bucketed them and moved the P0_FORE golden
    # head onto an openrouter lane. plan_tier is a PLAN fact that belongs to the
    # provider's own wiring, not something a price import overwrites.
    assert rows[1]['plan_tier'] is None
    assert rows[2]['model'] == 'prov/beta'
    assert rows[2]['perf_reasoning'] is None       # TR-044: net-new = NULL until probed
    # A NET-NEW row still takes the preset's declared tier: plan providers
    # (xkiro plan_tier_policy.default=0) keep stamping, a PAYG aggregator
    # declares null so no tier is invented for it.
    assert rows[2]['plan_tier'] == 0


def test_apply_lanes_new_rows_are_perf_null(tmp_path):
    p = tmp_path / 'models.jsonl'
    p.write_text('')
    lanes = rpi.normalize(_cat(), PRESET)
    rpi.apply_lanes(str(p), 'provtest', lanes, plan_tier=0, price_evidence='test')
    rows = [json.loads(l) for l in open(p) if l.strip()]
    for r in rows:
        for col in ('perf_reasoning', 'perf_code_gen', 'perf_debug'):
            assert r.get(col) is None


def test_ensure_probe_row_idempotent(tmp_path):
    p = tmp_path / 'probe_providers.jsonl'
    p.write_text(json.dumps({'id': 'other', 'base_url': 'https://o/v1',
                             'key_env': 'O_KEY', 'default_model': 'm'}) + '\n')
    assert rpi.ensure_probe_row(str(p), PRESET) is True
    rows = [json.loads(l) for l in open(p) if l.strip()]
    assert len(rows) == 2
    assert rows[1]['id'] == 'provtest'
    assert rows[1]['key_env'] == 'PROVTEST_KEY'
    assert 'api_key' not in rows[1] and 'key_value' not in rows[1]  # env NAME only, never a key value
    assert rpi.ensure_probe_row(str(p), PRESET) is False  # second call: no-op
    assert len([json.loads(l) for l in open(p) if l.strip()]) == 2


def test_preset_file_loads_and_matches_live_catalog_shape():
    """The committed xkiro preset must parse and normalize the SAVED live catalog
    snapshot identically to what we shipped by hand (115 lanes)."""
    preset = json.load(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'catalogs', 'xkiro.json')))
    snap = '/tmp/xkiro_models.json'
    if not os.path.exists(snap):
        pytest.skip('live catalog snapshot not on this machine')
    lanes = rpi.normalize(json.load(open(snap)), preset)
    assert len(lanes) == 115
    free = [l for l in lanes.values() if l['normalized_price'] == 0.0]
    assert len(free) == 42
