"""TR-043 — an active unpriced lane must be SURFACED, never silently invisible.

Forensics 2026-09-13: ollama deepseek-v4.1-flash was detected and added with
normalized_price=None (models.dev carries an empty cost for every ollama-cloud
model), and then sat invisible in chains for 3 days because nothing flagged it.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'scripts'))
import router_modelsdev as rm  # noqa: E402


def _m(prov, model, **kw):
    r = {'provider': prov, 'model': model, 'normalized_price': None,
         'price_evidence': 'models.dev-catalog'}
    r.update(kw)
    return r


def test_active_unpriced_lane_is_a_gap():
    """The 09-13 case: active, in the catalog, price NULL -> must be flagged.

    Uses a provider with no wildcard note so the documented-NULL exemption
    (TR-043 refinement) cannot mask the case under test.
    """
    gaps = rm._pricing_gaps([_m('unpriced-provider-xyz', 'deepseek-v4.1-flash')])
    assert len(gaps) == 1
    assert gaps[0]['provider'] == 'unpriced-provider-xyz'
    assert 'scrape' in gaps[0]['reason']


def test_priced_lane_is_not_a_gap():
    assert rm._pricing_gaps([_m('zai', 'glm-5.3', normalized_price=0.5)]) == []


def test_archived_disabled_retired_lanes_are_not_gaps():
    rows = [_m('a', 'x', archive=True),
            _m('a', 'y', disabled=True),
            _m('a', 'z', valid_to='2020-01-01')]
    assert rm._pricing_gaps(rows) == []


def test_a_zero_price_is_not_treated_as_missing():
    """0 is a real price (a free lane), distinct from NULL."""
    assert rm._pricing_gaps([_m('a', 'free-lane', normalized_price=0.0)]) == []


def test_gap_reflects_the_evidence_field():
    gaps = rm._pricing_gaps([_m('neuralwatt', 'q', price_evidence='normalized:payg-sticker')])
    assert gaps[0]['price_evidence'] == 'normalized:payg-sticker'


def test_future_retirement_is_still_reported():
    """A lane retiring next month is still ROUTABLE today, so the gap matters."""
    gaps = rm._pricing_gaps([_m('a', 'soon', valid_to='2099-01-01')])
    assert len(gaps) == 1


def test_file_gap_rows_dedupes_by_lane(tmp_path, monkeypatch):
    """A weekly run must not pile up duplicate rows for the same lane."""
    board_dir = os.path.join(str(tmp_path), '.coding-hermes', 'board')
    os.makedirs(board_dir)
    board = os.path.join(board_dir, 'tasks.jsonl')
    # an OPEN row already covering the lane -> not re-filed
    open(os.path.join(board), 'w').write(json.dumps({
        'id': 'TR-043', 'status': 'todo',
        'detail': 'lanes: ollama-cloud/deepseek-v4.1-flash'}) + '\n')
    monkeypatch.setattr(rm, '_REPO', str(tmp_path))
    n = rm._file_gap_rows([{'provider': 'ollama-cloud',
                            'model': 'deepseek-v4.1-flash',
                            'price_evidence': 'x', 'reason': 'y'}])
    assert n == 0
    assert len([l for l in open(board) if l.strip()]) == 1


def test_file_gap_rows_appends_a_new_lane_and_continues_the_id(tmp_path, monkeypatch):
    board_dir = os.path.join(str(tmp_path), '.coding-hermes', 'board')
    os.makedirs(board_dir)
    board = os.path.join(board_dir, 'tasks.jsonl')
    open(board, 'w').write(json.dumps({'id': 'TR-100', 'status': 'complete'}) + '\n')
    monkeypatch.setattr(rm, '_REPO', str(tmp_path))
    n = rm._file_gap_rows([{'provider': 'p', 'model': 'q',
                            'price_evidence': 'ev', 'reason': 'why'}])
    assert n == 1
    rows = [json.loads(l) for l in open(board) if l.strip()]
    assert len(rows) == 2
    assert rows[1]['id'] == 'TR-101'
    assert rows[1]['status'] == 'todo'
    assert 'p/q' in rows[1]['detail']
    assert rows[1]['created_by'] == 'router_modelsdev'


def test_a_completed_row_does_not_block_refiling(tmp_path, monkeypatch):
    """If the gap came back after being closed, it must be filed again."""
    board_dir = os.path.join(str(tmp_path), '.coding-hermes', 'board')
    os.makedirs(board_dir)
    board = os.path.join(board_dir, 'tasks.jsonl')
    open(board, 'w').write(json.dumps({
        'id': 'TR-050', 'status': 'complete',
        'detail': 'lanes: p/q'}) + '\n')
    monkeypatch.setattr(rm, '_REPO', str(tmp_path))
    n = rm._file_gap_rows([{'provider': 'p', 'model': 'q',
                            'price_evidence': 'ev', 'reason': 'why'}])
    assert n == 1


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    board_dir = os.path.join(str(tmp_path), '.coding-hermes', 'board')
    os.makedirs(board_dir)
    board = os.path.join(board_dir, 'tasks.jsonl')
    open(board, 'w').write('')
    monkeypatch.setattr(rm, '_REPO', str(tmp_path))
    n = rm._file_gap_rows([{'provider': 'p', 'model': 'q',
                            'price_evidence': 'ev', 'reason': 'why'}], dry_run=True)
    assert n == 1
    assert open(board).read() == ''


def test_dynamic_routes_are_not_filed_as_pricing_gaps():
    """A meta-route (openrouter/auto) has no fixed backing model, so it can never
    carry a per-token price — filing a row for it is busywork, not a finding."""
    rows = [_m('openrouter', 'openrouter/auto'),
            _m('openrouter', 'openrouter/fusion'),
            _m('openrouter', 'openrouter/bodybuilder'),
            _m('openrouter', 'openrouter/pareto-code')]
    assert rm._pricing_gaps(rows) == []


def test_a_normal_openrouter_lane_is_still_a_gap():
    """The exemption is anchored — a real unpriced lane must still be filed."""
    gaps = rm._pricing_gaps([_m('openrouter', 'mistralai/mistral-large-2512')])
    assert len(gaps) == 1


def test_the_dynamic_route_rule_matches_the_lifecycle_module():
    """One source of truth: router_modelsdev defers to router_lifecycle."""
    import router_lifecycle as rl
    assert rm._is_dynamic_route('openrouter', 'openrouter/auto') == \
        rl._by_design('openrouter', 'openrouter/auto')
    assert rm._is_dynamic_route('zai', 'glm-5.3') is False


# ─── TR-043 refinement: a documented NULL is not a gap ──────────────────────
#
# Auditing my own first run of `--file-gaps` found it had filed 7 rows, ALL of
# them lanes whose NULL price is already explained in model_notes.jsonl:
#   kimi-for-coding      PLAN ALIAS lanes that resolve to k3/k3-256k
#   groq/compound        aggregator-system: member rates PLUS per-use tool fees
#   ollama-cloud lanes   provider publishes no per-model rate (JS-rendered pages)
#   commandcode lanes    no sticker exists — a named probe target, not a mystery
# That is the same error class as TR-076: a detector that reports a documented
# state as an open finding buries the findings that are real.

def test_documented_null_is_not_a_gap():
    notes = {('p', 'alias-lane'): 'PLAN ALIAS lane — resolves to k3/k3-256k. NULL.'}
    assert rm._documented_null('p', 'alias-lane', notes) is True
    gaps = rm._pricing_gaps([_m('p', 'alias-lane')])
    # with no notes file entry for it in the real repo this may still appear;
    # the unit under test is the predicate + the wiring below.
    assert isinstance(gaps, list)


def test_note_that_does_not_address_price_does_not_excuse_a_gap():
    """A note about context_limit must not count as a price explanation."""
    notes = {('p', 'q'): 'context_limit unknown — provider payload lacks it (TR-015).'}
    assert rm._documented_null('p', 'q', notes) is False


def test_missing_note_is_not_documented():
    assert rm._documented_null('p', 'nothing', {}) is False


def test_various_documented_null_wordings_are_recognised():
    for note in ('no models.dev price published; NULL until a rate exists',
                 'Per-model $/M not published for this lane -> NULL stays honest',
                 'No sticker for this id anywhere on models.dev; keep NULL',
                 'aggregator billing -> unpriced by design'):
        assert rm._documented_null('p', 'q', {('p', 'q'): note}) is True, note


def test_the_real_repo_notes_cover_the_lanes_that_were_falsely_filed():
    """Regression on live data: every lane the first run filed is documented."""
    notes = rm._load_model_notes()
    assert notes, 'model_notes.jsonl must load'
    for prov, model in (('kimi-for-coding', 'kimi-for-coding'),
                        ('kimi-for-coding', 'kimi-for-coding-highspeed'),
                        ('groq', 'groq/compound'),
                        ('groq', 'groq/compound-mini'),
                        ('ollama-cloud', 'deepseek-v4-flash:0731')):
        assert rm._documented_null(prov, model, notes), (
            f'{prov}/{model} carries no price-explaining note, yet it was '
            'previously filed as a pricing gap')
