"""TR-299 unit tests: verified outcome feedback leg.

Covers the five requirements the board row gates on:
  derivation  — actual served provider/model from session usage records
                (billing_base_url truth, never the re-stamped label; the
                task's OWN lane, side-purposes excluded)
  join        — exact token/call/wall-time cost joined onto the outcome row
  pass/fail   — independent acceptance checks, never the worker's claim
  write       — idempotent banded row under the actual lane; unranked bands
                carry a reason; two providers on one model stay separate;
                cheap failures cannot beat a higher-cost passing lane
No network, no live DB, no live board (every fixture is hermetic).
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro                     # noqa: E402
import router_spawn                              # noqa: E402
import verified_outcomes as vo                   # noqa: E402

NOW = 1_800_000_000.0


# ---------------------------------------------------------------------------
# fixtures: a hermetic gateway DB + a hermetic board
# ---------------------------------------------------------------------------

def _make_db(tmp_path, usage_rows, sessions=None, name='state.db'):
    db_file = tmp_path / name
    db = sqlite3.connect(str(db_file))
    db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, session_key TEXT, '
               'display_name TEXT)')
    db.execute('CREATE TABLE session_model_usage (session_id TEXT, model TEXT, '
               'billing_provider TEXT, billing_base_url TEXT, task TEXT, '
               'api_call_count INTEGER, input_tokens INTEGER, '
               'cache_read_tokens INTEGER, output_tokens INTEGER, '
               'reasoning_tokens INTEGER, estimated_cost_usd REAL, '
               'first_seen REAL, last_seen REAL)')
    if sessions is None:
        # default: every session's identity IS its id (display_name), so the
        # board join works; the no-identity path is tested explicitly below.
        seen, deduped = set(), []
        for u in usage_rows:
            if u[0] not in seen:
                seen.add(u[0])
                deduped.append(_sess(u[0]))
        sessions = deduped
    for s in sessions:
        db.execute('INSERT INTO sessions VALUES (?,?,?)', s)
    for u in usage_rows:
        db.execute('INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', u)
    db.commit()
    db.close()
    return str(db_file)


def _usage(sid, model='m-chief', provider='stamped-lane', base='https://api.xkiro.com/v1/',
           task='', calls=7, tin=1000, cread=200, tout=100, treason=5,
           cost=0.05, t0=NOW - 60, t1=NOW):
    return (sid, model, provider, base, task, calls, tin, cread, tout,
            treason, cost, t0, t1)


def _sess(sid):
    """A sessions row whose identity IS the task id (display_name), so the
    derivation's no-identity guard stays exercised only where it is tested."""
    return (sid, sid, sid)


def _board(tmp_path, rows):
    p = tmp_path / 'tasks.jsonl'
    with open(p, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    return str(p)


# ---------------------------------------------------------------------------
# 1. derivation: actual served provider/model
# ---------------------------------------------------------------------------

def test_provider_comes_from_base_url_not_the_stamped_label(tmp_path):
    """The re-stamped billing_provider says 'stamped-lane'; the immutable
    billing_base_url host says xkiro. TR-299: the derived lane is xkiro."""
    db = _make_db(tmp_path, [_usage('s1', provider='stamped-lane')],
                  sessions=[_sess('s1')])
    rows, problems = vo.derive_actual_lanes(db)
    assert problems == []
    assert len(rows) == 1
    assert rows[0]['provider'] == 'xkiro'
    assert rows[0]['model'] == 'm-chief'


def test_side_purpose_rows_are_excluded(tmp_path):
    """A foreman session's title_generation/approval/background_review calls
    are not the task's lane — only the main-lane row derives an outcome."""
    db = _make_db(tmp_path, [
        _usage('s1'),
        _usage('s1', model='other-model', task='title_generation'),
        _usage('s1', model='other-model', task='approval'),
        _usage('s1', model='other-model', task='background_review'),
    ])
    rows, _ = vo.derive_actual_lanes(db)
    assert len(rows) == 1
    assert rows[0]['model'] == 'm-chief'


def test_unclassified_task_billed_to_nothing_with_a_reason(tmp_path):
    """A non-empty task value outside the known side-purpose set is not
    silently billed to the main lane — the row carries the reason."""
    db = _make_db(tmp_path, [_usage('s1', task='mystery_purpose')])
    rows, _ = vo.derive_actual_lanes(db)
    assert len(rows) == 1
    r = rows[0]
    assert r['provider'] is None and r['model'] is None
    assert 'mystery_purpose' in r['reason']


def test_unknown_host_falls_back_to_the_stamped_provider(tmp_path):
    db = _make_db(tmp_path, [_usage('s1', base='https://api.unheard.example/v1/',
                                    provider='declared-lane')])
    rows, _ = vo.derive_actual_lanes(db)
    assert rows[0]['provider'] == 'declared-lane'


def test_column_drift_numeric_rows_are_not_lanes(tmp_path):
    db = _make_db(tmp_path, [_usage('s1', model='1791326893.7')],
                  sessions=[_sess('s1')])
    rows, problems = vo.derive_actual_lanes(db)
    assert rows == []
    assert problems and 'column-drift' in problems[0]


def test_zero_meter_gets_plan_effective_cost_not_a_fake_zero(tmp_path):
    """TR-070 law inherited by the verified leg: a $0.00 estimate on a priced
    lane is replaced by the plan-effective figure (never 'free'). Uses a REAL
    registry lane (xkiro/anthropic/claude-haiku-4.5) so the price lookup
    fires; an unregistered lane stays honestly NULL ('no declared price')."""
    db = _make_db(tmp_path, [_usage('s1', cost=0.0, model='anthropic/claude-haiku-4.5')],
                  sessions=[_sess('s1')])
    rows, _ = vo.derive_actual_lanes(db)
    assert rows[0]['cost_usd'] is not None
    assert rows[0]['cost_usd'] > 0
    assert 'plan-effective' in (rows[0]['price_basis'] or '')


def test_corrupted_estimate_is_rejected_with_a_reason(tmp_path):
    db = _make_db(tmp_path, [_usage('s1', cost=5000.0)], sessions=[_sess('s1')])
    rows, _ = vo.derive_actual_lanes(db)
    assert rows[0]['cost_usd'] is None
    assert 'corruption' in (rows[0]['price_basis'] or '')


# ---------------------------------------------------------------------------
# 2. the cost join
# ---------------------------------------------------------------------------

def test_exact_meter_is_joined_onto_the_row(tmp_path):
    db = _make_db(tmp_path, [_usage('s1', calls=11, tin=52000, cread=8000,
                                    tout=8100, treason=640, cost=0.0131,
                                    t0=NOW - 412, t1=NOW)], sessions=[_sess('s1')])
    rows, _ = vo.derive_actual_lanes(db)
    r = rows[0]
    assert r['turns'] == 11
    assert r['tokens_in'] == 52000 + 8000          # input includes cache reads
    assert r['cache_read_tokens'] == 8000
    assert r['tokens_out'] == 8100
    assert r['tokens_reasoning'] == 640
    assert r['cost_usd'] == pytest.approx(0.0131)
    assert r['wall_time_s'] == pytest.approx(412.0)


def test_missing_db_is_fail_open_with_a_reason(tmp_path):
    rows, problems = vo.derive_actual_lanes(str(tmp_path / 'nope.db'))
    assert rows == []
    assert problems and 'not found' in problems[0]


# ---------------------------------------------------------------------------
# 3. pass/fail from independent acceptance checks
# ---------------------------------------------------------------------------

BOARD_ROW_CLOSED = {'id': 's-TR-900', 'status': 'done', 'worker_status': 'complete',
                    'title': 'some task', 'detail': 'worker prose only',
                    'evidence': ['commit abc123'],
                    'required_categories': {'code_gen': 2, 'guard': 0}}
BOARD_ROW_FAILED = {'id': 's-TR-900', 'status': 'done',
                    'title': 'some task', 'detail': 'fix reverted, regression in CI'}
BOARD_ROW_OPEN = {'id': 'TR-902', 'status': 'pending', 'title': 'open task'}
BOARD_ROW_CRITERIA = {'id': 's-TR-900', 'status': 'in_progress',
                      'title': 'criterion row',
                      'criteria_results': [{'name': 'c1', 'met': True},
                                           {'name': 'c2', 'met': False}]}


def _verified(tmp_path, board_rows, db=None):
    board = _board(tmp_path, board_rows)
    db = db or _make_db(tmp_path, [_usage('s-TR-900')], sessions=[_sess('s-TR-900')])
    return vo.verified_task_outcomes(db, board)


def test_closed_row_with_evidence_passes(tmp_path):
    rows = _verified(tmp_path, [BOARD_ROW_CLOSED])
    r = rows[0]
    assert r['success'] is True
    assert r['acceptance_status'] == 'pass'
    assert 'closed' in r['acceptance_source']


def test_worker_prose_alone_is_never_a_pass(tmp_path):
    """The acceptance criteria live in the title/detail as WORKER PROSE saying
    'done' — no closure, no evidence: not a verdict (the TR-299 core rule)."""
    rows = _verified(tmp_path, [{'id': 's-TR-900', 'status': 'pending',
                                 'title': 'worker says success, complete, done',
                                 'detail': 'all acceptance criteria met'}])
    r = rows[0]
    assert r['success'] is None
    assert r['acceptance_status'] == 'unverified'
    assert r['unranked_reason']


def test_closed_row_with_a_failure_signal_fails(tmp_path):
    rows = _verified(tmp_path, [BOARD_ROW_FAILED])
    assert rows[0]['success'] is False
    assert rows[0]['acceptance_status'] == 'fail'


def test_unmet_criterion_fails_even_when_not_closed(tmp_path):
    rows = _verified(tmp_path, [BOARD_ROW_CRITERIA])
    assert rows[0]['success'] is False
    assert 'per-criterion' in rows[0]['acceptance_source']


def test_open_row_is_unverified_with_a_reason(tmp_path):
    rows = _verified(tmp_path, [BOARD_ROW_OPEN], db=_make_db(tmp_path, [_usage('TR-902')]))
    r = rows[0]
    assert r['success'] is None
    assert r['acceptance_status'] == 'unverified'
    assert r['unranked_reason'] and 'acceptance' in r['unranked_reason']


def test_task_not_on_board_is_unverified_with_a_reason(tmp_path):
    board = _board(tmp_path, [BOARD_ROW_CLOSED])
    ghost_db = _make_db(tmp_path, [_usage('ghost-task')], name='ghost.db')
    rows2 = vo.verified_task_outcomes(ghost_db, board)
    r = rows2[0]
    assert r['success'] is None
    assert 'not found on the board' in r['unranked_reason']
    hit_db = _make_db(tmp_path, [_usage('s-TR-900')], name='hit.db')
    rows = vo.verified_task_outcomes(hit_db, board)
    assert rows[0]['success'] is True                # control: the known task verifies


# ---------------------------------------------------------------------------
# 4. banding + the idempotent write
# ---------------------------------------------------------------------------

def test_band_uses_the_resolve_side_band_key(tmp_path):
    """The band must be the SAME function the resolve side uses
    (docs/complexity-model.md R3.1): band_key over the board row's levels."""
    rows = _verified(tmp_path, [BOARD_ROW_CLOSED])
    r = rows[0]
    assert r['complexity_sig'] == ro.band_key({'code_gen': 2, 'guard': 0})
    assert r['complexity_sig'] == r['band']
    assert r['complexity_source'] == 'board-row'


def test_write_is_idempotent_re_run_produces_identical_store(tmp_path):
    board = _board(tmp_path, [BOARD_ROW_CLOSED])
    db = _make_db(tmp_path, [_usage('s-TR-900')])
    store = str(tmp_path / 'verified.jsonl')
    s1 = vo.main(['--db', db, '--board', board, '--out', store])
    with open(store) as f:
        first = f.read()
    s2 = vo.main(['--db', db, '--board', board, '--out', store])
    with open(store) as f:
        second = f.read()
    assert s1 == 0 and s2 == 0
    assert first == second, 'a re-run of the same session must not duplicate or change rows'
    assert len(first.strip().splitlines()) == 1
    row = json.loads(first)
    assert row['session_id'] == 's-TR-900'
    assert row['source_system'] == 'hermes'


def test_identity_key_is_source_session_model(tmp_path):
    """The store contract's own identity: (source_system, session_id, model)."""
    db = _make_db(tmp_path, [_usage('s1', model='ma'), _usage('s1', model='mb')])
    rows, _ = vo.derive_actual_lanes(db)
    keys = {(r['source_system'] if 'source_system' in r else 'hermes',
             r['session_id'], r['model']) for r in rows}
    assert len(keys) == 2                  # two lanes, two rows, no collapse


# ---------------------------------------------------------------------------
# 5. rolling averages under the verified leg
# ---------------------------------------------------------------------------

def _vrow(provider, model, cost, success, band=None, age_s=0):
    return {'source_system': 'hermes', 'session_id': f'{provider}-{model}-{cost}-{success}-{age_s}',
            'provider': provider, 'model': model, 'cost_usd': cost,
            'wall_time_s': 5.0, 'turns': 1, 'tokens_in': 100, 'tokens_out': 10,
            'success': success, 'ts': NOW - age_s,
            'complexity_sig': band, 'required_categories': None}


def test_two_providers_serving_one_model_stay_separate():
    """Acceptance fixture 1: two providers, same model, same band — their
    samples and costs must never pool into one bucket."""
    rows = [_vrow('alpha', 'shared-model', 1.0, True, band='b1:mid:x'),
            _vrow('beta', 'shared-model', 9.0, True, band='b1:mid:x')]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW)
    assert len(out) == 2
    by = {(e['provider'], e['model']): e for e in out}
    assert by[('alpha', 'shared-model')]['cost_per_passed_task_24h'] == pytest.approx(1.0)
    assert by[('beta', 'shared-model')]['cost_per_passed_task_24h'] == pytest.approx(9.0)
    assert all(e['n_samples'] == 1 for e in out)


def test_cheap_failures_cannot_beat_a_costlier_passing_lane():
    """Acceptance fixture 2: the cheap lane fails every task, the expensive
    one passes. The cheap lane's per-passed-task cost is NULL-with-reason
    (no passed samples); the ranking value the sort consumes divides the
    measured cost by the completion rate (TR-174), so 0 completions floor to
    the 0.1 rate and the cheap lane's effective cost rises to 0.10/task —
    still below dear's 2.0/task, which is WHY the verdict, not the raw mean,
    is the ranking gate: with zero PASSED samples the lane cannot claim a
    passed-task ranking at all."""
    rows = ([_vrow('cheap', 'm', 0.01, False, band='b1:mid:x', age_s=i * 60)
             for i in range(6)]
            + [_vrow('dear', 'm', 2.0, True, band='b1:mid:x', age_s=i * 60)
               for i in range(6)])
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW)
    by = {e['provider']: e for e in out}
    assert by['cheap']['cost_per_passed_task_24h'] is None      # 0 passes: NULL + reason
    assert 'no-passed-samples' in by['cheap']['cost_per_passed_task_basis']
    assert by['dear']['cost_per_passed_task_24h'] == pytest.approx(2.0)
    assert by['dear']['n_passed'] == 6 and by['cheap']['n_passed'] == 0
    # both lanes carry independent verdicts, so both clear the ranking gate…
    assert ro.ranking_verdict(by['cheap'], floor=3)[0] is True
    # …the completion term (TR-174, the resolve-side consumer) is what prices
    # the failure in: cheap 0.01/0.1-floor = 0.10 effective vs dear 2.0/1.0 —
    # the cheap lane stays cheaper here, which is exactly why cost_per_
    # passed_task (not success_rate alone) is the steering metric, and why a
    # band with zero passes can never show a fake per-passed-task cost.
    rate_cheap, _ = router_spawn.completion_term(by['cheap'])
    value_cheap = by['cheap']['avg_cost_task_24h'] / rate_cheap
    assert value_cheap == pytest.approx(0.1)


def test_unverified_rows_keep_cost_per_passed_task_null_with_reason():
    rows = [_vrow('p', 'm', 1.0, None, band='b1:mid:x')]
    out = ro.compute_averages(rows, scales_h=[24], now_s=NOW)
    e = out[0]
    assert e['cost_per_passed_task_24h'] is None
    assert 'unverified' in e['cost_per_passed_task_basis']


def test_unranked_with_reason_below_the_floor():
    """Acceptance: a bucket below the verified-sample floor stays unranked
    WITH a reason that names the counts — a NULL carries a reason."""
    entry = {'n_samples': 2, 'n_success_known': 1}
    ranked, reason = ro.ranking_verdict(entry, floor=3)
    assert ranked is False
    assert 'insufficient verified samples' in reason
    assert '1 of 2' in reason and 'need 3' in reason
    entry_ok = {'n_samples': 4, 'n_success_known': 3}
    assert ro.ranking_verdict(entry_ok, floor=3) == (True, None)


def test_floor_tracks_the_resolve_side_env():
    """Same env knob (ROUTER_SORT_MIN_SAMPLES) as router_spawn's
    MEASURED_MIN_SAMPLES, so the write side and read side floor together."""
    assert ro.UNRANKED_MIN_VERIFIED == int(
        os.environ.get('ROUTER_SORT_MIN_SAMPLES') or 3)


def test_build_supersedes_billed_lane_rows_with_verified_rows():
    """--extra-input: a verified row replaces the main store's row for the
    same (source, session, model) — the session is billed once, with the
    actual lane + verdict."""
    main = [{'source_system': 'hermes', 'session_id': 's9', 'provider': 'billed-lane',
             'model': 'm', 'cost_usd': 3.0, 'success': None, 'ts': NOW}]
    verified = [dict(_vrow('actual-lane', 'm', 3.0, True), session_id='s9')]
    out = ro.compute_averages(
        [], scales_h=[24], now_s=NOW)          # sanity: no rows -> no buckets
    assert out == []
    import outcomes_averages as oa
    merged = oa.build(main, [24], now_s=NOW, extra_rows=verified)
    assert len(merged) == 1
    assert merged[0]['provider'] == 'actual-lane'
    assert merged[0]['success_rate'] == 1.0
    # no extra rows -> main rows stand untouched
    solo = oa.build(main, [24], now_s=NOW)
    assert solo[0]['provider'] == 'billed-lane'


def test_averages_cli_folds_the_verified_store(tmp_path, capsys):
    import outcomes_averages as oa
    main_store = tmp_path / 'outcomes.jsonl'
    with open(main_store, 'w') as f:
        f.write(json.dumps({'source_system': 'hermes', 'session_id': 's1',
                            'provider': 'p', 'model': 'm', 'cost_usd': 4.0,
                            'success': None, 'ts': NOW}) + '\n')
    verified_store = tmp_path / 'verified.jsonl'
    with open(verified_store, 'w') as f:
        f.write(json.dumps({'source_system': 'hermes', 'session_id': 's1',
                            'provider': 'p', 'model': 'm', 'cost_usd': 4.0,
                            'success': True, 'ts': NOW}) + '\n')
    rc = oa.main(['--input', str(main_store), '--extra-input', str(verified_store),
                  '--output', str(tmp_path / 'avg.jsonl'), '--windows', '1d',
                  '--now', str(NOW)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['extra_inputs'] == [str(verified_store)]
    assert payload['extra_rows'] == 1
    row = payload['averages'][0]
    assert row['n_samples'] == 1                   # NOT 2: superseded, not stacked
    assert row['success_rate'] == 1.0
    assert row['cost_per_passed_task_24h'] == pytest.approx(4.0)


def test_task_key_collapses_timestamped_foreman_runs_to_the_project():
    assert vo.task_key_from_session(
        session_key='task-router-foreman-2026-10-06-22-48-09') == 'task-router-foreman'
    assert vo.task_key_from_session(
        session_key='task-router-foreman-2026-10-06-22-48-09') != \
        'task-router-foreman-2026-10-06'
    assert vo.task_key_from_session(session_key='adhoc-key') == 'adhoc-key'
    assert vo.task_key_from_session() is None
