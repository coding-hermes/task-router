"""TR-049 component 3 — the outcomes_averages.py rolling-average CLI.

Unit + CLI level tests for the exponential-decay windows, the bucket shape and
the dry-run/write paths. No network, no live store.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro
import outcomes_averages as oa  # noqa: E402

NOW = 1_800_000_000.0


def _row(model='m1', provider='p1', complexity=None, cost=1.0, age_s=0, ts=None,
         success=True, source='test'):
    return {'source_system': source, 'session_id': f'{source}-{model}-{age_s}-{cost}',
            'complexity': complexity, 'provider': provider, 'model': model,
            'turns': 1, 'tokens_in': 100, 'tokens_out': 10, 'tokens_reasoning': 0,
            'cost_usd': cost, 'wall_time_s': 5.0, 'success': success,
            'ts': ts if ts is not None else NOW - age_s}


# ---------------------------------------------------------------------------
# TR-049 component 3 — outcomes_averages.py CLI
# ---------------------------------------------------------------------------

def _store_of(tmp_path, rows):
    p = tmp_path / 'outcomes.jsonl'
    with open(p, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    return str(p)


def test_parse_windows_accepts_hours_and_days():
    import outcomes_averages as oa
    assert oa.parse_windows(None) == [24, 72, 168]
    assert oa.parse_windows('1d,3d,7d,30d') == [24, 72, 168, 720]
    assert oa.parse_windows('12h,24') == [12, 24]
    assert oa.parse_windows('48,24,24') == [24, 48]
    with pytest.raises(ValueError):
        oa.parse_windows('soon')
    with pytest.raises(ValueError):
        oa.parse_windows('0')


def test_averages_cli_dry_run_prints_without_writing(tmp_path, capsys):
    import outcomes_averages as oa
    store = _store_of(tmp_path, [_row(source='hermes', cost=2.0, age_s=0),
                                 _row(source='opencode', cost=8.0, age_s=3600)])
    out = tmp_path / 'averages.jsonl'
    rc = oa.main(['--input', store, '--output', str(out), '--dry-run',
                  '--windows', '1d', '--now', str(NOW)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['dry_run'] is True
    assert payload['buckets'] == 2          # per-backend isolation default
    assert payload['rows'] == 2
    assert all(a['avg_cost_task_24h'] is not None for a in payload['averages'])
    assert not out.exists(), '--dry-run must not write'


def test_averages_cli_writes_and_merges(tmp_path, capsys):
    import outcomes_averages as oa
    store = _store_of(tmp_path, [_row(source='hermes', cost=2.0, age_s=0),
                                 _row(source='opencode', cost=8.0, age_s=0)])
    out = tmp_path / 'averages.jsonl'
    rc = oa.main(['--input', store, '--output', str(out), '--merge-backends',
                  '--windows', '1d', '--now', str(NOW)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['buckets'] == 1 and payload['averages'][0]['avg_cost_task_24h'] == pytest.approx(5.0)
    rows = [json.loads(l) for l in open(out) if l.strip()]
    assert len(rows) == 1 and 'source_system' not in rows[0]


def test_averages_cli_is_fail_open_on_a_missing_store(tmp_path, capsys):
    import outcomes_averages as oa
    rc = oa.main(['--input', str(tmp_path / 'nope.jsonl'), '--dry-run'])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['rows'] == 0 and payload['buckets'] == 0


def test_averages_cli_bad_window_is_exit_2(tmp_path, capsys):
    import outcomes_averages as oa
    assert oa.main(['--input', str(tmp_path / 'x.jsonl'), '--windows', 'soon']) == 2
    assert 'bad window' in capsys.readouterr().err


def test_merge_average_rows_is_sample_weighted():
    rows = [{'source_system': 'hermes', 'provider': 'p', 'model': 'm',
             'complexity': None, 'avg_cost_task_24h': 1.0, 'n_samples': 1,
             'n_completed': 0, 'n_success_known': 0},
            {'source_system': 'opencode', 'provider': 'p', 'model': 'm',
             'complexity': None, 'avg_cost_task_24h': 11.0, 'n_samples': 9,
             'n_completed': 0, 'n_success_known': 0}]
    merged = ro.merge_average_rows(rows)
    assert len(merged) == 1
    # 9-sample backend outweighs the 1-sample one (not a flat mean of 6.0)
    assert merged[0]['avg_cost_task_24h'] == pytest.approx(10.0)
    assert merged[0]['n_samples'] == 10
    assert merged[0]['backends'] == ['hermes', 'opencode']
