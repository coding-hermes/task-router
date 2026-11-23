#!/usr/bin/env python3
"""router_background_jobs.py — TR-366: the router server's in-process jobs.

Two Hermes crons used to own the outcome pipeline and both died silently
(pause/no-agent delivery): dc9786e46a48 (hourly averages refresh, TR-065)
and 996dfcbdf4d5 (hourly outcome import drivers, TR-116). This module moves
BOTH loops INSIDE the serving process (scripts/router_server.py) so a run
needs no external scheduler: the same process that serves requests refreshes
the rolling averages and imports new outcome rows.

Jobs (mirroring the removed crons exactly):

- ``outcomes_import`` — the TR-116 driver sweep: import-hermes
  (+ tick-verdict stamping), import-pi, import-opencode, import-openclaw,
  each appended to the outcome store via router_outcomes.append_rows.
  Due when the store's mtime is older than the import period (or missing):
  the FILE is the clock, so a restart never resets it.

- ``averages`` — the TR-065 refresh: recompute the rolling averages from the
  store (outcomes_averages.build, the same math the CLI runs) and rewrite
  outcomes-averages.jsonl via write_rows. Due when the output file is
  missing, OLDER than the store (make-style: input newer than output), or
  past its own refresh age.

Contract:

- Due-ness derives from file mtime ONLY. Nothing in memory decides catch-up,
  so restarting the server at any moment either runs a due job immediately
  or waits until it is actually due — never a clock reset.
- Jobs run on ONE daemon thread with a fixed poll interval; the request
  path never awaits them (AC4: zero added work per served request — /health
  reads an in-memory dict and nothing else).
- Every run reports last_run / duration_s / rows / error into /health
  (router_health.background_jobs_block), including FAILED runs — a failing
  refresh surfaces instead of dying silently like the crons did. A failed
  job is re-attempted on a later poll (bounded by the import rerun floor so
  a store that gains no new rows cannot pin the loop).
- Fail-open: an exception inside the loop is recorded per-job and the loop
  keeps running; starting the jobs can never block or fail the boot.

Env hooks (read at construction): ROUTER_BG_POLL_S (default 30),
ROUTER_IMPORT_PERIOD_S (default 3600), ROUTER_AVERAGES_PERIOD_S
(default 3600), ROUTER_IMPORT_RERUN_FLOOR_S (default 900), ROUTER_BG_ENABLED
(set to '0' to boot without the loops).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import outcomes_averages as oa  # noqa: E402  (same math the averages CLI runs)
import router_outcomes as ro  # noqa: E402  (store paths + import drivers)

JOB_NAMES = ('outcomes_import', 'averages')


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec='seconds')


def _mtime(path: Path) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class BackgroundJobs:
    """The in-process scheduler. One daemon thread, two mtime-driven jobs."""

    def __init__(self, store_path: Any=None, averages_path: Any=None,
                 poll_s: float | None=None, import_period_s: float | None=None,
                 averages_period_s: float | None=None,
                 import_rerun_floor_s: float | None=None,
                 clock: Any=time.time, runners: dict | None=None) -> None:
        self.store_path = Path(store_path or ro.outcomes_path())
        self.averages_path = Path(averages_path or ro.averages_path())
        self.import_marker = self.store_path.with_name(self.store_path.name + '.imported')
        self.poll_s = float(poll_s if poll_s is not None
                            else _env_float('ROUTER_BG_POLL_S', 30.0))
        self.import_period_s = float(
            import_period_s if import_period_s is not None
            else _env_float('ROUTER_IMPORT_PERIOD_S', 3600.0))
        self.averages_period_s = float(
            averages_period_s if averages_period_s is not None
            else _env_float('ROUTER_AVERAGES_PERIOD_S', 3600.0))
        # The import's due-ness is the store's mtime, but a driver sweep that
        # adds no rows leaves that mtime unmoved — without a floor the loop
        # would re-scan the whole scheduler DB every poll. The floor bounds
        # re-attempts; mtime still decides catch-up across restarts.
        self.import_rerun_floor_s = float(
            import_rerun_floor_s if import_rerun_floor_s is not None
            else _env_float('ROUTER_IMPORT_RERUN_FLOOR_S', 900.0))
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = False
        #: per-job run report — exactly the fields /health publishes
        self._state: dict[str, dict] = {
            name: {'last_run': None, 'duration_s': None, 'rows': None,
                   'error': None, 'runs': 0}
            for name in JOB_NAMES}
        self._last_attempt: dict[str, float] = {}
        #: injectable for tests; default runners do the real cron-equivalent work
        self.runners: dict[str, Any] = runners or {
            'outcomes_import': self._run_outcomes_import,
            'averages': self._run_averages,
        }

    # ------------------------------------------------------------------ due
    def import_due(self, now: float | None=None) -> bool:
        """A successful sweep marker is the clock, not unrelated store writes.

        Until the FIRST successful sweep records the marker there is no
        marker clock — fall back to the store's own mtime so a restart
        never spuriously re-runs a recent import (the file is the clock,
        so the restart cannot reset it either).
        """
        now = self._clock() if now is None else now
        m = _mtime(self.import_marker)
        if m is None:
            s = _mtime(self.store_path)
            return s is None or now - s >= self.import_period_s
        return now - m >= self.import_period_s

    def averages_due(self, now: float | None=None) -> bool:
        """TR-065: missing output, input newer than output, or output aged out."""
        out_m = _mtime(self.averages_path)
        if out_m is None:
            return True
        now = self._clock() if now is None else now
        if now - out_m >= self.averages_period_s:
            return True
        in_m = _mtime(self.store_path)
        return in_m is not None and in_m > out_m

    # -------------------------------------------------------------- runners
    def _run_outcomes_import(self) -> int:
        """The removed cron's sweep: all four drivers + tick-verdict stamping."""
        store = str(self.store_path)
        added = 0
        errors = []
        for driver, args in (('import_hermes', (ro._hermes_db_path(),)),
                             ('import_pi', ()),
                             ('import_opencode', ()),
                             ('import_openclaw', ())):
            try:
                rows = getattr(ro, driver)(*args)
                if rows:
                    added += ro.append_rows(store, rows)
            except Exception as exc:  # noqa: BLE001 — continue the other drivers
                errors.append(f'{driver}: {type(exc).__name__}: {exc}')
        # Verdict rewriting can race live appenders; do not risk losing requests.
        # Surface this omission until the shared store has a common write lock.
        try:
            ro.apply_tick_verdicts(store_path=store)
        except Exception as exc:  # noqa: BLE001 — recorded, not fatal
            errors.append(f'tick_verdicts: {type(exc).__name__}: {exc}')
        if errors:
            raise RuntimeError('; '.join(errors))
        self.import_marker.parent.mkdir(parents=True, exist_ok=True)
        self.import_marker.touch()
        return added

    def _run_averages(self) -> int:
        """The removed cron's refresh, in-process (no subprocess, no CLI)."""
        rows = oa.read_rows(str(self.store_path))
        averages = oa.build(rows, oa.parse_windows(None))
        if averages:
            oa.write_rows(str(self.averages_path), averages)
        return len(averages)

    # ------------------------------------------------------------ execution
    def _run(self, name: str) -> None:
        t0 = time.perf_counter()
        report = {'last_run': _utc_now_iso(),
                  'duration_s': None, 'rows': None, 'error': None}
        try:
            report['rows'] = self.runners[name]()
        except Exception as exc:  # noqa: BLE001 — a failed job must SURFACE,
            # not kill the loop that is the whole point of TR-366.
            report['error'] = f'{type(exc).__name__}: {exc}'[:300]
        report['duration_s'] = round(time.perf_counter() - t0, 4)
        with self._lock:
            st = self._state[name]
            st.update(report)
            st['runs'] = st.get('runs', 0) + 1

    def _maybe_run(self, name: str, now: float) -> None:
        if name == 'outcomes_import':
            if not self.import_due(now):
                return
            floor = self.import_rerun_floor_s
        else:
            if not self.averages_due(now):
                return
            floor = 0.0  # self-limiting: a fresh write makes output newer
        last = self._last_attempt.get(name)
        if last is not None and (now - last) < floor:
            return
        self._last_attempt[name] = now
        self._run(name)

    def _loop(self) -> None:
        # First pass runs immediately: on boot, mtime already answers "due?",
        # so a restart catches up at once instead of waiting one poll.
        while True:
            now = self._clock()
            for name in ('outcomes_import', 'averages'):
                if self._stop.is_set():
                    return
                try:
                    self._maybe_run(name, now)
                except Exception:  # noqa: BLE001 — belt-and-braces; _run already
                    pass           # records job errors, never raise out of the thread
            if self._stop.wait(self.poll_s):
                return

    # --------------------------------------------------------------- control
    def start(self) -> 'BackgroundJobs':
        """Idempotent; returns self. A daemon thread — boot is never blocked."""
        if self.started:
            return self
        self.started = True
        self._thread = threading.Thread(target=self._loop,
                                        name='router-background-jobs',
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float=5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ---------------------------------------------------------------- health
    def health_snapshot(self) -> dict:
        with self._lock:
            jobs = {name: dict(st) for name, st in self._state.items()}
        now = self._clock()
        return {
            'enabled': True,
            'poll_s': self.poll_s,
            'store': str(self.store_path),
            'averages_file': str(self.averages_path),
            'import_period_s': self.import_period_s,
            'averages_period_s': self.averages_period_s,
            'due': {'outcomes_import': self.import_due(now),
                    'averages': self.averages_due(now)},
            'jobs': jobs,
            # The AC names the fields directly under the averages block; the
            # hoisted view below keeps `jq .averages` truthful without digging.
            'averages': {**jobs['averages'],
                         'path': str(self.averages_path),
                         'period_s': self.averages_period_s,
                         'due': self.averages_due(now)},
            'outcomes_import': {**jobs['outcomes_import'],
                                'path': str(self.store_path),
                                'period_s': self.import_period_s,
                                'due': self.import_due(now)},
        }


#: The process-wide instance (None until the server starts the loops; other
#: importers — tests, CLI tools — see enabled=False instead of a dead block).
_JOBS: BackgroundJobs | None = None


def enabled() -> bool:
    return os.environ.get('ROUTER_BG_ENABLED', '1').strip() != '0'


def start_default() -> BackgroundJobs | None:
    """Build + start the process-wide scheduler (server boot path).

    Returns None when disabled via ROUTER_BG_ENABLED=0. Never raises: the
    serving process boots with or without the loops.
    """
    global _JOBS
    if _JOBS is not None:
        return _JOBS
    if not enabled():
        return None
    _JOBS = BackgroundJobs().start()
    return _JOBS


def health_snapshot() -> dict:
    """/health view. A process that never started the loops reports that
    honestly (enabled: False) instead of fabricating an empty run report."""
    if _JOBS is None:
        if not enabled():
            return {'enabled': False, 'reason': 'ROUTER_BG_ENABLED=0'}
        return {'enabled': False,
                'reason': 'background loops not started in this process'}
    return _JOBS.health_snapshot()


if __name__ == '__main__':
    # Manual one-shot, cron-equivalent: run each job if due and print the
    # health view. (`python3 scripts/router_background_jobs.py`)
    jobs = BackgroundJobs()
    now = jobs._clock()
    for _name in JOB_NAMES:
        if (_name == 'outcomes_import' and jobs.import_due(now)) or \
           (_name == 'averages' and jobs.averages_due(now)):
            jobs._run(_name)
    print(json.dumps(jobs.health_snapshot(), ensure_ascii=False))
