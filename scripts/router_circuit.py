#!/usr/bin/env python3
"""router_circuit.py — circuit-breaker state for (provider, model) pairs.

Shared state file: ~/.hermes/model-router/circuit-state.json (override with
ROUTER_STATE_DIR for hermetic tests / scratch runs).
Cooldowns: flat per failure class (CLASS_COOLDOWN_S, TR-014):
  overload=120s, quota_window=300s, api_down=1800s, out_of_credit=14400s.
No exponential growth: every consecutive failure of the same class re-opens
with the same class cooldown.
A pair with open_until in the future is excluded from chains (router_spawn.py).

State semantics (documented, TR-024):
  OPEN    — open_until in the future: the pair is EXCLUDED from chains.
  cooling — open_until in the past (or absent): recorded history, no longer
            excluded; the breaker has cooled down and the pair is eligible
            again. Cooling entries are retained for audit until cleared.

Circuit breaker v2 (TR-014):
  Failures carry a class: api_down, out_of_credit, quota_window, overload.
  Provider-level breakers open when a provider records >=3 failures of a
  HARD class (api_down or out_of_credit) across ANY of its models within the
  class cooldown window.  A provider-level open excludes ALL lanes of that
  provider, with a longer cooldown than model-level breakers.
  Model-level breakers for overload/quota_window open only the (provider,
  model) pair with a short cooldown (TR-032 soft-gate support).
  Backward-compatible: old state files without the "v2" section keep working;
  new state adds {"v2": {"provider_breakers": {...}, "classes": {...}}} while
  leaving legacy pair entries untouched.

Concurrency (TR-027 hardening):
  Every read-modify-write (record-failure / record-success / clear) runs under
  an advisory flock (fcntl.flock LOCK_EX) on a `circuit-state.json.lock`
  sibling. Choice: BLOCKING lock, documented — the critical section is
  sub-millisecond to low-millisecond (load JSON -> mutate -> write temp ->
  os.replace), and the kernel releases the lock automatically on process death,
  so a crashed holder can never deadlock a waiter. The scheduler path
  is fire-and-forget: the worst case is a brief block, never a deadlock, and
  never a partial write. FAIL-OPEN: on any unexpected error the command exits
  1 with a clean message (the Go scheduler treats nonzero as "not recorded"
  and moves on).
  Writes are crash-safe: unique temp name (tempfile.mkstemp), fsync before
  os.replace, dir fsync after. NO shared fixed .tmp path — the old
  `STATE + '.tmp'` single-path rename was a lost-update + FileNotFoundError
  race (Sol's 40-process probe: 7 events silently lost).

Pruning (TR-027 / TR-014):
  Expired pairs (open_until in the past) are pruned on EVERY WRITE. Status is
  deliberately read-only (TR-024 contract: status --json lists cooling pairs)
  — a stale entry lives until the next write, which happens on every
  scheduler record call. The pair being written is never pruned first
  (re-failure after natural cooldown continues the streak).
  Provider breakers are pruned the same way when their open_until expires.

CLI (argparse, TR-027 / TR-014 / TR-190):
  record-failure <provider> <model> [--class CLASS | --kind KIND] [reason...]
  record-success <provider> <model>
  status [provider] [--json]
  clear <provider> <model> | --all
  Exit codes: 0 ok, 1 runtime error (e.g. unmapped kind/class — actionable
  message on stderr, nothing recorded), 2 usage error (argparse). --help on
  every subcommand. TR-190: --kind resolves free-text failure kinds through
  FAILURE_CLASS_MAP and fails LOUDLY on an unmapped kind — never a silent
  api_down default. A no-hop identity (provider/model 'none', '' etc.) is
  declined before any state write (fail-open exit 0).
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

# TR-014: failure-class taxonomy + cooldowns.  Hard classes can open a
# provider-level breaker; soft classes (overload / quota_window) only ever
# open the specific (provider, model) pair.
HARD_CLASSES = frozenset(('api_down', 'out_of_credit'))
SOFT_CLASSES = frozenset(('overload', 'quota_window'))
CLASSES = frozenset(HARD_CLASSES | SOFT_CLASSES)
CLASS_COOLDOWN_S = {
    'overload': 120,        # 2m  — model-level soft gate
    'quota_window': 300,    # 5m  — model-level rate-window gate
    'api_down': 1800,       # 30m — provider-level hard failure
    'out_of_credit': 14400, # 4h  — provider-level hard failure
}
# Optional per-class override via JSON dict in env.  Invalid JSON is ignored
# (fail-open: defaults keep working).
_ENV_OVERRIDE = os.environ.get('ROUTING_CIRCUIT_COOLDOWN_JSON')
if _ENV_OVERRIDE:
    try:
        _OVERRIDE = json.loads(_ENV_OVERRIDE)
        if isinstance(_OVERRIDE, dict):
            CLASS_COOLDOWN_S = dict(CLASS_COOLDOWN_S)
            for k, v in _OVERRIDE.items():
                if k in CLASS_COOLDOWN_S and isinstance(v, (int, float)):
                    CLASS_COOLDOWN_S[k] = int(v)
    except Exception:
        pass
PROVIDER_FAILURE_THRESHOLD = 3

# ---------------------------------------------------------------------------
# TR-190: the failure-kind -> class mapping as ONE table.
#
# The classification seam (router_server._circuit_class and the proxy's failure
# envelope) emits failure KINDS — "hop-wall-timeout", "429 from upstream",
# "connection refused" — and this table is the single authoritative mapping
# kind -> (class, cooldown window). The class IS the blast radius:
#   overload / quota_window  — SOFT: only the (provider, model) pair gates.
#   api_down / out_of_credit — HARD: >=3 within the window open a
#                               PROVIDER-WIDE breaker.
# "connection refused" -> api_down (provider-wide) is the legitimate case: a
# transport-level refusal really is every lane of that provider failing.
#
# BEFORE this table, record_failure coerced every unrecognized class to
# api_down (the hard default): three slow hops or three rate-limited hops
# removed a whole provider for 30 minutes — the measured mechanism of the
# 2026-09-25 lockup. The seam must never silently invent a class again: an
# unmapped kind is an actionable error (see failure_class_for), and a new kind
# means adding a row HERE and to the test table in tests/test_circuit_v2.py
# (EXPECTED_KIND_MAP), which pins every row's class + window.
FAILURE_CLASS_MAP = {
    'timed out': ('overload', 120),
    'hop-wall-timeout': ('overload', 120),
    'idle-timeout': ('overload', 120),
    'idle-timeout (no bytes for 180s)': ('overload', 120),
    '429': ('quota_window', 300),
    '429 from upstream': ('quota_window', 300),
    'rate limit exceeded': ('quota_window', 300),
    'connection refused': ('api_down', 1800),
}

# A ledger row with no hop is not a provider event. The proxy records such rows
# as (provider, model) = ('none', 'none') with the router's own error text as
# the reason; these placeholder identities must never open a breaker of any
# radius (TR-182: observed live — a breaker for a provider literally called
# `none` gated nothing useful and confused the audit).
NOT_A_LANE_IDENTITIES = frozenset(('', 'none', 'unknown', 'n/a', 'null'))


def failure_class_for(kind=None, fclass=None):
    """Resolve the failure KIND to (class, cooldown_seconds) — loudly.

    Exactly one of `kind` (free text from the failure envelope / --kind) or
    `fclass` (an explicit circuit class / --class) must be given.

    - kind: looked up in FAILURE_CLASS_MAP (case-insensitive, stripped).
      Unmapped text raises ValueError naming the kind and the table to extend
      — NEVER the hard default: a silent api_down fallback is how a typo or a
      new failure kind opened provider-wide breakers (2026-09-25 lockup).
    - fclass: must be a member of CLASSES; unknown values raise the same way
      (the CLI's argparse choices already guard this, and explicit callers get
      the same loud contract).
    """
    if (kind is None) == (fclass is None):
        raise ValueError(
            "failure_class_for needs exactly one of kind= or fclass= "
            f"(got kind={kind!r}, fclass={fclass!r})")
    if fclass is not None:
        c = (fclass or '').strip().lower()
        if c not in CLASSES:
            raise ValueError(
                f"unmapped failure class {fclass!r}: not one of "
                f"{sorted(CLASSES)} — extend FAILURE_CLASS_MAP in "
                f"scripts/router_circuit.py and its test table "
                f"(tests/test_circuit_v2.py EXPECTED_KIND_MAP) for a new kind")
        return c, CLASS_COOLDOWN_S.get(c, BASE_COOLDOWN_S)
    k = (kind or '').strip().lower()
    if not k:
        raise ValueError(
            "failure kind is required: pass --class <"
            f"{'|'.join(sorted(CLASSES))}> for an explicit class, or a "
            "failure kind present in FAILURE_CLASS_MAP")
    if k in FAILURE_CLASS_MAP:
        c, _window = FAILURE_CLASS_MAP[k]
        return c, CLASS_COOLDOWN_S.get(c, BASE_COOLDOWN_S)
    raise ValueError(
        f"unmapped failure kind {kind!r}: no row in FAILURE_CLASS_MAP "
        f"(scripts/router_circuit.py). Add it there AND to the test table "
        f"(tests/test_circuit_v2.py EXPECTED_KIND_MAP), or pass an explicit "
        f"--class {'|'.join(sorted(CLASSES))}. Refusing to default to "
        f"api_down: the class decides the blast radius "
        f"(provider-wide for hard classes).")


def load():
    """Read the state file. Tolerant: missing/corrupt -> fresh state."""
    try:
        return json.load(open(STATE))
    except Exception:
        return {'version': 1, 'pairs': {}, 'v2': {'provider_breakers': {},
                                                   'classes': {}}}


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


def _parse_utc(ts):
    """Best-effort ISO-8601 -> aware UTC datetime; None on any failure."""
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _is_open(entry, now_iso_str):
    """True when an entry (pair or provider breaker) is still open."""
    ou = entry.get('open_until')
    if not ou:
        return False
    return ou > now_iso_str


def _prune_expired(st, now, keep_pair=None):
    """Remove cooling pairs and provider breakers whose open_until is past.

    keep_pair: key that must survive pruning (the pair being written — its
    streak continues across a natural cooldown).
    """
    pairs = st.setdefault('pairs', {})
    for k in [k for k in pairs if k != keep_pair]:
        if not _is_open(pairs[k], now):
            del pairs[k]
    v2 = st.setdefault('v2', {})
    prov = v2.setdefault('provider_breakers', {})
    for k in list(prov.keys()):
        if not _is_open(prov[k], now):
            del prov[k]


def _ensure_v2(st):
    """Return a v2 block, initializing it if absent."""
    if 'v2' not in st or not isinstance(st['v2'], dict):
        st['v2'] = {'provider_breakers': {}, 'classes': {}}
    v2 = st['v2']
    if 'provider_breakers' not in v2 or not isinstance(v2['provider_breakers'], dict):
        v2['provider_breakers'] = {}
    if 'classes' not in v2 or not isinstance(v2['classes'], dict):
        v2['classes'] = {}
    return v2


def _record_class(v2, provider, model, fclass, now):
    """Append a class event under v2.classes.<provider>.<model>."""
    classes = v2.setdefault('classes', {})
    prov_classes = classes.setdefault(provider, {})
    mod_classes = prov_classes.setdefault(model, [])
    if not isinstance(mod_classes, list):
        mod_classes = []
    mod_classes.append({'class': fclass, 'ts': now})
    prov_classes[model] = mod_classes


def _provider_failures_in_window(v2, provider, fclass, now, window_s):
    """Count recent class events for provider across all models."""
    classes = v2.get('classes') or {}
    prov_classes = classes.get(provider) or {}
    if not isinstance(prov_classes, dict):
        return 0
    cutoff = (_parse_utc(now) - datetime.timedelta(seconds=window_s)).isoformat()
    count = 0
    for model, evs in prov_classes.items():
        if not isinstance(evs, list):
            continue
        for ev in evs:
            if isinstance(ev, dict) and ev.get('class') == fclass and ev.get('ts', '') >= cutoff:
                count += 1
    return count


def _open_provider_breaker(st, provider, fclass, now):
    """Open (or extend) the provider-level breaker for a hard class."""
    v2 = _ensure_v2(st)
    prov = v2.setdefault('provider_breakers', {})
    cd = CLASS_COOLDOWN_S.get(fclass, BASE_COOLDOWN_S)
    open_until = (_parse_utc(now) + datetime.timedelta(seconds=cd)).isoformat()
    prov[provider] = {
        'class': fclass,
        'open_until': open_until,
        'opened_at': now,
        'cooldown_s': cd,
    }
    return cd, open_until


def record_failure(provider, model, reason='', fclass='api_down', kind=None):
    """Record a failure for (provider, model) with class fclass.

    TR-190 classification seam (loud, never silent):
      - kind (free text, e.g. "hop-wall-timeout", "429 from upstream") is
        resolved through FAILURE_CLASS_MAP via failure_class_for(); an unmapped
        kind raises ValueError naming the kind and the table to extend. It is
        NEVER coerced to the hard default.
      - an explicit-but-unknown fclass raises the same way (the old behavior
        silently rewrote it to api_down — the 2026-09-25 lockup mechanism).

    No-hop ledger rows (TR-182): placeholder identities (provider or model in
    NOT_A_LANE_IDENTITIES, e.g. the proxy's ('none', 'none') no-hops row) are
    declined BEFORE any state write — a router-internal outcome is not a
    provider event and can never open a breaker. Fail-open: exit 0.

    For hard classes (api_down/out_of_credit) the provider-level breaker opens
    when >=3 failures of the same class occur within the class cooldown window
    across any model of that provider.  Soft classes (overload/quota_window)
    only open the specific (provider, model) pair with a short cooldown.
    """
    # --- no-hop guard: before the lock, before any state touch.
    _p = (provider or '').strip().lower()
    _m = (model or '').strip().lower()
    if _p in NOT_A_LANE_IDENTITIES or _m in NOT_A_LANE_IDENTITIES:
        print(f'SKIPPED {provider or ""}/{model or ""} — not a lane (no-hop '
              f'ledger row); no circuit event recorded')
        return 0
    # --- loud classification: validate or raise, never hard-default.
    fclass = fclass if fclass is not None else 'api_down'
    if kind is not None:
        fclass, _ = failure_class_for(kind=kind)
    else:
        fclass, _ = failure_class_for(fclass=fclass)
    key = f'{provider}/{model}'
    lf = _acquire_lock()
    try:
        st = load()
        now = now_iso()
        _prune_expired(st, now, keep_pair=key)
        _ensure_v2(st)
        c = st['pairs'].setdefault(key, {'failures': 0, 'open_until': None,
                                         'last_failure': None, 'reason': '',
                                         'class': fclass})
        c['failures'] = c.get('failures', 0) + 1
        c['class'] = fclass
        cd = CLASS_COOLDOWN_S.get(fclass)
        if cd is None:
            cd = min(BASE_COOLDOWN_S * (2 ** (c['failures'] - 1)), MAX_COOLDOWN_S)
        c['open_until'] = (_parse_utc(now) + datetime.timedelta(seconds=cd)).isoformat()
        c['last_failure'] = now
        c['cooldown_s'] = cd
        if reason:
            c['reason'] = reason
        _record_class(st['v2'], provider, model, fclass, now)

        # Provider-level breaker for hard classes.
        if fclass in HARD_CLASSES:
            window_s = cd
            recent = _provider_failures_in_window(st['v2'], provider, fclass, now, window_s)
            if recent >= PROVIDER_FAILURE_THRESHOLD:
                pcd, p_open = _open_provider_breaker(st, provider, fclass, now)
                save(st)
                print(f'OPEN {key} class={fclass} — {c["failures"]} consecutive failures, '
                      f'cooldown {cd}s, open until {c["open_until"]}; '
                      f'PROVIDER BREAKER {provider} class={fclass} open until {p_open}')
                return 0
        save(st)
    finally:
        lf.close()
    print(f'OPEN {key} class={fclass} — {c["failures"]} consecutive failures, '
          f'cooldown {cd}s, open until {c["open_until"]}')
    return 0


def record_success(provider, model):
    key = f'{provider}/{model}'
    lf = _acquire_lock()
    try:
        st = load()
        _prune_expired(st, now_iso())
        if key in st['pairs']:
            prev = st['pairs'].pop(key)
            # Also clear provider breaker for this provider if the success is
            # recorded on the last open model of that provider.  Conservative:
            # remove provider breaker immediately on any success for the pair
            # that helped open it — the provider has recovered.
            v2 = _ensure_v2(st)
            prov = v2.get('provider_breakers') or {}
            if provider in prov:
                del prov[provider]
            save(st)
            print(f'CLOSED {key} — was open until {prev.get("open_until")} ({prev.get("failures")} failures)')
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
    v2 = _ensure_v2(st)
    provider_breakers = v2.get('provider_breakers', {})
    if provider:
        pairs = {k: v for k, v in pairs.items() if k.startswith(provider + '/')}
        provider_breakers = {k: v for k, v in provider_breakers.items() if k == provider}
    if as_json:
        out = {
            'pairs': [{'pair': k, 'state': 'OPEN' if _is_open(v, now) else 'cooling',
                       'failures': v.get('failures', 0),
                       'open_until': v.get('open_until'),
                       'reason': v.get('reason', ''),
                       'class': v.get('class', 'api_down')}
                      for k, v in sorted(pairs.items())],
            'provider_breakers': [{'provider': k,
                                   'state': 'OPEN' if _is_open(v, now) else 'cooling',
                                   'class': v.get('class', ''),
                                   'open_until': v.get('open_until'),
                                   'opened_at': v.get('opened_at'),
                                   'cooldown_s': v.get('cooldown_s')}
                                  for k, v in sorted(provider_breakers.items())],
            'class_counts': _class_counts(st, provider),
        }
        print(json.dumps(out, indent=1))
        return 0
    printed = False
    if pairs:
        for key, c in sorted(pairs.items()):
            state = 'OPEN' if _is_open(c, now) else 'cooling'
            print(f'{key:<42} {state:<8} class={c.get("class", "api_down"):<14} '
                  f'failures={c.get("failures", 0)} open_until={c.get("open_until")} {c.get("reason", "")}')
            printed = True
    if provider_breakers:
        print('--- provider-level breakers ---')
        for key, c in sorted(provider_breakers.items()):
            state = 'OPEN' if _is_open(c, now) else 'cooling'
            print(f'{key:<42} {state:<8} class={c.get("class", ""):<14} '
                  f'open_until={c.get("open_until")} cooldown_s={c.get("cooldown_s")}')
            printed = True
    if not printed:
        print('no open/recorded circuits')
    return 0


def _class_counts(st, provider=None):
    """Return {class: count} across all recorded events (or one provider)."""
    v2 = st.get('v2') or {}
    classes = v2.get('classes') or {}
    if not isinstance(classes, dict):
        return {}
    counts = {}
    provs = [provider] if provider else list(classes.keys())
    for prov in provs:
        if prov not in classes:
            continue
        modmap = classes[prov]
        if not isinstance(modmap, dict):
            continue
        for evs in modmap.values():
            if not isinstance(evs, list):
                continue
            for ev in evs:
                if isinstance(ev, dict) and ev.get('class'):
                    counts[ev['class']] = counts.get(ev['class'], 0) + 1
    return counts


def clear(provider=None, model=None, all_=False):
    lf = _acquire_lock()
    try:
        st = load()
        _prune_expired(st, now_iso())
        if all_:
            st['pairs'] = {}
            v2 = _ensure_v2(st)
            v2['provider_breakers'] = {}
            v2['classes'] = {}
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
    argv = list(sys.argv[1:] if argv is None else argv)
    # Python 3.11 argparse interleave bug (fixed in 3.12): a trailing
    # nargs='*' positional after an interspersed optional ('record-failure
    # p m --class X "reason words"') errors with "unrecognized arguments".
    # Pre-move optional pairs to the END of record-failure argv so positionals
    # stay contiguous on every interpreter (Bane 2026-09-01, CI 3.11 vs local
    # 3.13 divergence).
    if argv and argv[0] == 'record-failure':
        opts, rest = [], []
        i = 1
        while i < len(argv):
            # --class and --kind both consume a value (TR-190 added --kind);
            # move option+value pairs to the END so positionals stay contiguous.
            if argv[i] in ('--class', '--kind') and i + 1 < len(argv):
                opts.extend(argv[i:i + 2])
                i += 2
            elif argv[i].startswith('--'):
                opts.append(argv[i])
                i += 1
            else:
                rest.append(argv[i])
                i += 1
        argv = ['record-failure'] + rest + opts

    parser = argparse.ArgumentParser(
        prog='router_circuit.py',
        description='Circuit-breaker state for (provider, model) pairs. '
                    'Fail-open: never blocks the scheduler.')
    sub = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)

    pf = sub.add_parser('record-failure', help='open (or extend) the circuit for a pair')
    pf.add_argument('provider')
    pf.add_argument('model')
    pf.add_argument('--class', dest='fclass', default='api_down',
                    choices=sorted(CLASSES),
                    help='failure class (default: api_down)')
    pf.add_argument('--kind', dest='kind', default=None,
                    help='failure kind resolved through FAILURE_CLASS_MAP '
                         '(TR-190); unmapped kinds fail loudly instead of '
                         'defaulting to api_down')
    pf.add_argument('reason', nargs='*', default='',
                    help='optional failure reason (multiple words are joined)')
    pf.set_defaults(func=lambda a: record_failure(a.provider, a.model,
                                                  ' '.join(a.reason),
                                                  fclass=a.fclass, kind=a.kind))

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
