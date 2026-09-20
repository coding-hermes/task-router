"""TR-072 — the openclaw driver (SPEC-PROXY-DRIVERS §3, §6).

The row's wire fact was verified against OpenClaw's OWN shipped documentation in
the installed package (docs/gateway/config-tools/custom-providers.md), which
states: "A custom provider with `baseUrl` but no `api` defaults to
`openai-completions`". Two consequences these tests pin:

  1. the dialect must still be DECLARED, because relying on an implicit default is
     what this driver exists to prevent;
  2. the caller header must be emitted, or the outcome row lands as
     'router-proxy' — the first live run did exactly that, on the same path that
     produced 'openclaw' once `models.providers.*.headers` was set.

The T surface is a per-agent SQLite DB with a schema that shares nothing with
opencode's, so the reader cannot be shared. These tests pin the shape (flat
`usage.cacheRead`, lane from `message.provider`, `stopReason` success) that was
measured on a real run.
"""
import json
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
sys.path.insert(0, os.path.join(REPO, 'scripts', 'drivers'))

import drivers          # noqa: E402
import router_outcomes as ro   # noqa: E402

OC = drivers.get_driver('openclaw')


# ------------------------------------------------------------------ config

def test_driver_is_registered():
    assert OC is not None
    assert OC.id == 'openclaw'
    assert 'W' in OC.surfaces
    assert OC.wire_format == 'openai-completions', 'the shipped default for a baseUrl route'


def test_config_declares_the_api_explicitly():
    """The default happens to be right, but an implicit default is not a contract."""
    frag = OC.config('http://127.0.0.1:9092')
    prov = frag['models']['providers']['task-router']
    assert prov['api'] == 'openai-completions'
    assert prov['baseUrl'].endswith('/v1')


def test_config_emits_the_caller_header():
    """Without this the row is stamped 'router-proxy' (TR-071), which is what the
    first live run produced on the same path that later produced 'openclaw'."""
    prov = OC.config('http://127.0.0.1:9092')['models']['providers']['task-router']
    assert prov['headers']['x-router-caller'] == 'openclaw'
    assert 'x-router-profile' in prov['headers']


def test_config_merges_rather_than_replacing():
    """The docs warn `config set` refuses destructive replacements; a driver must
    never clobber a user's other providers."""
    frag = OC.config('http://127.0.0.1:9092')
    assert frag['models']['mode'] == 'merge'
    assert list(frag['models']['providers']) == ['task-router']


def test_config_uses_a_credential_reference_and_no_cost_table():
    frag = OC.config('http://127.0.0.1:9092')
    prov = frag['models']['providers']['task-router']
    assert prov['apiKey'] == 'ROUTER_PROXY_KEY', 'a reference, not a secret'
    for m in prov['models']:
        assert m['cost'] == {'input': 0, 'output': 0, 'cacheRead': 0,
                             'cacheWrite': 0}, 'never invent lane pricing (TR-070)'


def test_config_declares_only_the_verified_compat_flag():
    """The docs are explicit that catalog compat flags must NOT be copied, and
    that a compat block is only for a genuinely custom route with endpoint-verified
    keys. The one fact verifiable here is that the proxy accepts `developer`
    (TR-096 normalizes it)."""
    assert OC.compat == {'supports_developer_role': True}
    frag = OC.config('http://127.0.0.1:9092')
    assert 'compat' not in frag['models']['providers']['task-router']


# --------------------------------------------------------------- T reader

def _mkstate(tmp_path, agents):
    """agents = {agent_id: [(session_id, cwd, [assistant msgs])]}"""
    base = tmp_path / 'state'
    for aid, sessions in agents.items():
        d = base / 'agents' / aid / 'agent'
        d.mkdir(parents=True)
        c = sqlite3.connect(str(d / 'openclaw-agent.sqlite'))
        c.execute('CREATE TABLE transcript_events (session_id TEXT, seq INTEGER,'
                  ' event_json TEXT, created_at TEXT)')
        seq = 0
        for sid, cwd, msgs in sessions:
            c.execute('INSERT INTO transcript_events VALUES (?,?,?,?)',
                      (sid, seq, json.dumps({'type': 'session', 'id': sid,
                                             'cwd': cwd}), 'now'))
            seq += 1
            for m in msgs:
                c.execute('INSERT INTO transcript_events VALUES (?,?,?,?)',
                          (sid, seq, json.dumps({'type': 'message',
                                                 'message': m}), 'now'))
                seq += 1
        c.commit()
        c.close()
    return str(base)


def _asst(p='task-router', m='tr-auto', tin=14, tout=5, cread=0, cwrite=0,
          cost=0.0, stop='stop', ts=1789881685452):
    return {'role': 'assistant', 'api': 'openai-completions', 'provider': p,
            'model': m, 'stopReason': stop, 'timestamp': ts,
            'usage': {'input': tin, 'output': tout, 'cacheRead': cread,
                      'cacheWrite': cwrite, 'totalTokens': tin + tout,
                      'cost': {'total': cost}}}


def test_reader_reads_the_agent_sqlite_store(tmp_path):
    base = _mkstate(tmp_path, {'main': [('s1', '/work', [_asst()])]})
    rows = ro.import_openclaw(base)
    assert len(rows) == 1
    assert rows[0]['source_system'] == 'openclaw'
    assert rows[0]['session_id'] == 's1'
    assert rows[0]['task_label'] == '/work'


def test_reader_sums_turns_and_flat_cache_fields(tmp_path):
    """openclaw's cache is FLAT (usage.cacheRead) unlike opencode's nested
    tokens.cache.read — the readers must not be shared."""
    base = _mkstate(tmp_path, {'main': [('s1', '/w', [
        _asst(tin=10, tout=2, cread=100, cwrite=5),
        _asst(tin=4, tout=3, cread=50, cwrite=1)])]})
    r = ro.import_openclaw(base)[0]
    assert r['turns'] == 2
    assert r['tokens_in'] == 14
    assert r['tokens_out'] == 5
    assert r['tokens_cache_read'] == 150
    assert r['tokens_cache_write'] == 6


def test_lane_comes_from_the_configured_route_not_the_response(tmp_path):
    """`responseModel` is whatever the upstream answered; the row must record the
    CONFIGURED lane so it can be priced."""
    m = _asst(p='task-router', m='tr-auto')
    m['responseModel'] = 'k3'
    base = _mkstate(tmp_path, {'main': [('s1', '/w', [m])]})
    r = ro.import_openclaw(base)[0]
    assert r['provider'] == 'task-router'
    assert r['model'] == 'tr-auto'


def test_success_from_stop_reason(tmp_path):
    base = _mkstate(tmp_path, {'main': [
        ('ok', '/w', [_asst(stop='stop')]),
        ('tool', '/w', [_asst(stop='toolUse')]),
        ('bad', '/w', [_asst(stop='error')])]})
    by = {r['session_id']: r for r in ro.import_openclaw(base)}
    assert by['ok']['success'] is True
    assert by['tool']['success'] is True
    assert by['bad']['success'] is False


def test_missing_stop_reason_stays_unknown(tmp_path):
    m = _asst()
    m.pop('stopReason')
    base = _mkstate(tmp_path, {'main': [('s1', '/w', [m])]})
    assert ro.import_openclaw(base)[0]['success'] is None


def test_all_agent_dbs_are_scanned(tmp_path):
    """A state dir can hold several agents; reading only the first under-reports."""
    base = _mkstate(tmp_path, {'main': [('s1', '/w', [_asst()])],
                               'second': [('s2', '/w', [_asst()])]})
    sids = {r['session_id'] for r in ro.import_openclaw(base)}
    assert sids == {'s1', 's2'}


def test_missing_or_corrupt_store_yields_no_rows(tmp_path):
    assert ro.import_openclaw(str(tmp_path / 'nope')) == []
    base = tmp_path / 'state' / 'agents' / 'main' / 'agent'
    base.mkdir(parents=True)
    (base / 'openclaw-agent.sqlite').write_bytes(b'not a database')
    assert ro.import_openclaw(str(tmp_path / 'state')) == []


def test_rows_without_a_lane_are_skipped(tmp_path):
    m = _asst()
    m.pop('provider')
    m.pop('model')
    base = _mkstate(tmp_path, {'main': [('s1', '/w', [m])]})
    assert ro.import_openclaw(base) == []


def test_reader_does_not_write_to_the_host_store(tmp_path):
    base = _mkstate(tmp_path, {'main': [('s1', '/w', [_asst()])]})
    db = os.path.join(base, 'agents', 'main', 'agent', 'openclaw-agent.sqlite')
    before = os.stat(db).st_mtime_ns
    ro.import_openclaw(base)
    assert os.stat(db).st_mtime_ns == before


def test_row_for_matches_the_tr049_shape():
    r = OC.row_for(session_id='s', provider='p', model='m')
    for key in ('source_system', 'session_id', 'provider', 'model', 'turns'):
        assert key in r
    assert r['source_system'] == 'openclaw'
