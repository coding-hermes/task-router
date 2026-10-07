#!/usr/bin/env python3
"""state_dir.py — THE one shared resolver for the router runtime state dir.

TR-REV-20261005-3: seven scripts each duplicated

    MR = os.environ.get('ROUTER_STATE_DIR', os.path.expanduser('~/.hermes/model-router'))

An absolute shared default. A second checkout of the repo invoked without env
vars silently reads/writes the PRODUCTION circuit/quota/health/ledger state in
~/.hermes/model-router. This module centralizes the resolution:

  1. ROUTER_STATE_DIR env wins, silently, always (an EMPTY value counts as
     unset — the old `os.environ.get` behavior for an empty set-var produced
     a broken '' path prefix).
  2. Otherwise the default stays ~/.hermes/model-router (production must not
     break — byte-identical behavior for every canonical invocation).
  3. When the env is unset AND the process was NOT started through the
     canonical live install (~/.hermes/scripts/* — symlinks back into this
     repo, or the byte-identical cron copies), ONE warning line goes to
     stderr naming the shared state dir and the escape hatch
     (ROUTER_STATE_DIR=<dir> to isolate), before any state read/write.

Fail-open is sacred: this module NEVER raises and NEVER writes to stdout, so
router_spawn.py can never block or error because of it. Callers may pass
their own quiet-mode-aware warn hook (router_spawn.py passes _err); every
warn call is exception-guarded, so a broken sink cannot fail a resolve.

Canonical-install detection reads the INVOCATION path (sys.argv[0],
abspath — deliberately NOT realpath, which would follow a live symlink out
of ~/.hermes/scripts into the repo and misclassify the canonical scheduler
invocation as a stray). Anything else — a direct repo run, a second clone,
a copied script, an in-process import — is non-canonical and warns once.

Subprocess-friendly test override (steers ONLY the warning, never the dir):

    ROUTER_STATE_DIR_WARN=force     emit the warning even when canonical
    ROUTER_STATE_DIR_WARN=suppress  never emit it

Usage (inside a router script):

    try:
        import state_dir as _state_dir_mod
    except ImportError:          # live byte-copy not yet synced: fail open
        _state_dir_mod = None    # -> old silent default, no warning

    if _state_dir_mod is not None:
        MR = _state_dir_mod.resolve_state_dir(script_file=__file__)
    else:
        MR = os.environ.get('ROUTER_STATE_DIR',
                            os.path.expanduser('~/.hermes/model-router'))
"""
import os
import sys

ENV_VAR = 'ROUTER_STATE_DIR'
WARN_ENV_VAR = 'ROUTER_STATE_DIR_WARN'
DEFAULT_STATE_DIR = os.path.expanduser('~/.hermes/model-router')
LIVE_INSTALL_DIR = os.path.realpath(os.path.expanduser('~/.hermes/scripts'))

_WARNED = set()


def _default_warn(message):
    """Sink used when the caller passes none: one line on stderr, guarded."""
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def _warn_mode():
    try:
        return (os.environ.get(WARN_ENV_VAR) or '').strip().lower()
    except Exception:
        return ''


def _is_canonical_invocation(argv0):
    """True when the process was started from the live install dir."""
    if not argv0:
        return False
    try:
        # abspath, NOT realpath: the live tools are symlinks INTO this repo;
        # realpath would follow them out of ~/.hermes/scripts and mark the
        # canonical scheduler invocation non-canonical.
        return os.path.abspath(argv0).startswith(LIVE_INSTALL_DIR + os.sep)
    except Exception:
        return True  # cannot tell -> stay silent (fail-open)


def resolve_state_dir(script_file=None, argv0=None, warn=None,
                      force_non_canonical=None):
    """Resolve the router state dir. Never raises; never touches stdout.

    script_file:  the CALLER's __file__ (used only for the warn dedup key).
    argv0:        override for sys.argv[0] (tests pass an explicit path to
                  simulate a non-canonical invocation).
    warn:         sink for the warning line (default: sys.stderr). router_spawn
                  passes its _quiet-aware _err here.
    force_non_canonical: True/False override of the canonical detection
                  (in-process tests); None -> consult ROUTER_STATE_DIR_WARN.
    """
    try:
        env = os.environ.get(ENV_VAR, '')
        if env:
            return env  # env wins, silently, always

        # The warn-env steers ONLY the final emission and beats everything
        # else (tests force/suppress via subprocess env): force -> warn even
        # on a canonical invocation; suppress -> never warn.
        mode = _warn_mode()
        if mode in ('force', '1', 'true', 'yes'):
            noncanonical = True
        elif mode in ('suppress', 'off', '0', 'no', 'false'):
            noncanonical = False
        elif force_non_canonical is not None:
            noncanonical = bool(force_non_canonical)
        else:
            a0 = argv0 if argv0 is not None else (
                sys.argv[0] if sys.argv and sys.argv[0] else '')
            noncanonical = not _is_canonical_invocation(a0)

        if noncanonical:
            try:
                key = (os.path.realpath(script_file) if script_file else '',
                       DEFAULT_STATE_DIR)
            except Exception:
                key = ('', DEFAULT_STATE_DIR)
            if key not in _WARNED:
                _WARNED.add(key)
                hook = warn or _default_warn
                try:
                    hook('WARNING [router state-dir] %s is unset and this '
                         'process was not started through the canonical '
                         'live install (%s): using the SHARED production '
                         'state dir %s — set %s=<dir> to isolate.'
                         % (ENV_VAR, LIVE_INSTALL_DIR, DEFAULT_STATE_DIR,
                            ENV_VAR))
                except Exception:
                    pass
        return DEFAULT_STATE_DIR
    except Exception:
        return DEFAULT_STATE_DIR
