"""TR-049 component 3 — the outcomes_averages.py rolling-average CLI.

Unit + CLI level tests for the exponential-decay windows, the bucket shape and
the dry-run/write paths. No network, no live store.
"""
import json
import os
import sys
import threading
import time

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


# ---------------------------------------------------------------------------
# TR-366 — the router server's IN-PROCESS outcome loops
#
# Two Hermes crons owned this pipeline and both died silently (paused
# no-agent jobs): dc9786e46a48 (hourly averages refresh, TR-065) and
# 996dfcbdf4d5 (hourly outcome import drivers, TR-116). The serving process
# now owns the loops via scripts/router_background_jobs.py:
#   * due-ness derives from FILE MTIME only — a restart never resets the clock;
#   * every run reports last_run/duration_s/rows/error into /health,
#     including FAILURES (a failed refresh surfaces, it does not die quietly);
#   * the loop runs on a daemon background thread — the request path never
#     awaits it.
# ---------------------------------------------------------------------------

import router_background_jobs as rbj  # noqa: E402 | scripts already on sys.path above.


class _FakeClock:
    """Deterministic now; advance() simulates time passing between polls."""

    def __init__(self, start=NOW):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _touch(path, mtime):
    os.utime(path, (mtime, mtime))


def _jobs(tmp_path, clock, store=True, averages=True, **kw):
    store_p = tmp_path / 'outcomes.jsonl'
    avg_p = tmp_path / 'outcomes-averages.jsonl'
    for p, present in ((store_p, store), (avg_p, averages)):
        if present:
            p.write_text('')
    kw.setdefault('import_rerun_floor_s', 0.0)
    return rbj.BackgroundJobs(store_path=store_p, averages_path=avg_p,
                              poll_s=0.01, clock=clock, **kw)


def test_import_due_when_store_missing_or_stale(tmp_path):
    clock = _FakeClock()
    jobs = _jobs(tmp_path, clock, store=False)
    assert jobs.import_due() is True  # missing store = due
    jobs = _jobs(tmp_path, clock, store=True)
    _touch(jobs.store_path, clock.t - 3601)
    assert jobs.import_due() is True  # older than the import period
    _touch(jobs.store_path, clock.t - 3599)
    assert jobs.import_due() is False  # fresh within the period


def test_averages_due_when_missing_or_input_newer(tmp_path):
    clock = _FakeClock()
    jobs = _jobs(tmp_path, clock, averages=False)
    assert jobs.averages_due() is True  # missing output = due
    jobs = _jobs(tmp_path, clock)
    _touch(jobs.store_path, clock.t - 10)
    _touch(jobs.averages_path, clock.t - 20)
    assert jobs.averages_due() is True  # input newer than output (make rule)
    _touch(jobs.averages_path, clock.t - 5)
    assert jobs.averages_due() is False  # output current
    _touch(jobs.averages_path, clock.t - 3601)
    assert jobs.averages_due() is True  # output aged past its period


def test_restart_does_not_reset_the_clock(tmp_path):
    """A fresh BackgroundJobs instance (the restart) must consult mtimes, not
    a memory of runs: a job that was serviced 5 minutes ago is NOT due even
    though the new instance has never run it."""
    clock = _FakeClock()
    jobs = _jobs(tmp_path, clock)
    _touch(jobs.store_path, clock.t - 300)
    _touch(jobs.averages_path, clock.t - 200)
    reborn = rbj.BackgroundJobs(store_path=jobs.store_path,
                                averages_path=jobs.averages_path,
                                poll_s=0.01, import_rerun_floor_s=0.0,
                                clock=clock)
    assert reborn.import_due() is False
    assert reborn.averages_due() is False
    # ...and a genuinely stale store IS picked up immediately at boot.
    _touch(jobs.store_path, clock.t - 7200)
    assert reborn.import_due() is True


def test_averages_runner_writes_file_and_reports(tmp_path):
    clock = _FakeClock()
    jobs = _jobs(tmp_path, clock)
    rows = [_row(source='hermes', cost=2.0, age_s=0),
            _row(source='hermes', cost=6.0, age_s=600)]
    with open(jobs.store_path, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    jobs._run('averages')
    st = jobs._state['averages']
    assert st['error'] is None and st['rows'] > 0
    assert st['last_run'] and isinstance(st['duration_s'], float)
    assert jobs.averages_path.exists()
    buckets = [json.loads(l) for l in jobs.averages_path.open() if l.strip()]
    assert buckets and all('avg_cost_task_24h' in b for b in buckets)
    # Use the filesystem clock for freshly written files, not the fake future.
    assert jobs.averages_due(now=time.time()) is False


def test_failed_refresh_surfaces_error_and_loop_continues(tmp_path):
    """AC3: a failing refresh surfaces in /health rather than silence — and
    the loop keeps going (self-healing: the next poll retries)."""
    clock = _FakeClock()
    calls = []

    def failing_runner():
        calls.append(1)
        raise OSError('unwritable averages path (probe)')

    jobs = _jobs(tmp_path, clock, averages=False,
                 runners={'averages': failing_runner,
                          'outcomes_import': lambda: 0})
    jobs._run('averages')
    st = jobs._state['averages']
    assert 'unwritable' in st['error']
    assert st['rows'] is None and st['last_run']
    # the loop retries on the next pass and the newest verdict wins
    jobs._maybe_run('averages', clock.t)
    assert len(calls) == 2
    assert jobs._state['averages']['runs'] == 2


def test_unwritable_averages_path_surfaces_error_for_real(tmp_path):
    """The failing-refresh probe against a REAL unwritable output path
    (the brief's named fixture). Skipped for root, which bypasses modes."""
    if os.geteuid() == 0:
        pytest.skip('root ignores directory modes')
    clock = _FakeClock()
    locked = tmp_path / 'locked'
    locked.mkdir()
    os.chmod(locked, 0o500)
    jobs = rbj.BackgroundJobs(store_path=str(tmp_path / 'outcomes.jsonl'),
                              averages_path=locked / 'outcomes-averages.jsonl',
                              poll_s=0.01, import_rerun_floor_s=0.0,
                              clock=clock)
    (tmp_path / 'outcomes.jsonl').write_text(
        json.dumps(_row(source='hermes', cost=1.0, age_s=0)) + '\n')
    try:
        jobs._run('averages')
    finally:
        os.chmod(locked, 0o700)
    st = jobs._state['averages']
    assert st['error'] and 'Permission' in st['error']
    assert st['rows'] is None


def test_import_runner_calls_every_driver_and_appends(tmp_path, monkeypatch):
    """The TR-116 sweep runs ALL four drivers + tick verdicts, appending
    through router_outcomes.append_rows — the same chain the removed cron's
    script drove via the CLI."""
    clock = _FakeClock()
    jobs = _jobs(tmp_path, clock)
    made = {'drivers': [], 'verdicts': 0}

    def fake_driver(tag, rows):
        def _f(*a, **kw):
            made['drivers'].append(tag)
            return rows
        return _f

    one_row = [_row(source='hermes', cost=1.0, age_s=0)]
    monkeypatch.setattr(ro, 'import_hermes', fake_driver('hermes', one_row))
    monkeypatch.setattr(ro, 'import_pi', fake_driver('pi', one_row))
    monkeypatch.setattr(ro, 'import_opencode', fake_driver('opencode', []))
    monkeypatch.setattr(ro, 'import_openclaw', fake_driver('openclaw', []))

    def fake_verdicts(store_path=None, db_path=None):
        made['verdicts'] += 1
        return {'stamped': 0}

    monkeypatch.setattr(ro, 'apply_tick_verdicts', fake_verdicts)
    monkeypatch.setattr(ro, '_hermes_db_path', lambda: str(tmp_path / 's.db'))
    n = jobs._run_outcomes_import()
    assert made['drivers'] == ['hermes', 'pi', 'opencode', 'openclaw']
    assert made['verdicts'] == 1
    assert n == 1  # the second driver returns the same key: deduplicated
    store_rows = [json.loads(l) for l in jobs.store_path.open() if l.strip()]
    assert len(store_rows) == 1


def test_import_rerun_floor_bounds_no_new_row_rescans(tmp_path):
    """A sweep that adds no rows leaves the store mtime unmoved, so it stays
    'due' — the rerun floor stops the loop rescanning the scheduler DB every
    poll while mtime still governs catch-up across restarts."""
    clock = _FakeClock()
    jobs = _jobs(tmp_path, clock, import_rerun_floor_s=900.0,
                 runners={'outcomes_import': lambda: 0, 'averages': lambda: 0})
    _touch(jobs.store_path, clock.t - 7200)
    assert jobs.import_due(clock.t)
    jobs._maybe_run('outcomes_import', clock.t)  # attempt 1 (adds nothing)
    clock.advance(60)
    jobs._maybe_run('outcomes_import', clock.t)  # inside the floor: skipped
    assert jobs._state['outcomes_import']['runs'] == 1
    clock.advance(900)
    jobs._maybe_run('outcomes_import', clock.t)  # floor elapsed: retried
    assert jobs._state['outcomes_import']['runs'] == 2


def test_loop_runs_in_background_thread_and_is_stopplable(tmp_path):
    """AC4: the request path never awaits the loops — they live on their own
    daemon thread, and stop() ends the loop promptly."""
    clock = _FakeClock()
    release = threading.Event()
    started_flag = threading.Event()

    def slow_runner():
        started_flag.set()
        release.wait(5)
        return 0

    jobs = _jobs(tmp_path, clock, averages=False,
                 runners={'outcomes_import': slow_runner,
                          'averages': slow_runner})
    t0 = time.time()
    jobs.start()
    assert jobs.started
    assert time.time() - t0 < 1.0  # start() returned before the run finished
    assert started_flag.wait(5)    # the loop kicked off the due job
    assert jobs._thread is not None and jobs._thread is not threading.current_thread()
    assert jobs._thread.daemon
    release.set()
    jobs.stop()
    jobs.join(5)
    assert not jobs._thread.is_alive()


def test_health_snapshot_shape_and_not_started_view(tmp_path):
    clock = _FakeClock()
    jobs = _jobs(tmp_path, clock, runners={'outcomes_import': lambda: 0,
                                           'averages': lambda: 0})
    snap = jobs.health_snapshot()
    assert snap['enabled'] is True
    for name in ('averages', 'outcomes_import'):
        block = snap[name]
        assert {'last_run', 'duration_s', 'rows', 'error'} <= set(block)
    assert snap['averages']['error'] is None
    # a process that never started the loops reports that honestly
    not_started = rbj.health_snapshot()
    assert not_started['enabled'] is False


def test_health_payload_carries_background_jobs_and_averages_block(monkeypatch):
    """router_health.health() publishes the block: an averages block with
    last_run/duration_s/rows/error at the top level, plus the full view
    under background_jobs. When the loops never started (other tools
    importing router_health), the block degrades fail-open."""
    import router_health as rh
    monkeypatch.setattr(rbj, 'health_snapshot',
                        lambda: {'enabled': False,
                                 'reason': 'loops not started in this process'},
                        raising=False)
    payload = rh.health(mode='read-only')
    assert payload['background_jobs'] == {'enabled': False,
                                          'reason': 'loops not started in this process'}
    block = payload['averages']
    assert block['enabled'] is False and 'error' not in block

    monkeypatch.setattr(rbj, 'health_snapshot', lambda: {
        'enabled': True, 'poll_s': 30,
        'averages': {'last_run': '2026-11-23T13:00:00+00:00',
                     'duration_s': 0.01, 'rows': 42, 'error': None,
                     'path': '/x', 'period_s': 3600, 'due': False},
        'outcomes_import': {'last_run': None, 'duration_s': None,
                            'rows': None, 'error': 'boom'},
    }, raising=False)
    payload = rh.health(mode='read-only')
    assert payload['background_jobs']['enabled'] is True
    avg = payload['averages']
    assert {'last_run', 'duration_s', 'rows', 'error'} <= set(avg)
    assert avg['rows'] == 42 and avg['error'] is None
    assert payload['background_jobs']['outcomes_import']['error'] == 'boom'


def test_module_singleton_start_default_is_idempotent(monkeypatch, tmp_path):
    """start_default() builds ONE process-wide scheduler; a second call
    returns the same instance (server boot must never spawn two loops)."""
    made = []
    real_init = rbj.BackgroundJobs.__init__

    def counting_init(self, *a, **kw):
        real_init(self, store_path=tmp_path / 'o.jsonl',
                  averages_path=tmp_path / 'a.jsonl', poll_s=0.01,
                  clock=_FakeClock(),
                  runners={'outcomes_import': lambda: 0, 'averages': lambda: 0})
        made.append(1)

    monkeypatch.setattr(rbj.BackgroundJobs, '__init__', counting_init)
    rbj._JOBS = None
    try:
        first = rbj.start_default()
        second = rbj.start_default()
        assert first is second and len(made) == 1
        assert first.started
    finally:
        if rbj._JOBS is not None:
            rbj._JOBS.stop()
        rbj._JOBS = None

