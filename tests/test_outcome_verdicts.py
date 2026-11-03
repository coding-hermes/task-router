"""TR-357 M1 unit tests: tick-derived success verdicts.

Covers the board acceptance for milestone 1 — every CONCLUDED scheduler tick
writes a non-null success verdict into the outcome store so known-n grows:
  (a) concluded tick, commits > 0   -> success=true
  (b) concluded tick, commits == 0  -> success=false
  (c) unconcluded tick              -> success=null WITH a reason (DATA > CODE)
  (d) idempotent re-import          -> no flips, no duplicates, byte-identical
  (e) unreadable scheduler DB       -> fail-open, success stays null
plus the join (session-key <-> UUID bridge), TR-299 merge integration,
and the CLI wiring (one `import-hermes` command leaves the store verdicted).
No network, no live DB — every fixture is hermetic (a synthetic scheduler.db,
gateway state.db and outcome store in tmp_path).
"""
import glob
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro  # noqa: E402

# ---------------------------------------------------------------------------
# fixtures: hermetic scheduler DB, gateway DB and outcome store
# ---------------------------------------------------------------------------

def _tick(tid='t1', project='task-router', session='sess-key-1',
          status='completed', outcome='ok', spawned_at='2026-11-01T00:00:00Z',
          completed_at='2026-11-01T00:05:00Z', commits=1, code_commits=1,
          board_commits=0):
    return (tid, project, session, status, outcome, spawned_at, completed_at,
            commits, code_commits, board_commits)


def _make_sched_db(tmp_path, ticks, name='scheduler.db'):
    p = tmp_path / name
    db = sqlite3.connect(str(p))
    db.execute(
        'CREATE TABLE ticks (id TEXT PRIMARY KEY, project_name TEXT,'
        ' session_id TEXT, status TEXT, outcome TEXT, spawned_at TEXT,'
        ' completed_at TEXT, commits INTEGER, code_commits INTEGER,'
        ' board_commits INTEGER)')
    for t in ticks:
        db.execute(
            'INSERT INTO ticks (id, project_name, session_id, status,'
            ' outcome, spawned_at, completed_at, commits, code_commits,'
            ' board_commits) VALUES (?,?,?,?,?,?,?,?,?,?)', t)
    db.commit()
    db.close()
    return str(p)


def _make_gateway_db(tmp_path, sessions=(), usage=(), name='state.db'):
    p = tmp_path / name
    db = sqlite3.connect(str(p))
    db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, session_key TEXT,'
               ' display_name TEXT)')
    db.execute(
        'CREATE TABLE session_model_usage (session_id TEXT, model TEXT,'
        ' billing_provider TEXT, billing_base_url TEXT, task TEXT,'
        ' api_call_count INTEGER, input_tokens INTEGER,'
        ' cache_read_tokens INTEGER, output_tokens INTEGER,'
        ' reasoning_tokens INTEGER, estimated_cost_usd REAL,'
        ' first_seen REAL, last_seen REAL)')
    for s in sessions:
        db.execute('INSERT INTO sessions VALUES (?,?,?)', s)
    for u in usage:
        db.execute(
            'INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?'
            ',?,?,?)', u)
    db.commit()
    db.close()
    return str(p)


def _usage(sid, model='m-chief', t1=1_800_000_000.0):
    return (sid, model, 'stamped-lane', 'https://api.xkiro.com/v1/', '', 3,
            1000, 0, 100, 0, 0.05, t1 - 60.0, t1)


def _store_row(session_id, model='m-chief', success=None, source='hermes'):
    return {'source_system': source, 'session_id': session_id,
            'task_label': 'tick work', 'complexity': None,
            'profile_id': 'P2_AGENTIC', 'required_categories': None,
            'provider': 'xkiro', 'model': model, 'turns': 3,
            'tokens_in': 1000, 'tokens_out': 100, 'tokens_reasoning': 0,
            'cost_usd': 0.05, 'price_basis': None, 'wall_time_s': 60.0,
            'success': success, 'ts': 1_800_000_000.0, 'steps': 1}


def _write_store(tmp_path, rows, name='outcomes.jsonl'):
    p = tmp_path / name
    with open(p, 'w') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    return str(p)


def _read_store(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _route_env(monkeypatch, sched, gateway, store):
    monkeypatch.setenv('HERMES_SCHEDULER_DB', sched)
    monkeypatch.setenv('HERMES_STATE_DB', gateway)
    monkeypatch.setenv('ROUTING_OUTCOMES_FILE', store)


def _one(verdicts, key):
    assert key in verdicts, f'no verdict for {key}: {verdicts}'
    return verdicts[key]


# ---------------------------------------------------------------------------
# _tick_verdicts: the derivation itself
# ---------------------------------------------------------------------------

def test_concluded_tick_with_commits_is_a_true_verdict(tmp_path, monkeypatch):
    _route_env(monkeypatch, _make_sched_db(tmp_path, [_tick(commits=2)]),
               str(tmp_path / 'unused.db'), str(tmp_path / 'o.jsonl'))
    verdicts, _ = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert _one(verdicts, 'sess-key-1') == (True, 'tick_commits_observed')


def test_concluded_tick_zero_commits_is_a_false_verdict(tmp_path, monkeypatch):
    _route_env(monkeypatch,
               _make_sched_db(tmp_path, [_tick(commits=0)]),
               str(tmp_path / 'unused.db'), str(tmp_path / 'o.jsonl'))
    verdicts, _ = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert _one(verdicts, 'sess-key-1') == (False, 'tick_concluded_no_commits')


def test_null_commits_coalesces_to_zero(tmp_path, monkeypatch):
    """Defensive: a NULL commits column on a concluded tick is 'nothing
    recorded' = 0 commits = failure, never an exception."""
    _route_env(monkeypatch,
               _make_sched_db(tmp_path, [_tick(commits=None)]),
               str(tmp_path / 'unused.db'), str(tmp_path / 'o.jsonl'))
    verdicts, _ = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert _one(verdicts, 'sess-key-1') == (False, 'tick_concluded_no_commits')


def test_unconcluded_tick_yields_a_reasoned_null(tmp_path, monkeypatch):
    _route_env(monkeypatch,
               _make_sched_db(tmp_path, [_tick(status='running',
                                               completed_at=None)]),
               str(tmp_path / 'unused.db'), str(tmp_path / 'o.jsonl'))
    verdicts, _ = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert _one(verdicts, 'sess-key-1') == (None, 'tick_not_concluded')


def test_every_terminal_status_concludes(tmp_path, monkeypatch):
    ticks = [_tick(tid=f't{i}', session=f's{i}', status=status,
                   completed_at=None)
             for i, status in enumerate(('completed', 'failed', 'timeout',
                                         'deferred'))]
    ticks.append(_tick(tid='t-completed-at', session='s-via-time',
                       status='running', completed_at='2026-11-01T00:05:00Z'))
    _route_env(monkeypatch, _make_sched_db(tmp_path, ticks),
               str(tmp_path / 'unused.db'), str(tmp_path / 'o.jsonl'))
    verdicts, _ = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert len(verdicts) == 5
    assert all(verdicts[f's{i}'][0] is True for i in range(4))
    assert verdicts['s-via-time'][0] is True


def test_unreadable_scheduler_db_fails_open(tmp_path, monkeypatch, capsys):
    """(e): a corrupt scheduler DB is one stderr line and an empty map —
    never an exception, never a fabricated verdict."""
    corrupt = tmp_path / 'scheduler.db'
    corrupt.write_bytes(b'this is not a sqlite database')
    _route_env(monkeypatch, str(corrupt), str(tmp_path / 'unused.db'),
               str(tmp_path / 'o.jsonl'))
    verdicts, status = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert verdicts == {} and status == 'unreadable'
    assert 'unreadable' in capsys.readouterr().err


def test_missing_scheduler_db_reports_missing(tmp_path, monkeypatch):
    _route_env(monkeypatch, str(tmp_path / 'nope.db'),
               str(tmp_path / 'unused.db'), str(tmp_path / 'o.jsonl'))
    verdicts, status = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert verdicts == {} and status == 'missing'


def test_uuid_bridge_attributes_historical_rows(tmp_path, monkeypatch):
    """The tick knows the session KEY; the store row may carry the gateway
    UUID — the sessions table bridges both to the same verdict."""
    sched = _make_sched_db(tmp_path, [_tick(session='sess-key-1')])
    gateway = _make_gateway_db(tmp_path,
                               sessions=[('uuid-1', 'sess-key-1', 'w1')])
    _route_env(monkeypatch, sched, gateway, str(tmp_path / 'o.jsonl'))
    verdicts, status = ro._tick_verdicts(gateway)
    assert status == 'ok'
    assert verdicts['uuid-1'] == verdicts['sess-key-1'] \
        == (True, 'tick_commits_observed')


def test_bridge_down_names_itself_instead_of_misattributing(
        tmp_path, monkeypatch):
    """Scheduler readable but the gateway DB gone: key-identity rows still
    stamp (partial attribution), and a UUID row that needs the bridge stays
    null with the TRUE reason — not a bare null, not a wrong one."""
    sched = _make_sched_db(tmp_path, [_tick(session='k-x', commits=3)])
    gateway = str(tmp_path / 'gateway-gone.db')   # does not exist
    store = _write_store(tmp_path, [_store_row('uuid-x'), _store_row('k-x')])
    _route_env(monkeypatch, sched, gateway, store)
    verdicts, status = ro._tick_verdicts(gateway)
    assert status == 'ok-no-bridge'
    summary = ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    rows = {r['session_id']: r for r in _read_store(store)}
    assert rows['k-x']['success'] is True         # partial attribution works
    assert rows['uuid-x']['success'] is None
    assert rows['uuid-x']['success_reason'] == 'gateway_bridge_unreadable'
    assert summary['verdict_status'] == 'ok-no-bridge'


def test_latest_concluded_tick_wins_and_unconcluded_never_erases(
        tmp_path, monkeypatch):
    ticks = [
        # s-a: unconcluded first, concluded later -> the conclusion wins
        _tick(tid='a1', session='s-a', status='running', completed_at=None,
              spawned_at='2026-11-01T00:00:00Z'),
        _tick(tid='a2', session='s-a', status='completed', commits=3,
              spawned_at='2026-11-01T01:00:00Z'),
        # s-b: concluded first, unconcluded later -> the conclusion SURVIVES
        _tick(tid='b1', session='s-b', status='failed', commits=0,
              spawned_at='2026-11-01T00:00:00Z'),
        _tick(tid='b2', session='s-b', status='running', completed_at=None,
              spawned_at='2026-11-01T01:00:00Z'),
    ]
    _route_env(monkeypatch, _make_sched_db(tmp_path, ticks),
               str(tmp_path / 'unused.db'), str(tmp_path / 'o.jsonl'))
    verdicts, _ = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert verdicts['s-a'] == (True, 'tick_commits_observed')
    assert verdicts['s-b'] == (False, 'tick_concluded_no_commits')


def test_latest_concluded_zero_commit_tick_wins_over_earlier_success(
        tmp_path, monkeypatch):
    ticks = [
        _tick(tid='x1', session='s-x', status='completed', commits=4,
              spawned_at='2026-11-01T00:00:00Z'),
        _tick(tid='x2', session='s-x', status='timeout', commits=0,
              spawned_at='2026-11-01T02:00:00Z'),
    ]
    _route_env(monkeypatch, _make_sched_db(tmp_path, ticks),
               str(tmp_path / 'unused.db'), str(tmp_path / 'o.jsonl'))
    verdicts, _ = ro._tick_verdicts(str(tmp_path / 'unused.db'))
    assert verdicts['s-x'] == (False, 'tick_concluded_no_commits')


# ---------------------------------------------------------------------------
# apply_tick_verdicts: the store pass
# ---------------------------------------------------------------------------

def test_store_pass_stamps_true_false_and_reasoned_nulls(tmp_path,
                                                         monkeypatch):
    """(a)+(b)+(c) end to end on the store."""
    sched = _make_sched_db(tmp_path, [
        _tick(tid='t1', session='k-win', commits=2),
        _tick(tid='t2', session='k-dry', commits=0),
        _tick(tid='t3', session='k-live', status='running',
              completed_at=None),
    ])
    gateway = _make_gateway_db(tmp_path, sessions=[
        ('uuid-win', 'k-win', 'w1'), ('uuid-dry', 'k-dry', 'w2')])
    store = _write_store(tmp_path, [
        _store_row('uuid-win'), _store_row('uuid-dry'),
        _store_row('k-live'), _store_row('k-ghost')])
    _route_env(monkeypatch, sched, gateway, store)
    summary = ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    assert summary['changed'] == 2 and summary['reasons_stamped'] == 2
    rows = {r['session_id']: r for r in _read_store(store)}
    assert rows['uuid-win']['success'] is True
    assert rows['uuid-win']['success_reason'] == 'tick_commits_observed'
    assert rows['uuid-dry']['success'] is False
    assert rows['uuid-dry']['success_reason'] == 'tick_concluded_no_commits'
    # (c): unconcluded -> success stays null, but the null carries a reason
    assert rows['k-live']['success'] is None
    assert rows['k-live']['success_reason'] == 'tick_not_concluded'
    # a session no tick ever owned is reasoned too, never a bare null
    assert rows['k-ghost']['success'] is None
    assert rows['k-ghost']['success_reason'] == 'no_scheduler_tick_for_session'


def test_reimport_is_idempotent_and_byte_identical(tmp_path, monkeypatch):
    """(d): the second run settles for 'already current' and does not flip,
    duplicate or rewrite a single row."""
    sched = _make_sched_db(tmp_path, [_tick(session='k1', commits=1),
                                      _tick(tid='t2', session='k2',
                                            commits=0)])
    gateway = _make_gateway_db(tmp_path)
    store = _write_store(tmp_path, [_store_row('k1'), _store_row('k2')])
    _route_env(monkeypatch, sched, gateway, store)
    first = ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    assert first['changed'] == 2
    before = open(store, 'rb').read()
    second = ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    assert second['changed'] == 0 and second['reasons_stamped'] == 0
    assert second['already_current'] == 0  # both rows now settled
    assert second['settled_kept'] == 2
    assert second['backup'] is None        # no-op run: no snapshot pile-up
    assert open(store, 'rb').read() == before
    assert len(_read_store(store)) == 2   # no duplicates
    assert len(glob.glob(store + '.bak-verdicts-*')) == 1  # only run 1 backed up


def test_settled_verdicts_never_flip_even_when_ticks_change(
        tmp_path, monkeypatch):
    """A row that already carries an independent verdict (e.g. the verified
    acceptance leg) is sticky — a later tick reading cannot overwrite it."""
    sched = _make_sched_db(tmp_path, [_tick(session='k1', commits=0),
                                      _tick(tid='t2', session='k2',
                                            commits=5)])
    gateway = _make_gateway_db(tmp_path)
    store = _write_store(tmp_path, [_store_row('k1', success=True),
                                    _store_row('k2', success=False)])
    _route_env(monkeypatch, sched, gateway, store)
    summary = ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    assert summary['changed'] == 0 and summary['settled_kept'] == 2
    rows = {r['session_id']: r for r in _read_store(store)}
    assert rows['k1']['success'] is True and rows['k2']['success'] is False


def test_unreadable_scheduler_db_fails_open_on_the_store(
        tmp_path, monkeypatch):
    """(e): the STORE pass fails open too — no exception, success stays null,
    and the null explains itself instead of sitting bare."""
    corrupt = tmp_path / 'scheduler.db'
    corrupt.write_bytes(b'not a database at all')
    gateway = _make_gateway_db(tmp_path)
    store = _write_store(tmp_path, [_store_row('k1', success=None)])
    _route_env(monkeypatch, str(corrupt), gateway, store)
    summary = ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    assert 'aborted' not in summary and 'error' not in summary
    row = _read_store(store)[0]
    assert row['success'] is None
    assert row['success_reason'] == 'scheduler_db_unreadable'
    # and the failure path is idempotent as well
    again = ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    assert again['reasons_stamped'] == 0 and again['already_current'] == 1


def test_foreign_source_rows_are_left_verbatim(tmp_path, monkeypatch):
    """pi/opencode/openclaw rows have no scheduler tick — the pass must not
    stamp or reason them."""
    sched = _make_sched_db(tmp_path, [_tick(session='k1', commits=0)])
    gateway = _make_gateway_db(tmp_path)
    store = _write_store(tmp_path, [_store_row('k1'),
                                    _store_row('oc-1', source='opencode')])
    _route_env(monkeypatch, sched, gateway, store)
    ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    rows = {r['session_id']: r for r in _read_store(store)}
    assert rows['oc-1']['success'] is None
    assert 'success_reason' not in rows['oc-1']
    assert rows['k1']['success'] is False


def test_torn_and_nonobject_lines_are_preserved(tmp_path, monkeypatch):
    sched = _make_sched_db(tmp_path, [_tick(session='k1', commits=1)])
    gateway = _make_gateway_db(tmp_path)
    p = tmp_path / 'outcomes.jsonl'
    torn = '{"source_system": "hermes", "session_id": "tor'
    with open(p, 'w') as f:
        f.write(torn + '\n')
        f.write('[1, 2, 3]\n')            # valid JSON, not an object
        f.write(json.dumps(_store_row('k1')) + '\n')
    _route_env(monkeypatch, sched, gateway, str(p))
    summary = ro.apply_tick_verdicts(store_path=str(p), db_path=gateway)
    assert summary['changed'] == 1
    with open(p) as f:
        lines = f.read().splitlines()
    assert lines[0] == torn and lines[1] == '[1, 2, 3]'
    assert json.loads(lines[2])['success'] is True


def test_mtime_guard_aborts_on_a_live_append(tmp_path, monkeypatch):
    """The streaming rewrite aborts untouched when the store changes under it
    (same contract as backfill_hermes_profiles)."""
    sched = _make_sched_db(tmp_path, [_tick(session='k1', commits=1)])
    gateway = _make_gateway_db(tmp_path)
    store = _write_store(tmp_path, [_store_row('k1')])
    _route_env(monkeypatch, sched, gateway, store)
    real_getmtime = os.path.getmtime
    state = {'calls': 0}

    def shaky_getmtime(path):
        state['calls'] += 1
        if state['calls'] >= 2:       # first read ok, post-write read "moved"
            return real_getmtime(path) + 10.0
        return real_getmtime(path)

    monkeypatch.setattr(ro.os.path, 'getmtime', shaky_getmtime)
    summary = ro.apply_tick_verdicts(store_path=store, db_path=gateway)
    assert 'aborted' in summary
    rows = _read_store(store)
    assert len(rows) == 1 and rows[0]['success'] is None   # untouched


def test_stamped_verdicts_survive_the_tr299_merge(tmp_path):
    """Integration with the merge the brief pins: a stamped row behaves like
    any settled row under _merge_task_rows — plain steps never flip a settled
    success, the sticky OR rules, and the acceptance leg still rules
    (TR-299)."""
    stamped = _store_row('k1', success=False)
    stamped['success_reason'] = 'tick_concluded_no_commits'
    plain_step = dict(stamped, success=None, tokens_in=10, steps=2)
    merged = ro._merge_task_rows(stamped, plain_step)
    assert merged['success'] is False
    assert merged['success_reason'] == 'tick_concluded_no_commits'
    winner = dict(_store_row('k1', success=True),
                  success_reason='tick_commits_observed')
    merged = ro._merge_task_rows(stamped, winner)
    # sticky OR (TR-299's own rule): a task that ever succeeded is a success,
    # and the newest reason follows the verdict
    assert merged['success'] is True
    assert merged['success_reason'] == 'tick_commits_observed'
    # ...but an ACCEPTANCE row is independent evidence and rules regardless
    acceptance = dict(winner, success=False, acceptance_status='fail',
                      acceptance_source='board')
    merged = ro._merge_task_rows(stamped, acceptance)
    assert merged['success'] is False


def test_normalize_row_round_trips_the_reason_field():
    """The validator knows success_reason: stamped rows re-post cleanly, and
    pre-TR-357 rows (no field) keep round-tripping shape-identically — the
    committed-sample contract."""
    bare = {'source_system': 'hermes', 'session_id': 'k1',
            'task_label': 't', 'complexity': None, 'profile_id': None,
            'required_categories': None, 'provider': 'p', 'model': 'm',
            'turns': 1, 'tokens_in': 1, 'tokens_out': 1,
            'tokens_reasoning': 0, 'cost_usd': 0.0, 'wall_time_s': 1.0,
            'success': None, 'ts': 1_800_000_000.0}
    assert ro.normalize_row(bare) == bare           # absent stays absent
    stamped = dict(bare, session_id='k2', success=False,
                   success_reason='tick_concluded_no_commits')
    assert ro.normalize_row(stamped) == stamped     # carried through
    bad = dict(bare, session_id='k3', success_reason='   ')
    with pytest.raises(ValueError):
        ro.normalize_row(bad)


def test_import_hermes_row_builder_still_defers(tmp_path, monkeypatch):
    """The gateway row builder must NOT infer (TR-050 doctrine kept): nulls
    at build, verdicts from the tick pass only."""
    gateway = _make_gateway_db(tmp_path, usage=[_usage('uuid-1')],
                               sessions=[('uuid-1', 'sess-key-1', 'w1')])
    _route_env(monkeypatch, str(tmp_path / 'no-sched.db'), gateway,
               str(tmp_path / 'o.jsonl'))
    rows = ro.import_hermes(gateway)
    assert len(rows) == 1
    assert rows[0]['success'] is None and rows[0]['provider'] == 'xkiro'


# ---------------------------------------------------------------------------
# CLI wiring: one command leaves the store fully verdicted
# ---------------------------------------------------------------------------

def _cli_fixtures(tmp_path, monkeypatch, ticks, sessions, usage, store_rows):
    sched = _make_sched_db(tmp_path, ticks)
    gateway = _make_gateway_db(tmp_path, sessions=sessions, usage=usage)
    store = _write_store(tmp_path, store_rows)
    _route_env(monkeypatch, sched, gateway, store)
    monkeypatch.setattr(ro, 'OUTCOMES', store)   # main()'s import-time global
    monkeypatch.setattr(ro, 'AVERAGES', str(tmp_path / 'averages.jsonl'))
    return sched, gateway, store


def test_import_hermes_verb_leaves_the_store_verdicted(tmp_path, monkeypatch,
                                                       capsys):
    sched, gateway, store = _cli_fixtures(
        tmp_path, monkeypatch,
        ticks=[_tick(session='sess-key-1', commits=2),
               _tick(tid='t2', session='sess-key-2', commits=0)],
        sessions=[('uuid-1', 'sess-key-1', 'w1')],
        usage=[_usage('uuid-1')],
        store_rows=[])
    assert ro.main(['import-hermes']) == 0
    out = capsys.readouterr().out
    assert 'tick verdicts:' in out
    rows = _read_store(store)
    assert len(rows) == 1
    # the freshly imported row went in null and came out verdicted in ONE run
    assert rows[0]['success'] is True
    assert rows[0]['success_reason'] == 'tick_commits_observed'
    # the dry tick has no store row (no session_model_usage) — nothing faked
    assert not any(r['session_id'] == 'sess-key-2' for r in rows)


def test_apply_verdicts_verb(tmp_path, monkeypatch, capsys):
    sched, gateway, store = _cli_fixtures(
        tmp_path, monkeypatch,
        ticks=[_tick(session='k1', commits=0)],
        sessions=[], usage=[],
        store_rows=[_store_row('k1')])
    assert ro.main(['apply-verdicts']) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload['changed'] == 1
    assert _read_store(store)[0]['success'] is False
