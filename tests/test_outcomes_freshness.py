"""TR-130: the freshness probe the hourly import job greps for, and what "fresh" must mean.

The job decides its exit code by grepping the AFTER probe for a line beginning
`OK: every present source`. The probe it called was never committed on any ref, so that line could
never appear and the job was red by design for 27+ hours. These contracts pin the semantics that
made it worth writing properly: lag is measured against each source's OWN newest meter row (an idle
source is not a stale store), an uninstalled source is absent rather than breached, and an unreadable
store is its own verdict.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes_freshness as fresh  # noqa: E402

NOW = 1_800_000_000.0


def _store(tmp_path, rows):
    p = tmp_path / 'outcomes.jsonl'
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return str(p)


def _src(ts, why='ok'):
    return lambda: (ts, why)


def test_a_current_store_is_ok_and_prints_the_greppable_prefix(tmp_path):
    store = _store(tmp_path, [{'source_system': 'hermes', 'ts': NOW - 60}])
    r = fresh.check(store_path=store, budget_h=6.0, now_s=NOW,
                    sources={'hermes': _src(NOW - 120)})
    assert r['verdict'] == 'ok' and r['present'] == ['hermes']


def test_an_idle_source_is_not_a_stale_store(tmp_path):
    """The measured case: pi's newest row was 157h old because pi was idle, not because the
    import froze. Lag is source-relative, so this must be OK."""
    store = _store(tmp_path, [{'source_system': 'pi', 'ts': NOW - 157 * 3600}])
    r = fresh.check(store_path=store, budget_h=6.0, now_s=NOW,
                    sources={'pi': _src(NOW - 157 * 3600 - 60)})
    assert r['verdict'] == 'ok', r


def test_a_frozen_import_breaches_and_names_the_source(tmp_path):
    store = _store(tmp_path, [{'source_system': 'hermes', 'ts': NOW - 40 * 3600}])
    r = fresh.check(store_path=store, budget_h=6.0, now_s=NOW,
                    sources={'hermes': _src(NOW - 60)})
    assert r['verdict'] == 'breach'
    assert r['breaches'][0][0] == 'hermes' and 'behind' in r['breaches'][0][1]


def test_a_present_source_with_no_store_rows_breaches(tmp_path):
    store = _store(tmp_path, [{'source_system': 'pi', 'ts': NOW - 60}])
    r = fresh.check(store_path=store, budget_h=6.0, now_s=NOW,
                    sources={'hermes': _src(NOW - 60)})
    assert r['verdict'] == 'breach'
    assert 'NO hermes row' in ' '.join(r['detail'])


def test_an_uninstalled_source_is_absent_not_breached(tmp_path):
    store = _store(tmp_path, [{'source_system': 'hermes', 'ts': NOW - 60}])
    r = fresh.check(store_path=store, budget_h=6.0, now_s=NOW,
                    sources={'hermes': _src(NOW - 60), 'openclaw': _src(None, 'absent')})
    assert r['verdict'] == 'ok'
    assert r['absent'][0][0] == 'openclaw'


def test_an_unreadable_store_is_its_own_verdict(tmp_path):
    r = fresh.check(store_path=str(tmp_path / 'nope.jsonl'), budget_h=6.0, now_s=NOW,
                    sources={'hermes': _src(NOW)})
    assert r['verdict'] == 'unreadable'


def test_main_prints_the_contract_line(tmp_path, capsys, monkeypatch):
    store = _store(tmp_path, [{'source_system': 'hermes', 'ts': NOW - 60}])
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', store)
    monkeypatch.setattr(fresh, 'SOURCES', {'hermes': _src(NOW - 60)})
    monkeypatch.setattr(fresh.time, 'time', lambda: NOW)
    rc = fresh.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert any(l.startswith('OK: every present source') for l in out.splitlines()), out
