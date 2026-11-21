"""Outcome attribution — which failures the TR-354 floor is allowed to charge
to a model, and the field trap that makes an audit read the ledger wrong.

Born from a filed defect card that measured 50,713 "pre-model rejections" by
selecting failures on `success_reason is null`. `success_reason` is the
SUCCESS-side field: it is null on 61,680 of 63,665 failures and carries a
scheduler tick verdict on the other 1,985, so it is not an attribution signal
in either direction. The failure-side field is `failure_reason`, and the
producer already stamps it (every router-proxy failure since 2026-10 carries
one). These tests pin the real contract so the next audit cannot re-derive the
phantom:

1. `compute_averages` charges a failure to the model unless its
   `failure_reason` is in `ROUTER_SIDE_FAILURE_REASONS` — a router-side reason
   leaves the denominator, an unattributed failure stays in it, and a named
   non-router reason (a 402 no-credit) stays in it too. That last one is
   CURRENT DOCTRINE, not an oversight: widening the set moves routing for
   every profile and needs an owner ruling.
2. `outcome_attribution_census` classifies rows the same way the gate does —
   pinned by comparing its attributable count against `compute_averages`'
   `n_success_known` on the same rows, so the measuring tool cannot drift from
   the thing it measures.
3. The census MEASURES and never reclassifies: it does not touch
   `ROUTER_SIDE_FAILURE_REASONS`.

Hermetic: synthetic rows only, no live store, no network.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import outcome_attribution_census as census_mod  # noqa: E402
import router_outcomes as ro  # noqa: E402

TS = 1795000000.0


def _row(provider='p1', model='m1', success=True, failure_reason=None,
         success_reason=None, tokens_in=100, tokens_out=10, wall_time_s=5.0,
         source_system='router-proxy', ts=TS):
    row = {'source_system': source_system, 'session_id': f's-{ts}-{model}',
           'provider': provider, 'model': model, 'tokens_in': tokens_in,
           'tokens_out': tokens_out, 'cost_usd': 0.01, 'wall_time_s': wall_time_s,
           'success': success, 'ts': ts}
    if failure_reason is not None:
        row['failure_reason'] = failure_reason
    if success_reason is not None:
        row['success_reason'] = success_reason
    return row


def _averages(rows):
    """The gate's own view: one bucket per (provider, model)."""
    return {(e['provider'], e['model']): e
            for e in ro.compute_averages(rows, scales_h=(24,), now_s=TS)}


# --------------------------------------------------------------------------
# 1. the gate's contract
# --------------------------------------------------------------------------

def test_a_router_side_reason_leaves_the_denominator():
    """A hop the router killed is not a model verdict: it must not move the
    completion rate the floor reads."""
    rows = [_row(success=True) for _ in range(3)]
    rows += [_row(success=False, failure_reason='no-hops') for _ in range(7)]
    entry = _averages(rows)[('p1', 'm1')]
    assert entry['n_router_side'] == 7
    assert entry['n_success_known'] == 3
    assert entry['success_rate'] == 1.0


def test_an_unattributed_failure_stays_in_the_denominator():
    """A failure nobody can explain is still a failure — the documented,
    conservative choice. This is the half of the contract a producer fix
    exists to shrink, never to bypass."""
    rows = [_row(success=True) for _ in range(3)]
    rows += [_row(success=False) for _ in range(7)]
    entry = _averages(rows)[('p1', 'm1')]
    assert entry['n_router_side'] == 0
    assert entry['n_unattributed'] == 7
    assert entry['n_success_known'] == 10
    assert entry['success_rate'] == pytest.approx(0.3)


def test_a_named_non_router_reason_still_charges_the_model():
    """CURRENT DOCTRINE, pinned so a change is deliberate.

    A 402 no-credit is a provider-account state, not a model-quality verdict,
    yet it is NOT in ROUTER_SIDE_FAILURE_REASONS, so it stays in the
    denominator. Moving it is a doctrine change that shifts routing for every
    profile and needs an owner ruling — this test exists so that change cannot
    land silently as a "bug fix".
    """
    assert '402-no-credit' not in ro.ROUTER_SIDE_FAILURE_REASONS
    rows = [_row(success=True) for _ in range(3)]
    rows += [_row(success=False, failure_reason='402-no-credit') for _ in range(7)]
    entry = _averages(rows)[('p1', 'm1')]
    assert entry['n_router_side'] == 0
    assert entry['n_success_known'] == 10
    assert entry['success_rate'] == pytest.approx(0.3)


def test_the_router_side_vocabulary_is_the_documented_four():
    """The set is a contract, not an accident: four reasons, each a statement
    about the proxy's own decision."""
    assert ro.ROUTER_SIDE_FAILURE_REASONS == frozenset({
        'hop-wall-timeout', 'no-hops', 'unservable-rating', 'gateway-draining'})


# --------------------------------------------------------------------------
# 2. the census agrees with the gate
# --------------------------------------------------------------------------

def test_census_attributable_count_matches_the_gate():
    """The measuring tool and the measured gate must agree row for row."""
    rows = ([_row(success=True) for _ in range(4)]
            + [_row(success=False, failure_reason='no-hops') for _ in range(3)]
            + [_row(success=False, failure_reason='402-no-credit') for _ in range(2)]
            + [_row(success=False) for _ in range(5)])
    entry = _averages(rows)[('p1', 'm1')]
    result = census_mod.census(rows)
    assert result['classes']['router-side'] == entry['n_router_side'] == 3
    assert result['classes']['unattributed'] == entry['n_unattributed'] == 5
    assert result['classes']['attributed-other'] == 2
    assert result['n_failures'] == 10


def test_census_never_reclassifies():
    """A census that widened the gate would be a doctrine change wearing a
    measurement's clothes."""
    before = set(ro.ROUTER_SIDE_FAILURE_REASONS)
    census_mod.census([_row(success=False, failure_reason='402-no-credit')])
    assert set(ro.ROUTER_SIDE_FAILURE_REASONS) == before


# --------------------------------------------------------------------------
# 3. the field trap
# --------------------------------------------------------------------------

def test_success_reason_is_not_an_attribution_signal():
    """The exact mis-read that produced the phantom card.

    Selecting failures on `success_reason is null` returns every failure that
    has no success-side reason — which is most of them — and silently drops
    the tick verdicts that do carry one. The census must key on
    `failure_reason`, and the two selections must be shown to disagree.
    """
    rows = ([_row(success=False, failure_reason='no-hops') for _ in range(2)]
            + [_row(success=False) for _ in range(3)]
            + [_row(success=False, success_reason='tick_concluded_no_commits')
               for _ in range(4)])
    failures = [r for r in rows if r.get('success') is False]
    by_success_reason = [r for r in failures if not r.get('success_reason')]
    assert len(by_success_reason) == 5          # misses the 4 tick verdicts
    assert len(failures) == 9
    result = census_mod.census(rows)
    assert result['n_success_reason_on_failures'] == 4
    assert result['classes']['router-side'] == 2
    assert result['classes']['tick-verdict'] == 4
    assert result['classes']['unattributed'] == 3


def test_a_tick_verdict_is_its_own_class_not_unattributed():
    """A tick that concluded with no commits is a statement about the tick.
    It is charged to the model today, so it must be visible as its own class
    rather than buried in the unattributed bucket."""
    row = _row(success=False, success_reason='tick_concluded_no_commits',
               source_system='hermes')
    assert census_mod.classify_failure(row) == 'tick-verdict'
    assert census_mod.is_tick_verdict(row) is True
    # a failure with neither field is genuinely unattributed
    assert census_mod.classify_failure(_row(success=False)) == 'unattributed'
    # a named reason wins over the tick-verdict shape
    assert census_mod.classify_failure(
        _row(success=False, failure_reason='no-hops',
             success_reason='tick_concluded_no_commits')) == 'router-side'


# --------------------------------------------------------------------------
# 4. pre-model classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize('tokens_in,tokens_out,wall,expected', [
    (None, None, 0.0, True),      # instant rejection, no accounting
    (None, None, 0.999, True),
    (None, None, 1.0, False),     # the boundary is exclusive
    (None, None, 5.0, False),
    (0, 0, 0.01, False),          # a reported zero is still accounting
    (100, 10, 0.01, False),
])
def test_premodel_boundary(tokens_in, tokens_out, wall, expected):
    row = _row(success=False, tokens_in=tokens_in, tokens_out=tokens_out,
               wall_time_s=wall)
    assert census_mod.is_premodel(row) is expected


def test_a_success_is_never_premodel():
    assert census_mod.is_premodel(_row(success=True, tokens_in=None,
                                       tokens_out=None, wall_time_s=0.0)) is False


# --------------------------------------------------------------------------
# 5. floor impact counterfactuals
# --------------------------------------------------------------------------

def test_floor_impact_separates_the_two_counterfactuals():
    """Three sunk lanes, three different causes: only the two whose cause is a
    non-model row may clear, and the genuinely bad lane must stay sunk."""
    rows = []
    # sunk by unattributed pre-model rejections (8 real passes survive the drop)
    rows += [_row(provider='pre', model='m', success=True) for _ in range(8)]
    rows += [_row(provider='pre', model='m', success=False, tokens_in=None,
                  tokens_out=None, wall_time_s=0.01) for _ in range(15)]
    # sunk by tick verdicts
    rows += [_row(provider='tick', model='m', success=True) for _ in range(8)]
    rows += [_row(provider='tick', model='m', success=False,
                  success_reason='tick_concluded_no_commits') for _ in range(15)]
    # genuinely bad: real model failures, nothing to drop
    rows += [_row(provider='bad', model='m', success=True) for _ in range(1)]
    rows += [_row(provider='bad', model='m', success=False,
                  failure_reason='transport-error') for _ in range(9)]
    floor = census_mod.floor_impact(rows, min_known=8, rate_floor=0.35)
    assert floor['n_floored'] == 3
    assert [e['provider'] for e in floor['would_clear_without_unattributed_premodel']] == ['pre']
    assert [e['provider'] for e in floor['would_clear_without_tick_verdicts']] == ['tick']
    assert floor['would_clear_without_unattributed_premodel'][0]['success_rate_after'] == 1.0
    assert floor['would_clear_without_tick_verdicts'][0]['success_rate_after'] == 1.0


def test_dropping_rows_below_the_known_floor_is_not_a_rescue():
    """A lane whose only evidence was the dropped rows becomes UNMEASURED, not
    healthy. Counting that as a rescue would trade one silent verdict for
    another."""
    rows = [_row(provider='thin', model='m', success=True) for _ in range(4)]
    rows += [_row(provider='thin', model='m', success=False, tokens_in=None,
                  tokens_out=None, wall_time_s=0.01) for _ in range(10)]
    floor = census_mod.floor_impact(rows, min_known=8, rate_floor=0.35)
    assert floor['n_floored'] == 1
    assert floor['n_would_clear_without_unattributed_premodel'] == 0


def test_floor_impact_respects_the_known_floor():
    """Below min_known a poor rate is thin evidence, not a verdict."""
    rows = [_row(provider='thin', model='m', success=False) for _ in range(7)]
    floor = census_mod.floor_impact(rows, min_known=8, rate_floor=0.35)
    assert floor['n_floored'] == 0


def test_floor_impact_ignores_unknown_verdicts():
    """success=null is not a failure and not a completion."""
    rows = [_row(provider='u', model='m', success=None) for _ in range(20)]
    floor = census_mod.floor_impact(rows, min_known=8, rate_floor=0.35)
    assert floor['n_floored'] == 0


# --------------------------------------------------------------------------
# 6. the CLI
# --------------------------------------------------------------------------

def test_cli_reads_a_store_and_emits_json(tmp_path, capsys):
    store = tmp_path / 'outcomes.jsonl'
    store.write_text('\n'.join(json.dumps(r) for r in [
        _row(success=True), _row(success=False, failure_reason='no-hops'),
        _row(success=False),
    ]) + '\n', encoding='utf-8')
    assert census_mod.main(['--store', str(store), '--json']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['n_rows'] == 3
    assert out['classes']['router-side'] == 1
    assert out['classes']['unattributed'] == 1


def test_cli_text_report_names_the_field_trap(tmp_path, capsys):
    store = tmp_path / 'outcomes.jsonl'
    store.write_text(json.dumps(_row(success=False)) + '\n', encoding='utf-8')
    assert census_mod.main(['--store', str(store)]) == 0
    out = capsys.readouterr().out
    assert 'success_reason' in out
    assert 'not an attribution signal' in out


def test_cli_missing_store_is_a_usage_error(tmp_path, capsys):
    assert census_mod.main(['--store', str(tmp_path / 'nope.jsonl')]) == 2
    assert 'cannot read' in capsys.readouterr().err


def test_a_torn_line_does_not_blind_the_census(tmp_path):
    """The store is append-only; a killed writer leaves a partial tail."""
    store = tmp_path / 'outcomes.jsonl'
    store.write_text(json.dumps(_row(success=True)) + '\n{"torn": \n',
                     encoding='utf-8')
    rows = census_mod.load_rows(str(store))
    assert len(rows) == 1
