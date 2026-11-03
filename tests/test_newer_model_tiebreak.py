"""TR-369: a newer same-family model must never lose to its older sibling on a
price tie.

The defect this pins down: _legacy_sort_key ends with the literal tie-break
`(model, provider)`, so two lanes that tie on every meaningful dimension are
ordered by ASCII model name. 'deepseek-v4.1-flash' sorts before
'deepseek-v4.1-flash-1017' because it is a PREFIX of it, so the older model
always heads the chain and the newer release is only ever reached on fallback —
even though it costs the same and is better in every dimension.

These tests are deliberately about ORDER, not about prices: the fix must not
re-rank lanes of different prices (the no-silent-re-rank guarantee), it only
decides the winner among lanes that already tie.
"""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_spawn():
    spec = importlib.util.spec_from_file_location('router_spawn_under_test',
                                                  ROOT / 'scripts' / 'router_spawn.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


spawn = _load_spawn()


def lane(model, provider, price=0.15, context=200000, plan_tier=1, **extra):
    row = {
        'model': model,
        'provider': provider,
        'normalized_price': price,
        'public_in_per_m': price,
        'context_limit': context,
        'plan_tier': plan_tier,
        # a priced plan lane: not PAYG, probed, no quota headroom signal
        '_payg': False,
        '_probe_unprobed': False,
        '_quota_headroom': None,
    }
    row.update(extra)
    return row


def order(lanes):
    return [m['model'] for m in sorted(lanes, key=spawn._legacy_sort_key)]


def test_newer_dated_sibling_beats_prefix_older():
    """deepseek-v4.1-flash-1017 must head its own prefix sibling at equal price."""
    lanes = [lane('deepseek-v4.1-flash', 'openrouter'),
             lane('deepseek-v4.1-flash-1017', 'openrouter')]
    assert order(lanes)[0] == 'deepseek-v4.1-flash-1017', (
        'the older prefix sibling still wins the ASCII tie-break — the newer '
        'release can only ever be a fallback hop')


def test_newer_minor_version_beats_older():
    """minimax-m2.1 must head minimax-m2 at equal price."""
    lanes = [lane('minimax-m2', 'xkiro'), lane('minimax-m2.1', 'xkiro-2')]
    assert order(lanes)[0] == 'minimax-m2.1'


def test_newer_point_release_beats_older_across_providers():
    """gpt-6.1-sol beats gpt-6-sol regardless of which provider carries it."""
    lanes = [lane('gpt-6-sol', 'commandcode', price=2.0),
             lane('gpt-6.1-sol', 'openrouter', price=2.0)]
    assert order(lanes)[0] == 'gpt-6.1-sol'


def test_cheaper_identical_model_provider_wins():
    """Same model on two providers: the cheaper lane heads (this already worked
    only by accident of provider name; pin it so the fix cannot lose it)."""
    lanes = [lane('glm-5.3-flash', 'zzz-reseller', price=0.30),
             lane('glm-5.3-flash', 'aaa-direct', price=0.20)]
    assert order(lanes)[0] == 'glm-5.3-flash'
    assert sorted(lanes, key=spawn._legacy_sort_key)[0]['provider'] == 'aaa-direct'


def test_price_still_dominates_version():
    """A cheaper older model is STILL allowed to win — sometimes old is cheaper
    and that is a legitimate reason to keep it. The fix must not override price."""
    lanes = [lane('minimax-m2', 'xkiro', price=0.10),
             lane('minimax-m2.1', 'xkiro', price=0.40)]
    assert order(lanes)[0] == 'minimax-m2'


def test_unpriced_lane_never_heads_a_priced_one():
    """Unknown price must still sink (the fake-cheap burn trap, TR-176)."""
    lanes = [lane('mystery-model', 'someone', price=None),
             lane('glm-5.3-flash', 'zai-glm', price=0.60)]
    assert order(lanes)[0] == 'glm-5.3-flash'


def test_no_silent_rerank_of_different_prices():
    """The invariant the sort must never break: a lane that is strictly more
    expensive than another can never sort ahead of it, whatever its name."""
    cheap = lane('aaa-old-model', 'p1', price=0.10)
    dear = lane('zzz-new-model', 'p2', price=0.90)
    assert order([dear, cheap])[0] == 'aaa-old-model'
