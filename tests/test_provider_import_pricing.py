"""Pricing contracts for the provider-catalog importer (OpenRouter preset).

Onboarding OpenRouter exposed three ways a catalog refresh can silently corrupt
the registry, all now pinned here:

1. SCALE. OpenRouter quotes USD PER TOKEN (`pricing.prompt` = 0.0000001);
   every lane in our tables is per 1M tokens. Without the preset's
   `price_scale: 1000000` the whole provider imports 1e-6x too cheap and would
   head every price-ordered chain.
2. SENTINELS. `openrouter/fusion` reports pricing -1000000 (a router
   placeholder). A negative price sorts AHEAD of every honest lane, so it must
   read UNPRICED (None) rather than be imported as a real number.
3. BASIS. Measured 2026-09-24: 347/347 existing openrouter lanes have
   normalized_price == public_in_per_m, while public_price is the 0.96/0.04
   blend. A preset that assumes one basis for both rewrites 346 rows by ~4% on
   a refresh that is only about real catalog changes — noise that hides the
   changes an operator is auditing.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_provider_import as imp   # noqa: E402

PRESET = {
    'id': 'openrouter',
    'catalog_format': 'openai_models_list',
    'price_scale': 1000000,
    'normalized_from': 'price_in',
    'blend': [0.96, 0.04],
    'field_map': {'model': 'id', 'price_in': 'pricing.prompt',
                  'price_out': 'pricing.completion', 'context': 'context_length'},
}


def catalog(*entries):
    return {'data': list(entries)}


def entry(mid, pin, pout, ctx=1000000):
    return {'id': mid, 'pricing': {'prompt': str(pin), 'completion': str(pout)},
            'context_length': ctx}


def lane_for(mid, **kw):
    cat = catalog(entry(mid, kw.pop('pin'), kw.pop('pout'), **kw))
    return imp.normalize(cat, PRESET)[mid]


# ---------- 1. per-token -> per-1M scaling ----------

def test_prices_are_scaled_from_per_token_to_per_million():
    lane = lane_for('openai/gpt-6-luna', pin=0.0000001, pout=0.0000005)
    assert lane['public_in_per_m'] == pytest.approx(0.1), '0.1 per 1M tokens, not 1e-7'
    assert lane['public_out_per_m'] == pytest.approx(0.5)


def test_a_preset_without_a_scale_keeps_catalogs_that_quote_per_million():
    preset = dict(PRESET)
    preset.pop('price_scale')
    lane = imp.normalize(catalog(entry('m', 0.09, 0.3)), preset)['m']
    assert lane['public_in_per_m'] == pytest.approx(0.09)


def test_float_noise_does_not_churn_the_registry():
    """0.8 must stay 0.8: 1e-16 drift alone rewrites every lane in the file."""
    lane = lane_for('m', pin=0.0000008, pout=0.0000016)
    assert lane['public_in_per_m'] == 0.8
    assert lane['public_out_per_m'] == 1.6


# ---------- 2. sentinel guard ----------

def test_negative_sentinel_price_is_not_imported():
    lane = lane_for('openrouter/fusion', pin=-1000000, pout=-1000000)
    assert lane['normalized_price'] is None, 'a negative price would win every chain'


def test_absurd_price_is_not_imported():
    assert imp._scaled_price(1e9, 1.0) is None
    assert imp._scaled_price(imp.MAX_LANE_PRICE_PER_M + 1, 1.0) is None


def test_nan_and_inf_are_not_imported():
    assert imp._scaled_price(float('nan'), 1.0) is None
    assert imp._scaled_price(float('inf'), 1.0) is None


def test_a_genuine_zero_price_survives():
    """Free lanes are real data ($0 list), not a missing measurement."""
    assert imp._scaled_price(0, 1000000) == 0.0


def test_missing_price_fields_stay_none():
    lane = imp.normalize(catalog({'id': 'm', 'pricing': {}}), PRESET)['m']
    assert lane['normalized_price'] is None and lane['public_in_per_m'] is None


# ---------- 3. basis + capability keys ----------

def test_normalized_follows_input_while_public_follows_the_blend():
    lane = lane_for('m', pin=0.0000008, pout=0.0000016)
    assert lane['normalized_price'] == pytest.approx(0.8), 'established basis: the input price'
    assert lane['public_price'] == pytest.approx(0.832), 'list blend 0.96/0.04'


def test_unmapped_capability_keys_are_absent_not_none():
    """Emitting vision=None for a catalog that has no such field diffs every
    lane in the registry — and a modality STRING is not a boolean flag."""
    lane = lane_for('m', pin=0.0000001, pout=0.0000005)
    assert 'vision' not in lane and 'thinking' not in lane


def test_mapped_capability_keys_are_written_when_the_preset_asks():
    preset = dict(PRESET)
    preset['field_map'] = dict(PRESET['field_map'], vision='architecture.modality')
    lane = imp.normalize(catalog({'id': 'm', 'pricing': {'prompt': '0', 'completion': '0'},
                                  'architecture': {'modality': 'text+image->text'}}), preset)['m']
    assert lane['vision'] is True


def test_the_openrouter_preset_declares_the_scale_and_basis():
    """The preset is data the runtime trusts; its two critical fields are pinned."""
    preset = json.load(open(os.path.join(REPO, 'data', 'catalogs', 'openrouter.json')))
    assert preset['price_scale'] == 1000000
    assert preset['normalized_from'] == 'price_in'
    assert preset['blend'] == [0.96, 0.04]
    assert 'vision' not in preset['field_map'] and 'thinking' not in preset['field_map']
