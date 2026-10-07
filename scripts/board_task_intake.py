#!/usr/bin/env python3
"""board_task_intake.py — TR-298: automatic test intake from the board.

Owner goal (2026-10-03): "every time we have a test go in we pick a model".
Until now a newly submitted board task only got its resolve when a human ran
`router_spawn.py --from-task <id>` by hand. This daemon closes that gap.

Source-of-truth event: a new row appended to the canonical board
(`.coding-hermes/board/tasks.jsonl`) with status `pending`. That append IS the
"task submitted" event in this fleet — nothing else to poll, no API to watch.

What it does, per poll (default every 30s, `--interval-s`):

    stat the board (mtime+size)          -> unchanged: nothing to do
    read only the NEW bytes (tracked offset)
    parse complete lines only            -> a truncated trailing line is left
                                            for the next poll (every JSONL
                                            consumer in scripts/ tolerates it)
    for each NEW pending task id not in the seen state:
        append a claim to the seen state          (crash-safe ordering)
        run the existing resolve as a subprocess:
          <python> scripts/router_spawn.py --from-task <id> --board <board> --format json
        append the result to the resolves ledger (id, ts, chain head,
        complexity_source, the FULL resolve payload, error when failed)
        complete the claim in the seen state

No synthesized profiles anywhere: the resolve is the existing `--from-task`
path, so ratings come from the classifier/JEV scoring of the real task text
and candidates come from the registry with their measured-or-price basis.

Ledgers (runtime state, gitignored — like the outcomes store, TR-049):

    data/state/intake-seen.jsonl      one {id, ts, resolved_at, source} row
                                      per event; LAST row per id wins, so a
                                      claim is completed by a later row, and
                                      a replay of a seen id resolves nothing
    data/state/intake-resolves.jsonl  one row per resolve attempt, including
                                      failures — errors are fail-open (the
                                      daemon never crashes, the loop keeps
                                      running) but NEVER quiet: every failure
                                      lands here with an `error` field

Crash safety: the seen row is claimed BEFORE the subprocess runs. If the
daemon dies mid-resolve, the next boot finds the dangling claim and writes an
`error` record to the resolves ledger (reconciliation at startup) — a task is
never lost silently, and it is never resolved twice.

Cold start: a first boot (empty seen state) seeds every id already on the
board as seen WITHOUT resolving them — intake begins with genuinely new
submissions. `--once` is the opposite contract: drain the CURRENT backlog and
exit (testing/CI), resolving every pending id the state does not know yet.
Repeat runs stay idempotent: the second `--once` adds no records.

Stdlib only (like every script in this repo — it must run in the bare board
venv). Exit code is always 0 in daemon form; `--once` also exits 0 and reports
its tally on stderr — the ledger is the record, not the exit code.
"""
import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BOARD = os.path.join(REPO, '.coding-hermes', 'board', 'tasks.jsonl')
DEFAULT_STATE_DIR = os.path.join(REPO, 'data', 'state')
DEFAULT_SPAWN_SCRIPT = os.path.join(REPO, 'scripts', 'router_spawn.py')

#: Ledger file names under the state dir (gitignored as a class, TR-298).
SEEN_FILE = 'intake-seen.jsonl'
RESOLVES_FILE = 'intake-resolves.jsonl'

#: The only board status that counts as "task submitted".
PENDING_STATUS = 'pending'

#: Budget for one resolve subprocess. The spawn path itself is fail-open
#: (exit 0 + {"error": ...}), so this only bounds a hung classifier/probe
#: under fleet load — the same sizing doctrine as tests/conftest.py SEED_TIMEOUT.
RESOLVE_TIMEOUT_S = 600

ENV_BOARD = 'ROUTER_INTAKE_BOARD'
ENV_STATE_DIR = 'ROUTER_INTAKE_STATE_DIR'
ENV_INTERVAL_S = 'ROUTER_INTAKE_INTERVAL_S'
ENV_SPAWN_CMD = 'ROUTER_INTAKE_SPAWN_CMD'


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def seen_path(state_dir):
    return os.path.join(state_dir, SEEN_FILE)


def resolves_path(state_dir):
    return os.path.join(state_dir, RESOLVES_FILE)


def append_jsonl(path, row):
    """Append ONE row, flock-serialized, fsync'd.

    Same discipline as the outcome store (router_outcomes.py): the ledger is
    the record of record — a crash must not leave a torn line behind.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(row, sort_keys=True) + '\n'
    with open(path, 'a', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_seen(path):
    """id -> last seen row. Malformed lines are skipped (tolerant, like the
    outcomes tail reader); the last row per id wins so claims complete."""
    seen = {}
    if not os.path.exists(path):
        return seen
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get('id'):
                seen[str(row['id'])] = row
    return seen


def run_resolve(spawn_cmd, board, task_id, timeout_s=RESOLVE_TIMEOUT_S):
    """Run the existing resolve for one task id as a subprocess.

    Returns (updates, error) where updates may carry `returncode`,
    `stdout_tail`/`stderr_tail` (attribution) and `resolve` (the full spawn
    payload). error is None on a clean resolve — including the spawn's own
    fail-open {"error": ...} shape, which is returned as data, not raised.
    """
    argv = [*spawn_cmd, '--from-task', str(task_id),
            '--board', board, '--format', 'json']
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {'returncode': None}, f'spawn timed out after {timeout_s}s'
    except OSError as e:
        return {'returncode': None}, f'{type(e).__name__}: {e}'
    updates = {'returncode': p.returncode}
    if p.returncode != 0:
        # router_spawn is fail-open (exit 0); nonzero means it crashed hard.
        updates['stdout_tail'] = (p.stdout or '')[-400:]
        updates['stderr_tail'] = (p.stderr or '')[-400:]
        return updates, f'spawn exited {p.returncode}'
    try:
        payload = json.loads(p.stdout)
    except ValueError:
        updates['stdout_tail'] = (p.stdout or '')[-400:]
        return updates, 'spawn stdout is not valid JSON'
    if not isinstance(payload, dict):
        return updates, 'spawn stdout JSON is not an object'
    updates['resolve'] = payload
    return updates, None


def _chain_head_of(payload):
    """(provider, model, usd_1m) of the resolve's head hop, or None."""
    head = payload.get('head')
    if not isinstance(head, dict) or not head.get('provider'):
        return None
    return {'provider': head.get('provider'),
            'model': head.get('model'),
            'usd_1m': head.get('usd_1m')}


class BoardIntake:
    """Tails the board JSONL and resolves every new pending task id once."""

    def __init__(self, board, state_dir, spawn_cmd,
                 timeout_s=RESOLVE_TIMEOUT_S, log=None):
        self.board = board
        self.state_dir = state_dir
        self.spawn_cmd = list(spawn_cmd)
        self.timeout_s = timeout_s
        self.seen_file = seen_path(state_dir)
        self.resolves_file = resolves_path(state_dir)
        self.seen = load_seen(self.seen_file)
        self.fresh_state = not self.seen
        self._offset = 0
        self._pending = b''
        self._stat = None
        self._board_error_reported = False
        self.log = log or (lambda msg: print(f'[intake] {msg}', file=sys.stderr))
        self._reconcile_claims()

    # -- state helpers ------------------------------------------------------

    def _reconcile_claims(self):
        """A claim without a completion = the daemon died mid-resolve.

        Fail-open + never-quiet: emit the error record the crash prevented,
        then complete the claim so the id is never re-resolved.
        """
        for tid, row in self.seen.items():
            if row.get('source') != 'claim' or row.get('resolved_at'):
                continue
            ts = now_iso()
            append_jsonl(self.resolves_file, {
                'id': tid, 'ts': ts, 'source': 'board-intake-recovery',
                'resolved_at': ts, 'chain_head': None,
                'complexity_source': None,
                'error': 'resolve interrupted before completion '
                         '(daemon restart); claim found in intake state',
            })
            done = dict(row, resolved_at=ts, source='claim-completed')
            append_jsonl(self.seen_file, done)
            self.seen[tid] = done
            self.log(f'recovered dangling claim for {tid} (error recorded)')

    def _claim(self, tid):
        """Mark an id claimed BEFORE spawning, so a crash can never cause a
        second resolve of the same task (at-most-once intake)."""
        row = {'id': str(tid), 'ts': now_iso(),
               'resolved_at': None, 'source': 'claim'}
        append_jsonl(self.seen_file, row)
        self.seen[str(tid)] = row
        return row['ts']

    def _complete_claim(self, tid, claim_ts, resolved_at, outcome):
        row = {'id': str(tid), 'ts': claim_ts, 'resolved_at': resolved_at,
               'source': 'claim-completed', 'outcome': outcome}
        append_jsonl(self.seen_file, row)
        self.seen[str(tid)] = row

    # -- board reading ------------------------------------------------------

    def _read_new_bytes(self, backlog=False):
        """Return the complete-line text since the tracked offset, and
        advance it. Bytes of a trailing line without a newline are NOT
        dropped: they are carried in `_pending` and prepended to the next
        read, so a row written across two polls (writer mid-append at poll
        time) is reassembled instead of being lost. A shrunken board
        (rewrite/truncate) resets both the offset and the carry — the seen
        state absorbs the re-read ids."""
        try:
            st = os.stat(self.board)
        except OSError as e:
            self._report_board_error(f'board stat failed: {e}')
            return ''
        size, mtime = st.st_size, st.st_mtime_ns
        if self._stat == (size, mtime):
            return ''
        self._stat = (size, mtime)
        if backlog:
            start = 0
        elif size < self._offset:
            self.log('board shrank — rescanning from 0 (seen state dedupes)')
            start = 0
        else:
            start = self._offset
        try:
            with open(self.board, 'rb') as f:
                f.seek(start)
                chunk = f.read()
        except OSError as e:
            self._report_board_error(f'board read failed: {e}')
            return ''
        self._offset = start + len(chunk)
        data = self._pending + chunk
        cut = data.rfind(b'\n')
        if cut < 0:
            # nothing complete yet — keep carrying, consume nothing
            self._pending = data
            return ''
        self._pending = data[cut + 1:]
        return data[:cut + 1].decode('utf-8', errors='replace')

    def _report_board_error(self, msg):
        # Board-level failures are logged every time but recorded ONCE per
        # incident in the resolves ledger — a per-poll record would just spam
        # the ledger while the board is briefly absent (visibility, not noise).
        self.log(f'ERROR: {msg}')
        if not self._board_error_reported:
            self._board_error_reported = True
            append_jsonl(self.resolves_file, {
                'id': None, 'ts': now_iso(), 'source': 'board-intake',
                'resolved_at': None, 'chain_head': None,
                'complexity_source': None, 'error': msg,
            })

    def _iter_rows(self, text):
        """Tolerant JSONL parse: blank/malformed lines are skipped, never
        fatal (the board is written by appenders; a torn line must not take
        the intake down)."""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                self.log('skipping malformed board line')
                yield None
                continue
            if not isinstance(row, dict):
                self.log('skipping non-object board line')
                yield None
                continue
            yield row

    # -- the intake step ----------------------------------------------------

    def _resolve_one(self, tid, claim_ts):
        updates, err = run_resolve(self.spawn_cmd, self.board, tid,
                                   timeout_s=self.timeout_s)
        resolved_at = now_iso()
        payload = updates.pop('resolve', None)
        rec = {'id': str(tid), 'ts': claim_ts, 'source': 'board-intake',
               'resolved_at': resolved_at, 'chain_head': None,
               'complexity_source': None}
        rec.update(updates)
        if payload is not None:
            rec['chain_head'] = _chain_head_of(payload)
            rec['complexity_source'] = (payload.get('complexity') or {}).get(
                'source')
            rec['resolve'] = payload
            if payload.get('error'):
                # The spawn's own fail-open shape (exit 0): resolved, but the
                # ledger must show WHY there is no chain.
                rec['error'] = f"resolve reported: {payload['error']}"
        if err is not None:
            rec['error'] = err
        append_jsonl(self.resolves_file, rec)
        self._complete_claim(tid, claim_ts, resolved_at,
                             'error' if err else 'ok')
        return rec

    def poll(self, backlog=False):
        """One pass over the board. Returns a tally dict; never raises."""
        tally = {'new': 0, 'resolved': 0, 'errors': 0, 'skipped': 0}
        text = self._read_new_bytes(backlog=backlog)
        if not text:
            return tally
        for row in self._iter_rows(text):
            if row is None:
                tally['skipped'] += 1
                continue
            tid = row.get('id')
            if not tid or row.get('status') != PENDING_STATUS:
                # Not a submission event (updated/complete rows, fixtures):
                # not an error, just not intake's business.
                tally['skipped'] += 1
                continue
            tid = str(tid)
            if tid in self.seen:
                tally['skipped'] += 1
                continue
            tally['new'] += 1
            claim_ts = self._claim(tid)
            try:
                rec = self._resolve_one(tid, claim_ts)
            except Exception as e:  # fail-open: the loop must survive anything
                rec = {'id': tid, 'ts': claim_ts, 'source': 'board-intake',
                       'resolved_at': now_iso(), 'chain_head': None,
                       'complexity_source': None,
                       'error': f'{type(e).__name__}: {e}'}
                append_jsonl(self.resolves_file, rec)
                self._complete_claim(tid, claim_ts, rec['resolved_at'],
                                     'error')
            if rec.get('error'):
                tally['errors'] += 1
                self.log(f"{tid}: resolve FAILED — {rec['error']}")
            else:
                tally['resolved'] += 1
                head = rec.get('chain_head') or {}
                self.log(f"{tid}: resolved -> "
                         f"{head.get('provider')}/{head.get('model')} "
                         f"(complexity_source={rec.get('complexity_source')})")
        return tally

    def seed(self):
        """First-boot baseline: mark every id already on the board as seen
        WITHOUT resolving it. Returns how many ids were seeded."""
        try:
            with open(self.board, 'rb') as f:
                raw = f.read()
        except OSError as e:
            self._report_board_error(f'seed read failed: {e}')
            return 0
        count = 0
        for row in self._iter_rows(raw.decode('utf-8', errors='replace')):
            tid = row.get('id') if row else None
            if not tid or str(tid) in self.seen:
                continue
            entry = {'id': str(tid), 'ts': now_iso(),
                     'resolved_at': None, 'source': 'seed'}
            append_jsonl(self.seen_file, entry)
            self.seen[str(tid)] = entry
            count += 1
        return count


def _env_float(name, default):
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f'[intake] ignoring non-numeric {name}={raw!r} — using {default}',
              file=sys.stderr)
        return default


def build_spawn_cmd(override):
    """Base argv for the resolve subprocess. Default: THIS interpreter on the
    repo's router_spawn.py — under the documented unit the interpreter IS the
    board venv python, reproducing the manual command exactly."""
    raw = override or os.environ.get(ENV_SPAWN_CMD, '').strip()
    if raw:
        return shlex.split(raw)
    return [sys.executable, DEFAULT_SPAWN_SCRIPT]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='TR-298 automatic board task intake — resolve every new '
                    'pending board task through the existing --from-task path')
    ap.add_argument('--board', default=None,
                    help=f'board tasks.jsonl path '
                         f'(default: {DEFAULT_BOARD}; env {ENV_BOARD})')
    ap.add_argument('--state-dir', default=None,
                    help=f'state dir for the intake ledgers '
                         f'(default: {DEFAULT_STATE_DIR}; env {ENV_STATE_DIR})')
    ap.add_argument('--interval-s', type=float, default=None,
                    help=f'poll interval in seconds (default: 30; '
                         f'env {ENV_INTERVAL_S})')
    ap.add_argument('--spawn-cmd', default=None,
                    help='base resolve command to extend per task '
                         '(default: this python + scripts/router_spawn.py; '
                         f'env {ENV_SPAWN_CMD})')
    ap.add_argument('--timeout-s', type=float, default=RESOLVE_TIMEOUT_S,
                    help='per-resolve subprocess budget '
                         f'(default: {RESOLVE_TIMEOUT_S}s)')
    ap.add_argument('--once', action='store_true',
                    help='drain the CURRENT backlog (every pending id the '
                         'state does not know) and exit — no cold-start '
                         'seeding; testing/CI mode')
    args = ap.parse_args(argv)

    board = args.board or os.environ.get(ENV_BOARD, '').strip() or DEFAULT_BOARD
    state_dir = (args.state_dir or os.environ.get(ENV_STATE_DIR, '').strip()
                 or DEFAULT_STATE_DIR)
    interval = (args.interval_s if args.interval_s is not None
                else _env_float(ENV_INTERVAL_S, 30.0))
    if interval <= 0:
        interval = 30.0

    intake = BoardIntake(board=board, state_dir=state_dir,
                         spawn_cmd=build_spawn_cmd(args.spawn_cmd),
                         timeout_s=args.timeout_s)

    if args.once:
        tally = intake.poll(backlog=True)
        intake.log(f"once: backlog drained — new={tally['new']} "
                   f"resolved={tally['resolved']} errors={tally['errors']} "
                   f"skipped={tally['skipped']}")
        return 0

    if intake.fresh_state:
        seeded = intake.seed()
        intake.log(f'first boot: seeded {seeded} existing board ids as seen '
                   f'(not resolved) — intake starts with NEW submissions')

    intake.log(f'watching {board} every {interval:g}s '
               f'(state: {state_dir})')
    while True:
        try:
            tally = intake.poll()
            if tally['new']:
                intake.log(f"poll: new={tally['new']} "
                           f"resolved={tally['resolved']} "
                           f"errors={tally['errors']}")
        except Exception as e:  # fail-open is sacred (AGENTS.md)
            intake.log(f'poll failed (continuing): {type(e).__name__}: {e}')
        time.sleep(interval)


if __name__ == '__main__':
    sys.exit(main())
