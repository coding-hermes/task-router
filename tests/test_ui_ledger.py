"""TR-151: the ledger browser must not be able to imply completeness it does not have."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_server as rs  # noqa: E402


def _store(tmp_path, rows):
    p = tmp_path / 'outcomes.jsonl'
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return str(p)


ROWS = [
    {'source_system': 'router-proxy', 'provider': 'xkiro', 'model': 'openai/gpt-6-luna',
     'ts': 1000.0, 'success': True, 'cost_usd': 0.0033, 'complexity_source': 'classifier',
     'complexity_sig': 'sig-a', 'session_id': 's1', 'price_basis': 'plan-effective: ...'},
    {'source_system': 'router-proxy', 'provider': 'stepfun', 'model': 'step-3.7-flash',
     'ts': 2000.0, 'success': False, 'cost_usd': None, 'complexity_source': 'classifier',
     'complexity_sig': 'sig-b', 'session_id': 's2', 'failure_reason': 'hop-wall-timeout'},
    {'source_system': 'hermes', 'provider': 'deepseek', 'model': 'deepseek-v4-flash',
     'ts': 3000.0, 'success': None, 'cost_usd': 0.0385, 'session_id': 's3'},
]


def test_free_text_and_filters(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', _store(tmp_path, ROWS))
    r = rs.ui_ledger({'q': 'luna'})
    assert r['total_matched'] == 1 and r['rows'][0]['model'] == 'openai/gpt-6-luna'
    assert rs.ui_ledger({'provider': 'stepfun'})['total_matched'] == 1
    assert rs.ui_ledger({'band': 'sig-a'})['total_matched'] == 1
    assert rs.ui_ledger({'complexity_source': 'classifier'})['total_matched'] == 2
    assert rs.ui_ledger({'outcome': 'failed'})['total_matched'] == 1
    assert rs.ui_ledger({'since': 2500.0})['total_matched'] == 1
    assert rs.ui_ledger({'until': 1500.0})['total_matched'] == 1


def test_the_response_always_reports_what_it_read(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', _store(tmp_path, ROWS))
    r = rs.ui_ledger({})
    assert r['rows_scanned'] == len(ROWS)
    assert r['scan_truncated'] is False and r['truncated'] is False
    assert r['total_matched'] == len(ROWS)


def test_a_truncated_scan_can_never_look_complete(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', _store(tmp_path, ROWS))
    r = rs.ui_ledger({'scan_limit': '2'})
    assert r['rows_scanned'] == 2
    assert r['scan_truncated'] is True and r['truncated'] is True


def test_a_truncated_page_is_reported_too(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', _store(tmp_path, ROWS))
    r = rs.ui_ledger({'limit': '1'})
    assert r['returned'] == 1 and r['total_matched'] == 3 and r['truncated'] is True


def test_paging_walks_the_matches(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', _store(tmp_path, ROWS))
    first = rs.ui_ledger({'limit': '1', 'offset': '0'})
    second = rs.ui_ledger({'limit': '1', 'offset': '1'})
    assert first['rows'][0]['session_id'] != second['rows'][0]['session_id']


def test_an_unreadable_store_is_an_error_not_an_empty_result(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', str(tmp_path / 'nope.jsonl'))
    r = rs.ui_ledger({})
    assert 'error' in r and r['total_matched'] == 0
