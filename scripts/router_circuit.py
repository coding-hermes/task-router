#!/usr/bin/env python3
"""router_circuit.py — circuit-breaker state for (provider, model) pairs.

Shared state file: ~/.hermes/model-router/circuit-state.json (override with
ROUTER_STATE_DIR for hermetic tests / scratch runs).
Cooldowns: 1st failure 5m, then double per consecutive failure, cap 1h.
A pair with open_until in the future is excluded from chains (router_spawn.py).

State semantics (documented, TR-024):
  OPEN    — open_until in the future: the pair is EXCLUDED from chains.
  cooling — open_until in the past (or absent): recorded history, no longer
            excluded; the breaker has cooled down and the pair is eligible
            again. Cooling entries are retained for audit until cleared.

Concurrency (TR-027 hardening):
  Every read-modify-write (record-failure / record-success / clear) runs under
  an advisory flock (fcntl.flock LOCK_EX) on a `circuit-state.json.lock`
  sibling. Choice: BLOCKING lock, documented — the critical section is
  sub-millisecond to low-millisecond (load JSON -> mutate -> write temp ->
  os.replace), and the kernel releases the lock automatically on process
  death, so a crashed holder can never deadlock a waiter. The scheduler path
  is fire-and-forget: the worst case is a brief block, never a deadlock, and
  never a partial write. FAIL-OPEN: on any unexpected error the command exits
  1 with a clean message (the Go scheduler treats nonzero as "not recorded"
  and moves on).
  Writes are crash-safe: unique temp name (tempfile.mkstemp), fsync before
  os.replace, dir fsync after. NO shared fixed .tmp path — the old
  `STATE + '.tmp'` single-path rename was a lost-update + FileNotFoundError
  race (Sol's 40-process probe: 7 events silently lost).

Pruning (TR-027):
  Expired pairs (open_until in the past) are pruned on EVERY WRITE. Status is
  deliberately read-only (TR-024 contract: status --json lists cooling pairs)
  — a stale entry lives until the next write, which happens on every
  scheduler record call. The pair being written is never pruned first
  (re-failure after natural cooldown continues the streak).

CLI (argparse, TR-027):
  record-failure <provider> <model> [reason...]
  record-success <provider> <model>
  status [provider] [--json]
  clear <provider> <model> | --all
  Exit codes: 0 ok, 2 usage error (argparse). --help on every subcommand.
  Positional forms are unchanged from the pre-argparse CLI — the scheduler
  Go client (circuit_client.go) invokes record-failure/record-success with
  the exact same argv.
"""
import argparse
import datetime
import fcntl
import json
import os
import sys
import tempfile
import time

STATE = os.path.join(os.environ.get('ROUTER_STATE_DIR',
                     os.path.expanduser('~/.hermes/model-router')),
                     'circuit-state.json')
BASE_COOLDOWN_S = 300   # 5m
MAX_COOLDOWN_S = 3600   # 1h


def load():
    """Read the state file. Tolerant: missing/corrupt -> fresh state."""
    try:
        return json.load(open(STATE))
    except Exception:
        return {'version': 1, 'pairs': {}}


def _fsync_dir(path):
    try:
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # some filesystems don't support dir fsync — best effort


def save(st):
    """Atomic crash-safe write: unique temp + fsync + os.replace + dir fsync."""
    d = os.path.dirname(STATE)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(STATE) + '.',
                               suffix='.tmp', dir=d)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(st, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE)
        _fsync_dir(STATE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _acquire_lock():
    """Blocking advisory lock on STATE + '.lock'.

    Kernel releases the flock on process death -> no deadlock possible; the
    critical section is ms-scale, so the fire-and-forget scheduler path is
    never blocked for more than a moment (fail-open preserved).
    """
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    lf = open(STATE + '.lock', 'a+')
    fcntl.flock(lf, fcntl.LOCK_EX)
    return lf


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _prune_expired(st, now, keep=None):
    """Remove cooling pairs whose open_until is in the past.

    keep: key that must survive pruning (the pair being written — its streak
    continues across a natural cooldown).
    """
    pairs = st['pairs']
    for k in [k for k in pairs if k != keep]:
        ou = pairs[k].get('open_until')
        if ou and ou < now:
            del pairs[k]


def record_failure(provider, model, reason=''):
    key = f'{provider}/{model}'
    lf = _acquire_lock()
    try:
        st = load()
        now = now_iso()
        _prune_expired(st, now, keep=key)
        c = st['pairs'].setdefault(key, {'failures': 0, 'open_until': None,
                                         'last_failure': None, 'reason': ''})
        c['failures'] = c.get('failures', 0) + 1
        cd = min(BASE_COOLDOWN_S * (2 ** (c['failures'] - 1)), MAX_COOLDOWN_S)
        c['open_until'] = (datetime.datetime.now(datetime.timezone.utc) +
                           datetime.timedelta(seconds=cd)).isoformat(timespec='seconds')
        c['last_failure'] = now
        if reason:
            c['reason'] = reason
        save(st)
    finally:
        lf.close()
    print(f'OPEN {key} — {c["failures"]} consecutive failures, cooldown {cd}s, open until {c["open_until"]}')
    return 0


def record_success(provider, model):
    key = f'{provider}/{model}'
    lf = _acquire_lock()
    try:
        st = load()
        _prune_expired(st, now_iso())
        if key in st['pairs']:
            prev = st['pairs'].pop(key)
            print(f'CLOSED {key} — was open until {prev.get("open_until")} ({prev.get("failures")} failures)')
            save(st)
        else:
            print(f'{key} already closed')
    finally:
        lf.close()
    return 0


def status(provider=None, as_json=False):
    # Read-only: no lock needed (os.replace makes state visibility atomic),
    # and deliberately NO pruning — TR-024 contract: status --json lists
    # cooling pairs. Expired entries are pruned on the next write.
    st = load()
    now = now_iso()
    pairs = st['pairs']
    if provider:
        pairs = {k: v for k, v in pairs.items() if k.startswith(provider + '/')}
    if as_json:
        print(json.dumps({
            'pairs': [{'pair': k, 'state': 'OPEN' if (v.get('open_until') and v['open_until'] > now)
                       else 'cooling',
                       'failures': v.get('failures', 0),
                       'open_until': v.get('open_until'),
                       'reason': v.get('reason', '')}
                      for k, v in sorted(pairs.items())],
        }, indent=1))
        return 0
    if not pairs:
        print('no open/recorded circuits')
        return 0
    for key, c in sorted(pairs.items()):
        state = 'OPEN' if c.get('open_until') and c['open_until'] > now else 'cooling'
        print(f'{key:<42} {state:<8} failures={c.get("failures", 0)} open_until={c.get("open_until")} {c.get("reason", "")}')
    return 0


def clear(provider=None, model=None, all_=False):
    lf = _acquire_lock()
    try:
        st = load()
        _prune_expired(st, now_iso())
        if all_:
            st['pairs'] = {}
            save(st)
            print('cleared all circuits')
            return 0
        key = f'{provider}/{model}'
        if key in st['pairs']:
            del st['pairs'][key]
            save(st)
            print(f'cleared {key}')
        else:
            print(f'{key} not recorded')
    finally:
        lf.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='router_circuit.py',
        description='Circuit-breaker state for (provider, model) pairs. '
                    'Fail-open: never blocks the scheduler.')
    sub = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)

    pf = sub.add_parser('record-failure', help='open (or extend) the circuit for a pair')
    pf.add_argument('provider')
    pf.add_argument('model')
    pf.add_argument('reason', nargs='*', default='',
                    help='optional failure reason (multiple words are joined)')
    pf.set_defaults(func=lambda a: record_failure(a.provider, a.model,
                                                  ' '.join(a.reason)))

    ps = sub.add_parser('record-success', help='close the circuit for a pair')
    ps.add_argument('provider')
    ps.add_argument('model')
    ps.set_defaults(func=lambda a: record_success(a.provider, a.model))

    pst = sub.add_parser('status', help='show recorded circuits (open + cooling)')
    pst.add_argument('provider', nargs='?', default=None)
    pst.add_argument('--json', action='store_true', help='machine-readable output')
    pst.set_defaults(func=lambda a: status(a.provider, as_json=a.json))

    pc = sub.add_parser('clear', help='remove a pair or all circuits')
    pc.add_argument('provider', nargs='?', default=None)
    pc.add_argument('model', nargs='?', default=None)
    pc.add_argument('--all', action='store_true', dest='all_', help='clear every circuit')
    pc.set_defaults(func=lambda a: clear(a.provider, a.model, all_=a.all_))

    a = parser.parse_args(argv)
    if a.command == 'clear':
        if a.all_ and (a.provider or a.model):
            parser.error('--all takes no provider/model')
        if not a.all_ and not (a.provider and a.model):
            parser.error('clear needs <provider> <model> or --all')
    try:
        return a.func(a)
    except Exception as e:  # fail-open: clean exit, never a traceback
        print(f'router_circuit error: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
