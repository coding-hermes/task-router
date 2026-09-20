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
    """The 09-13 case: active, in the catalog, price NULL -> must be flagged."""
    gaps = rm._pricing_gaps([_m('ollama-cloud', 'deepseek-v4.1-flash')])
    assert len(gaps) == 1
    assert gaps[0]['provider'] == 'ollama-cloud'
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
