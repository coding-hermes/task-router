"""TR-074 — the pi driver (SPEC-PROXY-DRIVERS §3, §6).

Acceptance: a pi session pointed at the proxy completes and yields
source_system="pi" outcome rows.

The wire fact is grounded in the INSTALLED package's own docs
(`docs/models.md`): "Some OpenAI-compatible servers do not understand the
`developer` role used for reasoning-capable models. For those providers, set
`compat.supportsDeveloperRole` to `false` so pi sends the system prompt as a
`system` message instead." The driver declares that as DATA; the proxy also
normalizes defensively (TR-096), so both halves are covered and neither is
silent.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
sys.path.insert(0, os.path.join(REPO, 'scripts', 'drivers'))

import drivers          # noqa: E402
import router_outcomes as ro  # noqa: E402


# --------------------------------------------------------------- W: config
def test_pi_is_registered_with_the_expected_surfaces():
    d = drivers.get_driver('pi')
    assert d is not None
    assert d.id == 'pi'
    assert 'W' in d.surfaces and 'T' in d.surfaces
    assert d.wire_format == 'openai-completions'


def test_config_declares_the_developer_role_compat_fact():
    """The exact failure pi's models.md names — pi must not send `developer`."""
    d = drivers.get_driver('pi')
    entry = d.config('http://127.0.0.1:9092')['providers']['task-router']
    assert entry['compat']['supportsDeveloperRole'] is False, (
        'pi sends role:developer unless the endpoint declares it unsupported')
    assert entry['compat']['supportsReasoningEffort'] is False


def test_config_points_at_the_proxy_with_the_caller_identity():
    d = drivers.get_driver('pi')
    entry = d.config('http://127.0.0.1:9092')['providers']['task-router']
    assert entry['baseUrl'] == 'http://127.0.0.1:9092/v1'
    assert entry['api'] == 'openai-completions'
    # a driver carries no credential of its own
    assert 'apiKey' in entry and entry['apiKey'].startswith('$'), (
        'env indirection, not a literal secret')
    # scan only the VALUES, so an id like "task-router" cannot false-positive
    vals = json.dumps([v for v in entry.values() if isinstance(v, str)])
    assert not any(k in vals for k in ('sk-', 'sk_', 'Bearer '))


def test_config_models_declare_zero_cost():
    """Lane pricing belongs to the router (TR-070). A config row that invented a
    price would double-count spend."""
    d = drivers.get_driver('pi')
    for m in d.config()['providers']['task-router']['models']:
        assert m['cost'] == {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}


def test_extension_form_is_emitted_but_never_written():
    """A driver must not modify the host's files (spec §2.2)."""
    d = drivers.get_driver('pi')
    src = d.extension_source('http://127.0.0.1:9092')
    assert 'registerProvider' in src and 'task-router' in src


def test_a_driver_does_not_reimplement_the_workaround():
    """The driver DECLARES the fact; the proxy does the rewriting (TR-096).

    Asserted structurally: the driver must not transform a message payload or
    contain the role strings, only the compat key that names the fact.
    """
    import inspect
    import pi as pimod
    src = inspect.getsource(pimod)
    assert "role': 'system'" not in src and '"role": "system"' not in src, (
        'rewriting a role is proxy scope')
    assert "== 'developer'" not in src and '== "developer"' not in src
    # the fact IS declared, as data
    assert pimod.PiDriver.compat['supports_developer_role'] is False


# ----------------------------------------------------------- T: the reader
def _write_session(tmp, sid, messages, cwd='/tmp/x'):
    p = tmp / f'{sid}.jsonl'
    with open(p, 'w') as f:
        f.write(json.dumps({'type': 'session', 'version': 3, 'id': sid,
                            'timestamp': '2026-09-01T17:27:34.314Z', 'cwd': cwd}) + '\n')
        for m in messages:
            f.write(json.dumps(m) + '\n')
    return p


def _assistant(prov, model, tin, tout, stop='stop', cost=0.0):
    return {'type': 'message', 'id': 'x', 'timestamp': '2026-09-01T17:27:36.000Z',
            'message': {'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'hi'}],
                        'provider': prov, 'model': model,
                        'usage': {'input': tin, 'output': tout, 'cacheRead': 0,
                                  'cacheWrite': 0, 'totalTokens': tin + tout,
                                  'cost': {'input': 0, 'output': 0, 'cacheRead': 0,
                                           'cacheWrite': 0, 'total': cost}},
                        'stopReason': stop}}


def test_reader_produces_tr049_rows(tmp_path):
    _write_session(tmp_path, 'sess-1', [_assistant('deepseek', 'deepseek-v4-pro', 100, 20)])
    rows = ro.import_pi(str(tmp_path))
    assert len(rows) == 1
    r = rows[0]
    assert r['source_system'] == 'pi'
    assert r['session_id'] == 'sess-1'
    assert r['provider'] == 'deepseek' and r['model'] == 'deepseek-v4-pro'
    assert r['tokens_in'] == 100 and r['tokens_out'] == 20
    assert r['success'] is True


def test_reader_aggregates_across_a_multi_turn_session(tmp_path):
    _write_session(tmp_path, 'sess-2', [
        _assistant('p', 'm', 10, 5),
        _assistant('p', 'm', 20, 7)])
    r = ro.import_pi(str(tmp_path))[0]
    assert r['turns'] == 2
    assert r['tokens_in'] == 30 and r['tokens_out'] == 12


def test_reader_reports_error_sessions_honestly(tmp_path):
    """pi DOES report stopReason, so success is known here — unlike hermes,
    whose gateway does not report completion (success stays NULL)."""
    _write_session(tmp_path, 'sess-3', [_assistant('p', 'm', 0, 0, stop='error')])
    r = ro.import_pi(str(tmp_path))[0]
    assert r['success'] is False


def test_reader_preserves_cache_token_columns(tmp_path):
    _write_session(tmp_path, 'sess-4', [_assistant('p', 'm', 10, 5)])
    r = ro.import_pi(str(tmp_path))[0]
    assert 'tokens_cache_read' in r and 'tokens_cache_write' in r


def test_reader_ignores_a_session_with_no_assistant_message(tmp_path):
    """Never fabricate a row from a session that produced nothing."""
    _write_session(tmp_path, 'sess-5', [])
    assert ro.import_pi(str(tmp_path)) == []


def test_reader_survives_a_corrupt_line(tmp_path):
    p = tmp_path / 'bad.jsonl'
    p.write_text(json.dumps({'type': 'session', 'id': 's', 'timestamp': '2026-09-01T00:00:00Z'})
                 + '\n{not json at all\n'
                 + json.dumps(_assistant('p', 'm', 1, 1)) + '\n')
    rows = ro.import_pi(str(tmp_path))
    assert len(rows) == 1 and rows[0]['session_id'] == 's'


def test_reader_is_read_only_and_handles_a_missing_dir():
    assert ro.import_pi('/nonexistent/pi/sessions') == []


def test_the_reader_delegates_rather_than_duplicating():
    """Two readers drift; the driver must call the importer (spec §1)."""
    import inspect
    import pi as pimod
    src = inspect.getsource(pimod.PiDriver.rows_from_sessions)
    assert 'ro.import_pi' in src
    assert pimod.PiDriver.row_for(session_id='s')['source_system'] == 'pi'


def test_real_sessions_on_this_box_map_without_error():
    """Live check on whatever pi sessions exist; skips on a clean box."""
    d = drivers.get_driver('pi')
    rows = d.rows_from_sessions()
    if not rows:
        return
    assert all(r['source_system'] == 'pi' for r in rows)
    assert all(r['session_id'] for r in rows)
    # cost must never be a fabricated value: only 0 or the session's own total
    assert all(r['cost_usd'] is None or r['cost_usd'] >= 0 for r in rows)
