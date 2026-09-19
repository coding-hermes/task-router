#!/usr/bin/env python3
"""router_quota.py — plan-window quota gates for the task router (TR-060).

WHY (live audit 2026-09-17): a subscription lane that answers 429
'usage limit has been reached' (OpenAI/ChatGPT plan) or 'Weekly/Monthly Limit
Exhausted. Your limit will reset at <ts>' (zai-glm) is exhausted for HOURS or
DAYS, but the only automatic reaction was an api_down circuit that cools in 30
minutes. So the resolver re-picked the dead lane on the next tick, it re-failed,
and every affected fleet session fell back to the PAYG deepseek default
(276/276 fallback sessions in the audit window). This tool records the plan
window as a STATE GATE with the provider's own reset time, so the head advances
to the next eligible lane and the gate EXPIRES BY ITSELF when the plan refills.

STATE (same file the scheduler's spawn path reads):
  <ROUTER_STATE_DIR>/quota-state.json   (default ~/.hermes/model-router/)
  "quota_exhausted": {"<provider>": {"status": "gated", "reason": "<why>",
                                     "reset_at": "<ISO ts>",
                                     "detected_at": "<ISO ts>"}}
The resolver (scripts/router_spawn.py `load_quota_gates`) excludes a provider
while status is not open/cleared/expired AND reset_at is in the future (or
missing). A reset_at in the PAST is auto-cleared — the entry stays for audit
and NOTHING has to be edited for the lane to return. A hand-written
providers.<p>.quota_exhausted object is honored as well.

`router quota ...` (task_router.cli) deliberately does NOT redirect
ROUTER_STATE_DIR to the data home for this command: the fleet's spawn path
invokes scripts/router_spawn.py directly and reads the default state dir, so a
gate written into the data-home bootstrap sample would never take effect. Use
--state-file/--state-dir to target a different file explicitly (tests, scratch).

CLI:
  set <provider> <reason> <reset-at> [--detected-at ISO] [--state-file P]
        [--state-dir D] [--json]
  clear (<provider> | --all) [--state-file P] [--state-dir D] [--json]
  status [<provider>] [--json] [--state-file P] [--state-dir D]
Exit codes: 0 ok, 2 usage/validation error (operator error — never masked),
1 unexpected failure (clean stderr message, no traceback).
"""
import argparse
import datetime
import fcntl
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.realpath(__file__))
# Identical resolution to router_spawn.py: the state dir the SPAWN path reads.
STATE_DIR = os.environ.get('ROUTER_STATE_DIR',
                           os.path.expanduser('~/.hermes/model-router'))
QUOTA_FILE = 'quota-state.json'
GATED_STATUS = 'gated'


def state_file(state_file_arg=None, state_dir_arg=None):
    """Resolved path of the quota state file (explicit args win, in order)."""
    if state_file_arg:
        return os.path.abspath(os.path.expanduser(state_file_arg))
    if state_dir_arg:
        return os.path.join(os.path.abspath(os.path.expanduser(state_dir_arg)),
                            QUOTA_FILE)
    return os.path.join(STATE_DIR, QUOTA_FILE)


def load(path):
    """Read the state file. Tolerant: missing/corrupt -> minimal document."""
    try:
        doc = json.load(open(path))
        if isinstance(doc, dict):
            return doc
    except Exception:
        pass
    return {'updated': None, 'providers': {}}


def _fsync_dir(path):
    try:
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # some filesystems don't support dir fsync — best effort


def save(path, doc):
    """Atomic crash-safe write: unique temp + fsync + os.replace + dir fsync.

    Never truncates the live file (a torn quota-state.json fails OPEN — the
    resolver would drop every gate at once), and the indent=1 style matches
    router_circuit.py's state writer.
    """
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + '.',
                               suffix='.tmp', dir=d)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(doc, f, indent=1)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Lock:
    """Blocking advisory lock on <state>.lock (same policy as router_circuit).

    Blocking (not non-blocking) is deliberate: the critical section is a
    sub-millisecond read-modify-write, the kernel drops the flock on process
    death (no deadlock), and a lost update here would silently drop a gate.
    """

    def __init__(self, path):
        self.path = path + '.lock'
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        self.fh = open(self.path, 'a+')
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            self.fh.close()
            self.fh = None
        return False


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def parse_reset(value):
    """ISO-8601 (naive -> UTC) or an integer epoch (seconds) -> ISO string.

    Raises ValueError with an operator-readable message. The epoch form is
    accepted because provider 429 bodies carry it verbatim
    (OpenAI: `'resets_at': 1789805473`), so the evidence can be pasted
    straight into the gate.
    """
    raw = str(value).strip()
    if not raw:
        raise ValueError('reset-at is empty')
    if raw.lstrip('-').isdigit():
        try:
            dt = datetime.datetime.fromtimestamp(int(raw), datetime.timezone.utc)
        except (OverflowError, OSError, ValueError) as e:
            raise ValueError(f'reset-at epoch {raw!r} is not a usable timestamp: {e}')
        return dt.isoformat(timespec='seconds')
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f'reset-at {raw!r} is not ISO-8601 '
                         "(expected e.g. 2026-09-20T04:07:32+00:00) "
                         'or an integer epoch')
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec='seconds')


def _validate_provider(parser, provider):
    if not provider or not str(provider).strip():
        parser.error('provider is empty')
    prov = str(provider).strip()
    if '/' in prov or any(c.isspace() for c in prov):
        parser.error(f'provider {prov!r} must be a bare provider id '
                     "(e.g. zai-glm) — pairs ('provider/model') are not gated here")
    return prov


def _resolver_helpers():
    """(load_quota_gates, quota_gate_summary) from router_spawn.py, or (None, None).

    Reused so `router quota status` classifies EXACTLY like the resolver does
    (single source of truth for GATED vs auto-cleared). A failed import must
    never break the operator tool — status degrades to raw entries.
    """
    try:
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import router_spawn
        return router_spawn.load_quota_gates, router_spawn.quota_gate_summary
    except Exception:
        return None, None


def _touch_updated(doc):
    """Stamp the document's `updated` date; retire a stale bootstrap note."""
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if doc.get('updated') == 'bootstrap':
        doc['note'] = ('first-run bootstrap providers preserved; plan-window '
                       'quota_exhausted gates written by `router quota`')
    doc['updated'] = today
    return today


# ------------------------------------------------------------------- set ----


def quota_set(provider, reason, reset_at, detected_at=None, state_file_arg=None,
              state_dir_arg=None, as_json=False):
    path = state_file(state_file_arg, state_dir_arg)
    reset_iso = parse_reset(reset_at)          # ValueError -> main() exit 2
    detected = now_iso()
    if detected_at:
        detected = parse_reset(detected_at)
    warn = None
    if reset_iso <= now_iso():
        warn = (f'WARNING: reset_at {reset_iso} is in the PAST — the gate is '
                'recorded for audit but auto-clears immediately (the plan '
                'window has already refilled).')
    with _Lock(path):
        doc = load(path)
        section = doc.get('quota_exhausted')
        if not isinstance(section, dict):
            section = {}
        entry = {'status': GATED_STATUS, 'reason': str(reason).strip(),
                 'reset_at': reset_iso, 'detected_at': detected}
        section[str(provider)] = entry
        doc['quota_exhausted'] = {k: section[k] for k in sorted(section)}
        _touch_updated(doc)
        save(path, doc)
    if warn:
        print(warn, file=sys.stderr)
    out = {'state_file': path, 'action': 'set', 'provider': provider,
           'entry': entry, 'updated': doc.get('updated')}
    if as_json:
        print(json.dumps(out, indent=1))
    else:
        print(f'GATED {provider} — quota exhausted: {entry["reason"]} '
              f'(resets {reset_iso}); detected_at={detected}')
        print(f'  state: {path}')
    return 0


# ----------------------------------------------------------------- clear ----


def quota_clear(provider=None, all_=False, state_file_arg=None,
                state_dir_arg=None, as_json=False):
    path = state_file(state_file_arg, state_dir_arg)
    removed = []
    with _Lock(path):
        doc = load(path)
        section = doc.get('quota_exhausted')
        section = dict(section) if isinstance(section, dict) else {}
        if all_:
            removed = sorted(section)
            if section:
                doc.pop('quota_exhausted', None)
        elif provider in section:
            removed = [provider]
            del section[provider]
            if section:
                doc['quota_exhausted'] = {k: section[k] for k in sorted(section)}
            else:
                doc.pop('quota_exhausted', None)
        else:
            # a hand-written NESTED gate is the operator's other spelling —
            # clear it too, or `quota clear` would silently lie
            provs = doc.get('providers')
            if isinstance(provs, dict) and isinstance(provs.get(provider), dict) \
                    and 'quota_exhausted' in provs[provider]:
                del provs[provider]['quota_exhausted']
                removed = [provider]
        if removed:
            _touch_updated(doc)
            save(path, doc)
    out = {'state_file': path, 'action': 'clear', 'cleared': removed}
    if as_json:
        print(json.dumps(out, indent=1))
    elif removed:
        print(f'cleared plan-window quota gate(s): {", ".join(removed)}')
        print(f'  state: {path}')
    else:
        target = 'all providers' if all_ else str(provider)
        print(f'no plan-window quota gate recorded for {target} ({path})')
    return 0


# ---------------------------------------------------------------- status ----


def quota_status(provider=None, state_file_arg=None, state_dir_arg=None,
                 as_json=False):
    path = state_file(state_file_arg, state_dir_arg)
    doc = load(path)
    present = os.path.exists(path)
    load_quota_gates, quota_gate_summary = _resolver_helpers()
    if load_quota_gates is not None:
        gates = load_quota_gates(doc)
        summary = quota_gate_summary(gates)
        gated, expired = summary['gated'], summary['expired']
        # An entry that is neither gated nor auto-cleared carries an explicit
        # open/cleared status — report it as OPEN instead of hiding it (a
        # recorded-but-open lane must not look like a missing entry).
        open_ = [{'provider': g['provider'], 'status': g['status'],
                  'reason': g['reason'], 'reset_at': g['reset_at'],
                  'detected_at': g['detected_at']}
                 for _p, g in sorted(gates.items())
                 if not g['active'] and not g['expired']]
        considered = sorted(gates)
    else:  # degrade: raw entries, no classification (never a traceback)
        section = doc.get('quota_exhausted')
        section = section if isinstance(section, dict) else {}
        gated, expired, open_ = [], [], []
        for prov in sorted(section):
            ent = section[prov] if isinstance(section[prov], dict) else {}
            row = {'provider': prov, 'status': ent.get('status'),
                   'reason': ent.get('reason'), 'reset_at': ent.get('reset_at'),
                   'detected_at': ent.get('detected_at')}
            gated.append(row)
        considered = list(section)
    if provider:
        keep = lambda rows: [r for r in rows if r.get('provider') == provider]
        gated, expired, open_ = keep(gated), keep(expired), keep(open_)
        considered = [p for p in considered if p == provider]
    out = {'state_file': path, 'present': present,
           'providers_considered': considered,
           'gated': gated, 'expired': expired, 'open': open_}
    if as_json:
        print(json.dumps(out, indent=1))
        return 0
    print(f'plan-window quota gates — {path}')
    if not present:
        print('  (state file absent — nothing is gated; fail-open)')
        return 0
    if not considered:
        print('  no quota_exhausted entries recorded')
        return 0
    for row in gated:
        print(f"  {row.get('provider'):<20} GATED    "
              f"reset_at={row.get('reset_at') or '-'} "
              f"detected_at={row.get('detected_at') or '-'} "
              f"{row.get('reason') or ''}")
    for row in expired:
        print(f"  {row.get('provider'):<20} expired  (auto-cleared; was gated "
              f"until {row.get('reset_at') or '-'})")
    for row in open_:
        print(f"  {row.get('provider'):<20} open     (recorded, not gated)")
    return 0


# ------------------------------------------------------------------ main ----


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog='router_quota.py',
        description='Plan-window quota gates (429 exhaustion) in quota-state.json. '
                    'A gate excludes the provider until its reset_at passes.')
    sub = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)

    def add_target(p):
        p.add_argument('--state-file', dest='state_file', default=None,
                       help=f'explicit state file (default: '
                            f'$ROUTER_STATE_DIR/{QUOTA_FILE})')
        p.add_argument('--state-dir', dest='state_dir', default=None,
                       help='state directory holding quota-state.json')

    ps = sub.add_parser('set', help='record a plan-window gate for a provider')
    ps.add_argument('provider')
    ps.add_argument('reason', help='why (quote it — goes into the gate_reason)')
    ps.add_argument('reset_at', help='plan reset: ISO-8601 (tz optional) or epoch seconds')
    ps.add_argument('--detected-at', dest='detected_at', default=None,
                    help='when the 429 was observed (default: now)')
    ps.add_argument('--json', action='store_true')
    add_target(ps)

    pc = sub.add_parser('clear', help='remove a plan-window gate (re-enable the lane)')
    pc.add_argument('provider', nargs='?', default=None)
    pc.add_argument('--all', action='store_true', dest='all_',
                    help='clear every plan-window gate')
    pc.add_argument('--json', action='store_true')
    add_target(pc)

    pst = sub.add_parser('status', help='show recorded plan-window gates')
    pst.add_argument('provider', nargs='?', default=None)
    pst.add_argument('--json', action='store_true')
    add_target(pst)

    a = parser.parse_args(argv)
    try:
        if a.command == 'set':
            return quota_set(_validate_provider(parser, a.provider), a.reason,
                             a.reset_at, detected_at=a.detected_at,
                             state_file_arg=a.state_file, state_dir_arg=a.state_dir,
                             as_json=a.json)
        if a.command == 'clear':
            if a.all_ and a.provider:
                parser.error('--all takes no provider')
            if not a.all_ and not a.provider:
                parser.error('clear needs <provider> or --all')
            prov = _validate_provider(parser, a.provider) if a.provider else None
            return quota_clear(prov, all_=a.all_, state_file_arg=a.state_file,
                               state_dir_arg=a.state_dir, as_json=a.json)
        prov = _validate_provider(parser, a.provider) if a.provider else None
        return quota_status(prov, state_file_arg=a.state_file,
                            state_dir_arg=a.state_dir, as_json=a.json)
    except ValueError as e:
        # Operator input error: the parser's own error channel (exit 2), so a
        # typo is DETECTABLE and never reads as success.
        parser.error(str(e))
    except Exception as e:  # fail-open: clean message, never a traceback
        print(f'router_quota error: {type(e).__name__}: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
