"""The model answers zero-shot; the router maps and bands (owner 2026-10-03).

The owner's correction: "the whole job of the LLM model we just added to task
router is for it to look at the prompt request and then answer that question in a
zero shot." These tests pin the split - the model answers, the router converts -
and the property that makes cost-per-task possible at all: two tasks with the
same SHAPE of demand must land in the same band, or the rolling averages can never
accumulate a sample. Measured before this change: 242 rated requests produced 137
distinct level-maps and 148 bands, 85% seen once, 0 of 400 live chains bindable.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro  # noqa: E402


def test_two_wordings_of_the_same_demand_share_one_band():
    """The pooling property. If this fails, cost-per-task cannot bind."""
    a, _ = ro.map_dimensions({'mechanical': 1}, 0)
    b, _ = ro.map_dimensions({'rename': 1, 'formatting': 1}, 0)
    assert ro.band_key(a) == ro.band_key(b)


def test_different_shapes_get_different_bands():
    hard, _ = ro.map_dimensions({'concurrency': 3, 'debugging': 3}, 3)
    easy, _ = ro.map_dimensions({'docs': 1}, 0)
    assert ro.band_key(hard) != ro.band_key(easy)


def test_the_band_is_coarse_and_readable():
    """A band preserves task tier and the dominant demand category."""
    levels, _ = ro.map_dimensions({'concurrency': 3, 'debugging': 2, 'go': 2}, 3)
    band = ro.band_key(levels)
    assert band.startswith(f'{ro.BAND_VERSION}:')
    assert '+' not in band
    assert len(band.split(':')) == 3
    assert 'debug' in band or 'reasoning' in band


def test_a_resolved_band_is_stable_across_key_order():
    x, _ = ro.map_dimensions({'security': 3, 'code review': 3}, 3)
    y, _ = ro.map_dimensions({'code review': 3, 'security': 3}, 3)
    assert ro.band_key(x) == ro.band_key(y)


def test_hardness_alone_answers_when_the_vocabulary_misses():
    """The model's own words are not guaranteed to be in our alias table. That must
    not lose the rating: hardness is the direct answer to the question, and the
    unmapped words are reported rather than dropped."""
    levels, meta = ro.map_dimensions({'quantum widget wrangling': 3}, 2)
    assert levels, 'hardness alone must still produce a matrix'
    assert 'quantum widget wrangling' in meta['unmapped']
    assert meta['hardness'] == 2
    assert ro.band_key(levels)


def test_routine_work_becomes_a_negative_demand_on_purpose():
    """Hardness 0 means a cheap lane is the RIGHT answer - chosen, not accidental."""
    levels, _ = ro.map_dimensions({'mechanical': 1}, 0)
    assert min(levels.values()) <= -1


def test_no_hardness_and_no_mapped_dimensions_yields_no_band():
    levels, meta = ro.map_dimensions({}, None)
    assert ro.band_key(levels) is None
    assert meta['unmapped'] == []


def test_the_model_answer_is_never_asked_to_use_our_vocabulary():
    """v2's contract: free-form dimension words, one hardness integer."""
    prompt = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'data', 'classifier', 'prompt-v2.md')).read()
    assert 'hardness' in prompt and 'dimensions' in prompt
    assert 'use EXACTLY these keys' not in prompt
    assert 'Never invent category names' not in prompt
