"""TR-144: rolling averages in JSONL, per model AND per complexity band.

Hermetic: every test builds its own ledger in tmp_path. The point of these
contracts is the honesty rules — a metric with no samples is None with a reason
(never 0, which would read as a perfect 0% failure rate), every average discloses
its sample count, cost averages disclose how many samples were priced, a window
discloses how much of the file it scanned, and a rebuild is byte-reproducible.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_proxy_stats as rps   # noqa: E402

NOW = 1_800_000_000.0   # fixed clock: nothing here depends on the wall clock


def _row(**kw):
    row = {'source_system': 'router-proxy', 'session_id': 's', 'provider': 'p1',
           'model': 'm1', 'required_categories': {'code_gen': 2}, 'complexity_sig': 'sig-a',
           'steps': 1, 'tokens_in': 100, 'tokens_out': 10, 'cache_read_tokens': 50,
           'cost_usd': 0.02, 'wall_time_s': 1.5, 'success': True, 'ts': NOW - 60}
    row.update(kw)
    return row


def _ledger(tmp_path, rows, name='outcomes.jsonl'):
    path = tmp_path / name
    # an EMPTY ledger writes nothing: one blank line would count as a scanned row
    path.write_text(('\n'.join(json.dumps(r) for r in rows) + '\n') if rows else '')
    return str(path)


# ---------- the band key ----------

def test_band_follows_the_required_levels():
    assert rps.band_for({'required_categories': {'a': 0, 'b': -2}})[0] == 'trivial'
    assert rps.band_for({'required_categories': {'a': 2}})[0] == 'light'
    assert rps.band_for({'required_categories': {'a': 3, 'b': 1}})[0] == 'medium'
    assert rps.band_for({'required_categories': {'a': 4}})[0] == 'heavy'
    assert rps.band_for({'required_categories': {'a': 5}})[0] == 'frontier'


def test_an_unknown_complexity_stays_unknown():
    """Guessing a band would put rows in a bucket nothing measured."""
    assert rps.band_for({}) == (None, 'unknown')
    assert rps.band_for({'required_categories': 'nonsense'}) == (None, 'unknown')


# ---------- the rollup math ----------

def test_rollup_averages_and_rates_per_model_and_band(tmp_path):
    rows = [
        _row(cost_usd=0.02, steps=1, wall_time_s=1.0, tokens_in=100, cache_read_tokens=50),
        _row(cost_usd=0.04, steps=3, wall_time_s=3.0, tokens_in=300, cache_read_tokens=150,
             success=False, failure_reason='hop-wall-timeout'),
        _row(provider='p2', model='m2', required_categories={'code_gen': 4}, cost_usd=1.0),
    ]
    data = rps.get_rollup(_ledger(tmp_path, rows), windows=(24,), now=NOW)
    groups = data['24h']['groups']

    a = groups['p1/m1|light']
    assert a['samples'] == 2 and a['cost_samples'] == 2
    assert a['success_rate'] == pytest.approx(0.5)
    assert a['cost_usd_per_task'] == pytest.approx(0.03)
    assert a['steps_per_task'] == pytest.approx(2.0)
    assert a['wall_time_s_per_task'] == pytest.approx(2.0)
    assert a['cache_read_ratio'] == pytest.approx(200 / 400)
    assert a['failure_reasons'] == {'hop-wall-timeout': 1}

    # the same model at a different band is a DIFFERENT group: that is the point
    assert 'p2/m2|heavy' in groups
    assert 'p2/m2|light' not in groups


def test_grouping_collapses_the_keys_deliberately(tmp_path):
    rows = [_row(), _row(required_categories={'code_gen': 4})]
    path = _ledger(tmp_path, rows)
    assert set(rps.get_rollup(path, windows=(24,), now=NOW)['24h']['groups']) == {
        'p1/m1|light', 'p1/m1|heavy'}
    assert set(rps.get_rollup(path, windows=(24,), now=NOW, grouping='model')['24h']['groups']) == {'p1/m1'}
    assert set(rps.get_rollup(path, windows=(24,), now=NOW, grouping='band')['24h']['groups']) == {'light', 'heavy'}


# ---------- the honesty rules ----------

def test_a_cold_start_is_None_with_a_reason_never_a_zero(tmp_path):
    data = rps.get_rollup(_ledger(tmp_path, []), windows=(24,), now=NOW)
    assert data['24h']['groups'] == {}
    assert data['24h']['rows_scanned'] == 0 and data['24h']['proxy_rows_matched'] == 0


def test_an_unpriced_lane_reports_None_and_says_why(tmp_path):
    """A plan lane with no published price is UNMEASURED, not free."""
    rows = [_row(cost_usd=None, success=True), _row(cost_usd=None, success=True)]
    g = rps.get_rollup(_ledger(tmp_path, rows), windows=(24,), now=NOW)['24h']['groups']['p1/m1|light']
    assert g['cost_usd_per_task'] is None
    assert g['cost_samples'] == 0 and 'no priced samples' in g['cost_reason']
    # ...while the metrics that WERE measurable are still reported
    assert g['success_rate'] == 1.0


def test_missing_input_tokens_leave_the_cache_ratio_None(tmp_path):
    rows = [_row(tokens_in=None, cache_read_tokens=None)]
    g = rps.get_rollup(_ledger(tmp_path, rows), windows=(24,), now=NOW)['24h']['groups']['p1/m1|light']
    assert g['cache_read_ratio'] is None and 'not reported' in g['cache_reason']
    assert g['cache_samples'] == 0


def test_a_window_discloses_what_it_scanned(tmp_path):
    old = _row(ts=NOW - 48 * 3600)
    fresh = _row(ts=NOW - 60)
    data = rps.get_rollup(_ledger(tmp_path, [old, fresh]), windows=(24, 168), now=NOW)
    assert data['24h']['rows_scanned'] == 2 and data['24h']['rows_in_window'] == 1
    assert data['168h']['rows_in_window'] == 2


def test_non_proxy_rows_are_not_counted_into_proxy_averages(tmp_path):
    other = {'source_system': 'cli', 'provider': 'p1', 'model': 'm1', 'cost_usd': 99.0,
             'success': True, 'ts': NOW - 60, 'complexity': {'code_gen': 2}}
    rows = [_row(), other]
    data = rps.get_rollup(_ledger(tmp_path, rows), windows=(24,), now=NOW)
    assert data['24h']['groups']['p1/m1|light']['samples'] == 1
    assert data['24h']['groups']['p1/m1|light']['cost_usd_per_task'] == pytest.approx(0.02)


def test_a_missing_ledger_is_empty_not_an_exception(tmp_path):
    data = rps.get_rollup(str(tmp_path / 'nope.jsonl'), windows=(24,), now=NOW)
    assert data['24h']['groups'] == {} and data['24h']['rows_scanned'] == 0


# ---------- the JSONL snapshot ----------

def test_a_rebuild_is_byte_reproducible(tmp_path):
    """The delete-and-rebuild proof: the file is a function of the data alone."""
    rows = [_row(), _row(success=False, failure_reason='hop-wall-timeout')]
    data = rps.get_rollup(_ledger(tmp_path, rows), windows=(24,), now=NOW)
    path = str(tmp_path / 'proxy_rollups.jsonl')
    first = rps.rebuild(path, data, '2026-09-25T00:00:00Z')
    second = rps.rebuild(path, data, '2026-09-25T00:00:00Z')
    assert first == second
    assert open(path).read() == first + '\n'
    parsed = json.loads(first)
    assert parsed['kind'] == 'proxy-rolling-averages'
    assert parsed['windows']['24h']['groups']['p1/m1|light']['samples'] == 2


def test_snapshots_append_without_rewriting_history(tmp_path):
    path = str(tmp_path / 'proxy_rollups.jsonl')
    data = rps.get_rollup(_ledger(tmp_path, [_row()]), windows=(24,), now=NOW)
    rps.append_snapshot(path, data, '2026-09-25T00:00:00Z')
    rps.append_snapshot(path, data, '2026-09-25T01:00:00Z')
    lines = [l for l in open(path) if l.strip()]
    assert len(lines) == 2
    assert [json.loads(l)['generated_at'] for l in lines] == [
        '2026-09-25T00:00:00Z', '2026-09-25T01:00:00Z']


# ---------- the cached path ----------

def test_the_cache_discloses_its_age_and_reuses_the_scan(tmp_path, monkeypatch):
    path = _ledger(tmp_path, [_row()])
    rps._CACHE.update({'at': 0.0, 'path': None, 'value': None})
    monkeypatch.setenv('ROUTER_PROXY_STATS_TTL_S', '600')
    first = rps.get_rollup_cached(path, windows=(24,))
    assert first['cached'] is False and first['age_s'] >= 0
    assert first['ttl_s'] == 600
    second = rps.get_rollup_cached(path, windows=(24,))
    assert second['cached'] is True, 'the second call must not rescan 115MB'


def test_rolling_for_an_unknown_lane_is_None_not_zeros(tmp_path):
    path = _ledger(tmp_path, [_row()])
    rps._CACHE.update({'at': 0.0, 'path': None, 'value': None})
    assert rps.rolling_for('p1', 'm1', 'light', path=path)['samples'] == 1
    assert rps.rolling_for('nope', 'nope', 'light', path=path) is None


def test_windows_come_from_the_env_with_a_sane_default(monkeypatch):
    monkeypatch.delenv('ROUTER_STATS_WINDOWS', raising=False)
    assert rps.windows_from_env() == rps.DEFAULT_WINDOWS_H
    monkeypatch.setenv('ROUTER_STATS_WINDOWS', '6, 48 ,junk,')
    assert rps.windows_from_env() == (6.0, 48.0)
