"""TR-076 — catalog-drift matching must survive real provider id conventions
WITHOUT inventing matches. Every case below is a real pair from the live
registry/cache or a real trap found while fixing the detector.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'scripts'))
import router_lifecycle as rl  # noqa: E402


CACHE = {
    'fireworks-ai': {
        'accounts/fireworks/models/kimi-k3': {},
        'accounts/fireworks/models/glm-5p2': {},
        'accounts/fireworks/models/glm-5p3': {},
        'accounts/fireworks/models/deepseek-v4p1-flash': {},
        'accounts/fireworks/models/gpt-oss-120b': {},
        'accounts/fireworks/models/nemotron-3-ultra-nvfp4': {},
        'accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b': {},
    },
    'minimax': {
        'MiniMax-M2.7': {},
        'MiniMax-M3': {},
    },
    'ollama-cloud': {
        'gpt-oss:20b': {},
        'gpt-oss:120b-exp': {},
    },
    'synthetic': {
        'hf:moonshotai/Kimi-K3': {},
        'hf:zai-org/GLM-4.7-Flash': {},
    },
    'acme': {
        'totally-different-model': {},
    },
}


def _row(prov, model, **kw):
    r = {'provider': prov, 'model': model}
    r.update(kw)
    return r


def _kind(prov, model):
    out = rl.catalog_drift([_row(prov, model)], cache=CACHE)
    return out[0]['kind'] if out else 'ok'


# ── the three conventions the naive exact compare missed ─────────────────────

def test_namespace_prefix_is_matched_not_reported_absent():
    """registry `kimi-k3` vs fireworks `accounts/fireworks/models/kimi-k3`."""
    out = rl.catalog_drift([_row('fireworks-ai', 'kimi-k3')], cache=CACHE)
    assert len(out) == 1 and out[0]['kind'] == 'remap'
    assert out[0]['catalog_id'] == 'accounts/fireworks/models/kimi-k3'


def test_case_difference_is_matched():
    """registry `minimax-m2.7` vs minimax `MiniMax-M2.7`."""
    out = rl.catalog_drift([_row('minimax', 'minimax-m2.7')], cache=CACHE)
    assert len(out) == 1 and out[0]['kind'] == 'remap'
    assert out[0]['catalog_id'] == 'MiniMax-M2.7'


def test_fireworks_p_for_dot_is_matched():
    """glm-5.2 is served as glm-5p2; deepseek-v4.1-flash as deepseek-v4p1-flash."""
    for reg, cid in (('glm-5-2', 'accounts/fireworks/models/glm-5p2'),
                     ('glm-5-3', 'accounts/fireworks/models/glm-5p3'),
                     ('deepseek-v4-1-flash', 'accounts/fireworks/models/deepseek-v4p1-flash')):
        out = rl.catalog_drift([_row('fireworks-ai', reg)], cache=CACHE)
        assert out and out[0]['kind'] == 'remap', reg
        assert out[0]['catalog_id'] == cid, reg


def test_a_lane_already_spelled_like_the_catalog_is_not_reported():
    """No drift entry at all when the id matches exactly."""
    assert rl.catalog_drift(
        [_row('fireworks-ai', 'accounts/fireworks/models/kimi-k3')], cache=CACHE) == []


# ── the traps: the matcher must NOT invent a match ──────────────────────────

def test_p_is_not_mangled_inside_words():
    """'preview'/'plus'/'pro'/'compound' must not be read as dots.

    A naive `s.replace('p','.')` maps every one of these onto unrelated ids.
    """
    # 'plus'/'preview' must survive intact under the p-as-dot form
    assert rl._norm(rl._pdot('hy3-preview')) == 'hy3preview'
    assert rl._norm(rl._pdot('qwen3-7-plus')) == 'qwen37plus'
    assert rl._norm(rl._pdot('glm-5-2')) == 'glm52'      # the real convention
    assert rl._norm(rl._pdot('glm-5p2')) == 'glm52'
    # and the p-form keys never collapse two different lanes onto each other
    assert rl._keys('hy3-preview') & rl._keys('hy3') == set()


def test_suffix_difference_is_not_a_remap():
    """gpt-oss:120b must NEVER be silently remapped to gpt-oss:20b.

    Stripping the tag leaves 'gpt-oss', which two catalog ids share, so the tier
    returns `ambiguous` — the safe answer: a human decides, nothing is rewritten.
    """
    out = rl.catalog_drift([_row('ollama-cloud', 'gpt-oss:120b')], cache=CACHE)
    assert out and out[0]['kind'] == 'ambiguous', out
    assert out[0].get('catalog_id') is None


def test_namespace_colon_does_not_collapse_the_id():
    """`hf:moonshotai/Kimi-K3` splits on ':' only in the LAST segment.

    Blindly splitting on the first ':' collapsed this id to the key 'hf', which
    matched any other hf: id — a false remap.
    """
    assert rl._keys('hf:moonshotai/Kimi-K3') & rl._keys('hf:zai-org/GLM-4.7-Flash') == set()
    assert _kind('synthetic', 'hf:moonshotai/Kimi-K3') == 'ok'  # exact present
    # a genuinely different hf: model is NOT resolved to it
    assert _kind('synthetic', 'hf:moonshotai/Kimi-K2.6') == 'absent'


def test_degenerate_short_keys_are_dropped():
    """A <3-char key matches too much to be evidence; it is dropped.

    The threshold is 3, not 4, so that the real 3-char lane `hy3` still
    resolves (a length-4 floor reported it absent while it is in the catalog).
    A short id's FULL path can still clear the floor — that is intended, since
    `a/xy` is a more specific key than the bare `xy`.
    """
    assert rl._keys('hf') == set()               # single 2-char segment: dropped
    assert 'xy' not in rl._keys('a/xy')          # the bare 2-char name is dropped
    assert 'axy' in rl._keys('a/xy')             # the 3-char path key is kept
    assert 'hy3' in rl._keys('hy3')              # the real 3-char lane survives


def test_genuinely_absent_lane_stays_absent():
    out = rl.catalog_drift([_row('acme', 'model-that-does-not-exist')], cache=CACHE)
    assert len(out) == 1 and out[0]['kind'] == 'absent'


# ── filters that must keep working ──────────────────────────────────────────

def test_retired_archived_and_disabled_rows_are_skipped():
    rows = [_row('acme', 'gone', archive=True),
            _row('acme', 'gone2', disabled=True),
            _row('acme', 'gone3', valid_to='2020-01-01')]
    assert rl.catalog_drift(rows, cache=CACHE) == []


def test_provider_outside_the_public_catalog_is_skipped():
    """proxies/plans have no models.dev section — never drift."""
    assert rl.catalog_drift([_row('9router', 'anything')], cache=CACHE) == []


def test_future_available_from_is_skipped():
    rows = [_row('acme', 'soon', available_from='2099-01-01')]
    assert rl.catalog_drift(rows, cache=CACHE) == []


def test_every_entry_carries_a_kind_and_a_note():
    rows = [_row('fireworks-ai', 'kimi-k3'), _row('acme', 'nope')]
    for d in rl.catalog_drift(rows, cache=CACHE):
        assert d.get('kind') in ('remap', 'tag-shape', 'ambiguous', 'absent')
        assert d.get('note')


# ── TR-076 tier 2/3: reordered ids and the by-design whitelist ───────────────

def test_reordered_listing_is_token_shape_not_absent():
    """fireworks `nemotron-3-5-lightning-30b-a3b` vs catalog
    `nemotron-lightning-3p5-30b-a3b` — same model, reordered + p-for-dot."""
    out = rl.catalog_drift([_row('fireworks-ai', 'nemotron-3-5-lightning-30b-a3b')],
                           cache=CACHE)
    assert len(out) == 1 and out[0]['kind'] == 'token-shape', out
    assert out[0]['catalog_id'] == 'accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b'


def test_quant_suffix_variant_is_not_folded_onto_the_base_model():
    """`nemotron-3-ultra` vs `nemotron-3-ultra-nvfp4` are DIFFERENT artifacts —
    the extra `nvfp4` token must keep them apart."""
    assert rl._token_key('nemotron-3-ultra') != rl._token_key(
        'accounts/fireworks/models/nemotron-3-ultra-nvfp4')
    assert _kind('fireworks-ai', 'nemotron-3-ultra') == 'absent'


def test_version_written_three_ways_yields_one_token_key():
    """'3.5', '3-5' and '3p5' are the same version, so the keys must agree."""
    a = rl._token_key('nemotron-3-5-lightning-30b-a3b')
    b = rl._token_key('nemotron-lightning-3p5-30b-a3b')
    c = rl._token_key('nemotron-lightning-3.5-30b-a3b')
    assert a == b == c


def test_size_token_stays_intact():
    """A size like 30b carries a unit — it must not be split into 3|0|b."""
    assert '30b' in rl._token_key('nemotron-3-5-lightning-30b-a3b')


def test_dynamic_routes_are_by_design_not_possibly_gone():
    """OpenRouter's auto/bodybuilder/fusion/pareto-code pick a backing model at
    request time, so they can never be in the catalog. They are excluded by
    PATTERN (documented), not silenced individually."""
    for mid in ('openrouter/auto', 'openrouter/bodybuilder',
                'openrouter/fusion', 'openrouter/pareto-code'):
        out = rl.catalog_drift([_row('openrouter', mid)], cache={'openrouter': {'a/b': {}}})
        assert len(out) == 1 and out[0]['kind'] == 'by-design', (mid, out)


def test_a_normal_openrouter_lane_is_not_excused_by_the_by_design_pattern():
    """The whitelist is anchored — it must not swallow real lanes."""
    out = rl.catalog_drift([_row('openrouter', 'openrouter/auto-something-else')],
                           cache={'openrouter': {'a/b': {}}})
    assert out and out[0]['kind'] != 'by-design', out
