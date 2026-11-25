#!/usr/bin/env python3
"""intake_supervise.py — TR-367 supervision bootstrap for board_task_intake.py.

The intake daemon stopped resolving on 2026-10-13 not because the daemon
script broke (its suite stayed green) but because nothing RAN it: every
supervision asset lived only as an inline snippet in
docs/board-task-intake.md, so when the polling process died there was no
installed unit and nothing on the tree to restart it. This module IS the
committed supervision asset set:

    check    offline validation of the wiring (unit committed, parses, the
             ExecStart points at the real daemon with board/state args,
             restart semantics + install target present, arm step documented)
    arm      install the unit into ~/.config/systemd/user/ (or --unit-dir),
             daemon-reload, enable --now — the explicit operator step; refuses
             (fail-closed) when the referenced board/daemon paths are missing
    status   is-active/is-enabled + the last resolves-ledger line

Stdlib only (board venv). `arm` is the only state-changing command and is
never run implicitly: this repo ships wiring; humans or a foreman fire `arm`
on a host that wants the daemon after review.
"""
from __future__ import annotations
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNIT_NAME = 'task-router-intake.service'
UNIT_FILE = REPO / 'scripts' / UNIT_NAME
DAEMON_NAME = 'board_task_intake.py'
#: Unit-dir override for sandboxed/CI arming (and for the operator who wants
#: a different XDG user unit dir).
DEFAULT_UNIT_DIR = Path(os.path.expanduser('~/.config/systemd/user'))
#: systemctl failure exit codes are 1..8; any other rc is an execution error.
SYSTEMCTL_RANGE = range(0, 9)


def parse_ini(path: Path) -> dict:
    """Minimal systemd-unit reader: {section: {key: value}}. Lines without
    '=' in a section (and stray text) are recorded as '' keys — never fatal,
    a check must be able to NAME the defect, not crash on it."""
    out: dict = {}
    section = None
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        if section is None:
            out.setdefault('<none>', {})[line] = ''
            continue
        key, _, value = line.partition('=')
        out[section][key.strip()] = value.strip()
    return out


def _systemctl(args: list) -> int:
    cmd = ['systemctl', '--user', *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode not in SYSTEMCTL_RANGE:
        print(f'[supervise] systemctl died abnormally (rc={p.returncode}): '
              f'{p.cmd}\n{p.stdout}{p.stderr}', file=sys.stderr)
        return p.returncode
    if p.stdout.strip():
        print(p.stdout.strip())
    return p.returncode


def _repo_of_exec_start(exec_start: str) -> Path | None:
    """The repo dir the unit runs its daemon from (from the ExecStart argv
    path .../<repo>/scripts/board_task_intake.py), or None if not stated."""
    for tok in shlex.split(exec_start):
        if tok.endswith('/' + 'scripts/' + DAEMON_NAME) or \
                tok.endswith('scripts/' + DAEMON_NAME):
            return Path(tok).resolve().parents[1]
    return None


def check() -> int:
    """Offline wiring validation. Exit 0 + a summary on stdout, exit 1 with
    every finding named otherwise. Runs in the bare venv; no filesystem
    writes."""
    findings = []
    if not UNIT_FILE.is_file():
        print(f'FAIL: {UNIT_FILE.relative_to(REPO)} is missing — no committed '
              'supervision wiring for the intake daemon (the TR-367 defect: '
              'a lost process has nothing to restart it).', file=sys.stderr)
        return 1
    if not (REPO / 'scripts' / DAEMON_NAME).is_file():
        findings.append(f'scripts/{DAEMON_NAME} missing from this repo — the '
                        'wiring points at a daemon that is not shipped')
    try:
        units = parse_ini(UNIT_FILE)
    except Exception as exc:  # unreadable/garbled unit
        print(f'FAIL: unit does not parse: {exc}', file=sys.stderr)
        return 1

    def section(key_startswith: str) -> tuple:
        for name, kv in units.items():
            if name.lower() == key_startswith.lower():
                return name, kv
        return key_startswith, {}

    unit_name, unit = section('Unit')
    svc_name, service = section('Service')
    inst_name, install = section('Install')
    if not unit:
        findings.append(f'[{unit_name}] section missing')
    if not service:
        findings.append(f'[{svc_name}] section missing')
    if not install:
        findings.append(f'[{inst_name}] section missing')
    exec_start = service.get('ExecStart', '')
    for needle in (DAEMON_NAME, '--board', '--state-dir', '--interval-s',
                   'tasks.jsonl'):
        if needle not in exec_start:
            findings.append(f'ExecStart does not name {needle!r}')
    exec_py = shlex.split(exec_start)[0] if exec_start else ''
    if 'bin/python' not in exec_py:
        findings.append(f'ExecStart interpreter looks wrong: {exec_py!r}')
    if not service.get('Restart'):
        findings.append('no Restart= policy — a dead daemon stays dead')
    if not service.get('RestartSec'):
        findings.append('no RestartSec= — restart storms possible')
    if install.get('WantedBy', '') != 'default.target':
        findings.append(f"WantedBy invalid: {install.get('WantedBy')!r} "
                        '(user unit must target default.target)')
    text = UNIT_FILE.read_text(encoding='utf-8')
    if 'systemctl --user' not in text:
        findings.append('the arm step is not documented on the unit itself')
    if 'intake_supervise.py' not in text:
        findings.append('the unit does not reference the supervisor wrapper')
    if findings:
        print('FAIL: intake supervision wiring invalid:', file=sys.stderr)
        for f in findings:
            print(f'  - {f}', file=sys.stderr)
        return 1
    repo_of = _repo_of_exec_start(exec_start)
    print('OK: intake supervision wiring valid '
          f'({UNIT_NAME}, daemon scripts/{DAEMON_NAME}, '
          f"repo {repo_of or 'unstated'}, "
          f"restart={service.get('Restart')}/{service.get('RestartSec')})")
    return 0


def arm(unit_dir: str = '') -> int:
    """Install + enable the unit. Fail-closed: `check` must pass and the
    referenced board path must exist BEFORE anything is installed."""
    rc = check()
    if rc != 0:
        return rc
    # resolve the board path from the unit's ExecStart (the same one check
    # validated) and refuse a wrong-repo install — a board that will never
    # be polled must fail here, not as a silently idle daemon.
    exec_start = parse_ini(UNIT_FILE)['Service']['ExecStart']
    board = None
    for tok in shlex.split(exec_start):
        if board is not None and 'tasks.jsonl' in tok:
            board = Path(tok)
            break
        if tok == '--board':
            board = 'next'
    if board is None or not Path(board).is_file():
        return _refuse(f'board path from ExecStart does not exist: {board}')
    target_dir = Path(unit_dir) if unit_dir else DEFAULT_UNIT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / UNIT_NAME
    target.write_text(UNIT_FILE.read_text(encoding='utf-8'),
                      encoding='utf-8')
    if _systemctl(['daemon-reload']) != 0:
        return _refuse('systemctl --user daemon-reload failed')
    if _systemctl(['enable', '--now', UNIT_NAME]) != 0:
        return _refuse('systemctl --user enable --now failed')
    active = subprocess.run(['systemctl', '--user', 'is-active', UNIT_NAME],
                            capture_output=True, text=True)
    print(f'arm: unit installed at {target}; service state: '
          f'{active.stdout.strip() or active.stderr.strip()}')
    return 0 if active.stdout.strip() == 'active' else 1


def _refuse(msg: str) -> int:
    print(f'[supervise] REFUSED: {msg}', file=sys.stderr)
    return 1


def status() -> int:
    if not UNIT_FILE.is_file():
        return _refuse('committable unit missing from scripts/ — run check')
    active = subprocess.run(['systemctl', '--user', 'is-active', UNIT_NAME],
                            capture_output=True, text=True)
    enabled = subprocess.run(['systemctl', '--user', 'is-enabled', UNIT_NAME],
                             capture_output=True, text=True)
    print(f'active: {active.stdout.strip() or active.stderr.strip()}; '
          f'enabled: {enabled.stdout.strip() or enabled.stderr.strip()}')
    ledger = REPO / 'data' / 'state' / 'intake-resolves.jsonl'
    if ledger.is_file():
        lines = ledger.read_text(encoding='utf-8',
                                 errors='replace').splitlines()
        print(f'last resolve ({len(lines)} records): {lines[-1][:160]}…')
    return 0 if active.stdout.strip() == 'active' else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0 if argv else 1
    cmd, args = argv[0], argv[1:]
    if cmd == 'check':
        return check()
    unit_dir = ''
    if '--unit-dir' in args:
        unit_dir = args[args.index('--unit-dir') + 1]
    if cmd == 'arm':
        return arm(unit_dir)
    if cmd == 'status':
        return status()
    print(f'unknown command {cmd!r} — expected check|arm|status',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
