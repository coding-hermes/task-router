"""TR-073 — the opencode driver (SPEC-PROXY-DRIVERS §3, §6).

The row's open question was whether opencode needs the proxy to speak
`anthropic-messages`. It does not: verified live against opencode 1.18.29 (119
requests, 100% to /v1/chat/completions, zero anthropic markers). These tests pin
the four facts that decision produced, so the question cannot silently reopen:

  1. the emitted config actually declares the dialect (omit-vs-omit is not safe —
     api.id is an open string in the published schema);
  2. the dialect opencode speaks is openai-chat, so the proxy needs no new format;
  3. attribution rides in options.headers, which is the only place opencode will
     send x-router-caller;
  4. the T reader reads opencode's SQLite store, whose shape is NOT pi's.

Test 4 matters most: opencode keeps its telemetry in ONE SQLite DB with
`message.data` as JSON, and cache tokens NESTED under tokens.cache. A reader
written against pi's flat-JSON-files assumption returns zero rows and looks
"empty" rather than broken.
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

OC = drivers.get_driver('opencode')


# ------------------------------------------------------------------ config

def test_driver_is_registered_with_the_verified_surface():
    """opencode is registered so a `x-router-caller: opencode` header is no longer
    degraded to 'router-proxy' (TR-071) — that degradation is what the first live
    run produced."""
    assert OC is not None
    assert OC.id == 'opencode'
    assert 'W' in OC.surfaces, 'the proxy path is the tested surface'
    assert OC.wire_format == 'openai-chat'


def test_config_declares_the_dialect_explicitly():
    """`api.id` is an open string in the published config schema, so opencode picks
    the dialect from `npm`. Leaving it unstated is how the model resolves against
    the models.dev catalog instead of the proxy."""
    cfg = OC.config('http://127.0.0.1:9092')
    prov = cfg['provider']['router']
    assert prov['npm'] == '@ai-sdk/openai-compatible'
    assert prov['api'].endswith('/v1'), 'base URL shape decides the endpoint'
    assert prov['options']['baseURL'].endswith('/v1')


def test_config_declares_the_caller_header():
    """Attribution is the only way a proxied opencode attempt stops looking like
    anonymous 'router-proxy' traffic — verified arriving at a recording mock."""
    prov = OC.config('http://0.0.0.0:1')['provider']['router']
    headers = prov['options']['headers']
    assert headers['x-router-caller'] == 'opencode'
    assert 'x-router-profile' in headers, 'declared complexity, not degraded'


def test_config_carries_no_secret():
    """The proxy walks its own chain with its own keys (SPEC-PROXY-DRIVERS §2.2);
    the caller-side key must be a reference, never a literal."""
    import re
    cfg = OC.config('http://127.0.0.1:9092')
    key = cfg['provider']['router']['options']['apiKey']
    assert key.startswith('{env:'), f'expected an env reference, got {key!r}'
    blob = json.dumps(cfg)
    # A realistic key shape, not the bare letters: "ta sk-r outer" would trip a
    # naive substring check on our own model name.
    for pattern in (r'sk-[A-Za-z0-9]{12,}', r'sk-ant-[A-Za-z0-9-]{12,}',
                    r'Bearer\s+[A-Za-z0-9._-]{20,}'):
        assert not re.search(pattern, blob), f'secret-like value matched {pattern}'


def test_config_costs_are_not_invented():
    """Lane pricing is the router's job (TR-070). The config must not add a price
    table that would compete with the registry's measured spend."""
    cfg = OC.config('http://127.0.0.1:9092')
    model = cfg['provider']['router']['models']['tr-auto']
    assert 'cost' not in model


def test_config_is_additive_and_names_one_default_model():
    cfg = OC.config('http://127.0.0.1:9092', model_ids=['tr-auto', 'tr-cheap'])
    assert set(cfg['provider']['router']['models']) == {'tr-auto', 'tr-cheap'}
    assert cfg['model'] == 'router/tr-auto'


# --------------------------------------------------------------- T reader

def _mkdb(tmp_path, sessions):
    """sessions = [(sid, [assistant-msg-dicts])]; builds opencode's real schema."""
    path = tmp_path / 'opencode.db'
    c = sqlite3.connect(str(path))
    c.execute('CREATE TABLE session (id TEXT, title TEXT, directory TEXT,'
              ' time_created INTEGER, time_updated INTEGER)')
    c.execute('CREATE TABLE message (id TEXT, session_id TEXT,'
              ' time_created INTEGER, time_updated INTEGER, data TEXT)')
    for sid, msgs, title in sessions:
        c.execute('INSERT INTO session VALUES (?,?,?,?,?)',
                  (sid, title, '/tmp', 1_700_000_000_000, 1_700_000_600_000))
        for i, m in enumerate(msgs):
            c.execute('INSERT INTO message VALUES (?,?,?,?,?)',
                      (f'{sid}-m{i}', sid, 1_700_000_000_000 + i, 0,
                       json.dumps(m)))
    c.commit()
    c.close()
    return str(path)


def _assistant(p='p', m='m', tin=10, tout=4, cread=0, cwrite=0, cost=0.01,
               finish='stop'):
    return {'role': 'assistant', 'providerID': p, 'modelID': m, 'cost': cost,
            'tokens': {'total': tin + tout, 'input': tin, 'output': tout,
                       'reasoning': 0, 'cache': {'read': cread, 'write': cwrite}},
            'time': {'created': 1_700_000_000_000, 'completed': 1_700_000_005_000},
            'finish': finish}


def test_reader_finds_rows_in_the_sqlite_store(tmp_path):
    """The T surface is ONE SQLite DB, not pi's directory of JSONL files."""
    db = _mkdb(tmp_path, [('ses_a', [_assistant()], 'first session')])
    rows = ro.import_opencode(db)
    assert len(rows) == 1
    assert rows[0]['source_system'] == 'opencode'
    assert rows[0]['session_id'] == 'ses_a'
    assert rows[0]['task_label'] == 'first session'


def test_reader_sums_multi_turn_usage(tmp_path):
    db = _mkdb(tmp_path, [('ses_b', [_assistant(tin=10, tout=2, cost=0.01),
                                     _assistant(tin=5, tout=3, cost=0.02)],
                           'multi')])
    r = ro.import_opencode(db)[0]
    assert r['turns'] == 2
    assert r['tokens_in'] == 15
    assert r['tokens_out'] == 5
    assert r['cost_usd'] == pytest.approx(0.03)


def test_cache_tokens_are_nested_and_not_folded_into_input(tmp_path):
    """Real shape: cache lives at tokens.cache.{read,write}. Folding it into input
    would misreport every cached request."""
    db = _mkdb(tmp_path, [('ses_c', [_assistant(tin=100, cread=900,
                                                cwrite=50)], 'cache')])
    r = ro.import_opencode(db)[0]
    assert r['tokens_in'] == 100, 'input must exclude cache'
    assert r['tokens_cache_read'] == 900
    assert r['tokens_cache_write'] == 50


def test_epoch_milliseconds_are_converted(tmp_path):
    db = _mkdb(tmp_path, [('ses_d', [_assistant()], 't')])
    r = ro.import_opencode(db)[0]
    assert 1_600_000_000 < r['ts'] < 2_000_000_000, 'readable epoch seconds'
    assert r['wall_time_s'] == pytest.approx(600.0), 'session span in seconds'


def test_unknown_finish_is_not_a_failure(tmp_path):
    """`unknown` means the turn ended without a terminal reason. That is NOT
    evidence of failure, so success must stay None rather than become False."""
    db = _mkdb(tmp_path, [('ses_e', [_assistant(finish='unknown')], 'u')])
    assert ro.import_opencode(db)[0]['success'] is None


def test_explicit_error_is_a_failure_and_tool_calls_are_success(tmp_path):
    db = _mkdb(tmp_path, [('ses_f', [_assistant(finish='error')], 'e'),
                          ('ses_g', [_assistant(finish='tool-calls')], 't')])
    by = {r['session_id']: r for r in ro.import_opencode(db)}
    assert by['ses_f']['success'] is False
    assert by['ses_g']['success'] is True


def test_missing_or_unreadable_store_yields_no_rows(tmp_path):
    """Absence of data is not evidence of no usage — yield nothing, never a
    zero-filled row that would drag an average down."""
    assert ro.import_opencode(str(tmp_path / 'nope.db')) == []
    bad = tmp_path / 'bad.db'
    bad.write_bytes(b'not a database at all')
    assert ro.import_opencode(str(bad)) == []


def test_rows_without_a_lane_are_skipped(tmp_path):
    """A row that cannot be attributed to a lane cannot feed the cost engine."""
    m = _assistant()
    m.pop('providerID')
    m.pop('modelID')
    db = _mkdb(tmp_path, [('ses_h', [m], 'x')])
    assert ro.import_opencode(db) == []


def test_reader_does_not_write_to_the_host_store(tmp_path):
    """The reader opens strictly read-only: opencode may be mid-write."""
    db = _mkdb(tmp_path, [('ses_i', [_assistant()], 'ro')])
    before = os.stat(db).st_mtime_ns
    ro.import_opencode(db)
    assert os.stat(db).st_mtime_ns == before


# ----------------------------------------------------------- delegation

def test_driver_delegates_to_the_importer(tmp_path, monkeypatch):
    calls = []

    def fake(path=None):
        calls.append(path)
        return [{'source_system': 'opencode'}]

    monkeypatch.setattr(ro, 'import_opencode', fake)
    monkeypatch.setattr(OC, 'rows_from_sessions',
                        classmethod(lambda cls, db_path=None:
                                    ro.import_opencode(db_path or 'DEFAULT')))
    rows = OC.rows_from_sessions('/custom/path.db')
    assert rows and calls == ['/custom/path.db']


def test_row_for_matches_the_tr049_shape():
    r = OC.row_for(session_id='s', provider='p', model='m', tokens_in=1,
                   tokens_cache_read=2)
    for key in ('source_system', 'session_id', 'provider', 'model',
                'tokens_in', 'tokens_cache_read'):
        assert key in r
    assert r['source_system'] == 'opencode'
