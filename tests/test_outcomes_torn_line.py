"""TR-244: one torn JSONL line must never stall the outcomes ledger.

2026-09-29 incident: a concurrent append truncated a record mid-token (the
suffix of the NEXT row rode the same physical line), so the whole file failed
`json.loads` — every import driver crashed with JSONDecodeError, the hourly
cron reported all 4 drivers FAILED for ~39h and the gate reported the store
38h behind. The data was repaired by hand; the CODE change makes the two
whole-store scans tolerant: skip + warn on malformed lines, dedupe and bucket
semantics unchanged for good rows.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro

NOW = 1_800_000_000.0

# The literal torn line from the incident: a record cut mid-token with the
# suffix of the following record riding the same physical line.
TORN = '{"source_system": "hermes", "tokens_in": 324, "to{"source_system": "router-proxy"}'


def _row(model='m1', provider='p1', complexity='P1', cost=1.0, source='test'):
    return {'source_system': source, 'session_id': f'{source}-{model}-{cost}',
            'complexity': complexity, 'provider': provider, 'model': model,
            'turns': 1, 'tokens_in': 100, 'tokens_out': 10, 'tokens_reasoning': 0,
            'cost_usd': cost, 'wall_time_s': 5.0, 'success': True, 'ts': NOW}


def test_append_rows_tolerates_torn_line(tmp_path, capsys):
    """The append scan skips the torn line (with a stderr warning) instead of
    raising; good rows already in the store still dedupe the append."""
    p = str(tmp_path / 'outcomes.jsonl')
    with open(p, 'w') as f:
        f.write(json.dumps(_row(model='m1')) + '\n')
        f.write(TORN + '\n')
        f.write(json.dumps(_row(model='m2')) + '\n')
    good = [_row(model='m1'), _row(model='m2'), _row(model='m3')]
    n = ro.append_rows(p, good)  # must NOT raise
    assert n == 1  # m1/m2 already present, only m3 is new
    lines = ro.load_outcome_rows(p)  # tolerant scan for verification
    models = sorted(r['model'] for r in lines)
    assert models == ['m1', 'm2', 'm3']
    err = capsys.readouterr().err
    assert 'append_rows: skipped malformed line' in err
    assert TORN[:80] in err  # names the offender, first 80 chars


def test_append_rows_dedupe_still_works_after_torn_line(tmp_path):
    """Dedupe semantics unchanged for good lines: over a store that contains a
    torn line, the first append adds the new good rows and the SECOND append
    of the same rows adds 0 (the brief's twice-appended case)."""
    p = str(tmp_path / 'outcomes.jsonl')
    with open(p, 'w') as f:
        f.write(TORN + '\n')
    rows = [_row(model='m1'), _row(model='m2')]
    assert ro.append_rows(p, rows) == 2
    assert ro.append_rows(p, rows) == 0  # dedupe holds on the second append
    assert ro.append_rows(p, [_row(model='m3')]) == 1  # genuinely new rows still land


def test_append_rows_warns_on_bad_row_objects_too(tmp_path, capsys):
    """Non-dict rows (a bare string, a list) are also skipped + warned, not
    fatal: the dedupe key assumes a mapping shape."""
    p = str(tmp_path / 'outcomes.jsonl')
    with open(p, 'w') as f:
        f.write(json.dumps(_row(model='m1')) + '\n')
        f.write('"just a string"\n')
        f.write(json.dumps(_row(model='m2')) + '\n')
    assert ro.append_rows(p, [_row(model='m3')]) == 1
    err = capsys.readouterr().err
    assert 'append_rows: skipped malformed line' in err


def test_averages_scan_tolerates_torn_line(tmp_path, capsys, monkeypatch):
    """The averages command's whole-store scan skips the torn line and still
    produces buckets from the good rows (drives the same scan loop the CLI
    uses, via the ROUTING_OUTCOMES_FILE resolver — not a pre-filtered list)."""
    p = str(tmp_path / 'outcomes.jsonl')
    with open(p, 'w') as f:
        f.write(json.dumps(_row(model='m1', cost=2.0)) + '\n')
        f.write(TORN + '\n')
        f.write(json.dumps(_row(model='m1', cost=6.0)) + '\n')
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', p)
    rows = ro.load_outcome_rows(OUTCOMES := ro.outcomes_path())
    assert capsys.readouterr().err.count('averages: skipped malformed line') == 1
    avgs = ro.compute_averages(rows, scales_h=[24], now_s=NOW)
    assert len(avgs) == 1  # one bucket from the good rows
    a = avgs[0]
    assert a['provider'] == 'p1' and a['model'] == 'm1'
    assert a['avg_cost_task_24h'] == pytest.approx(4.0)  # 2.0 and 6.0, both fresh
    assert OUTCOMES == p
