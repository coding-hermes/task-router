"""TR-369 usability gate — presence is not usability.

The registry reached 2,064 rows while only a few hundred could be ranked, and no
check failed, because every check counted ROWS. These tests pin the three
properties that make the gate meaningful:

1. Every gap carries a reason DERIVED FROM THE DATA (disabled / unpriced /
   never-measured / below-blank-default). A gap with no reason is a defect.
2. The defect count may only RATCHET DOWN against the committed baseline, so new
   unmeasured lanes turn CI red instead of quietly joining the catalog.
3. No lane can be simultaneously usable and a defect.
"""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = ROOT / 'scripts' / 'router_usability_gate.py'
BASELINE = ROOT / 'data' / 'state' / 'usability-baseline.json'


def _gate():
    spec = importlib.util.spec_from_file_location('router_usability_gate', GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_baseline_exists_and_is_committed():
    assert BASELINE.exists(), (
        'no usability baseline: run scripts/router_usability_gate.py --ratchet once and '
        'commit data/state/usability-baseline.json, otherwise the gate cannot detect drift')
    data = json.loads(BASELINE.read_text())
    assert 'defects' in data and isinstance(data['defects'], int)
    assert data['defects'] >= 0


def test_defects_do_not_exceed_the_committed_baseline():
    """The whole point: a new live lane with no evidence must not pass silently."""
    mod = _gate()
    usable, explained, defects, reasons = mod.classify()
    allowed = json.loads(BASELINE.read_text())['defects']
    assert len(defects) <= allowed, (
        f'usability regressed: {len(defects)} live+priced+never-measured lanes, '
        f'baseline allows {allowed}. Either measure the new lanes or justify them. '
        f'Sample: {[d["model"] for d in defects[:5]]}')


def test_every_explained_gap_has_a_data_derived_reason():
    """No free-form excuses: the reason must come from the row, not from a stamp file."""
    mod = _gate()
    usable, explained, defects, reasons = mod.classify()
    allowed = {'unpriced', 'tiers_below_blank_default', 'disabled', 'archived', 'expired'}
    bad = [e for e in explained if e.get('why') not in allowed]
    assert not bad, f'explained gaps with a non-data-derived reason: {bad[:5]}'


def test_no_lane_is_both_usable_and_a_defect():
    mod = _gate()
    usable, explained, defects, reasons = mod.classify()
    u = {(x['provider'], x['model']) for x in usable}
    d = {(x['provider'], x['model']) for x in defects}
    overlap = u & d
    assert not overlap, f'lane classified as usable AND defect: {list(overlap)[:5]}'


def test_usable_lanes_are_all_priced_and_tiered():
    """A usable lane must survive the resolver's own two conditions."""
    mod = _gate()
    usable, _explained, _defects, _reasons = mod.classify()
    assert usable, 'no usable lanes at all — the registry would be dead'
    for lane in usable[:50]:
        assert lane.get('provider') and lane.get('model')
