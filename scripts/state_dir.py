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
from __future__ import annotations
import json
import os
import sys
from typing import Any

ENV_VAR = 'ROUTER_STATE_DIR'
WARN_ENV_VAR = 'ROUTER_STATE_DIR_WARN'
DEFAULT_STATE_DIR = os.path.expanduser('~/.hermes/model-router')
LIVE_INSTALL_DIR = os.path.realpath(os.path.expanduser('~/.hermes/scripts'))
QUOTA_STATE_NAME = 'quota-state.json'
BOOTSTRAP_NOTE = 'first-run bootstrap: all providers OPEN; edit to gate'

_WARNED = set()


def _default_warn(message: Any) -> None:
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


def _is_canonical_invocation(argv0: Any):
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


def resolve_state_dir(script_file: Any=None, argv0: Any=None, warn: Any=None,
                      force_non_canonical: Any=None):
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


# --------------------------------------------------------------------------
# TR-345: first-run bootstrap FOR THE RESOLVED STATE DIR
#
# The bootstrap used to live only in task_router/cli.py and it always wrote to
# the DATA HOME root (paths.quota_state_path()). That split-brain is the defect
# this module now owns the fix for: an operator following the README's
# isolation recipe (ROUTER_STATE_DIR=<scratch>, TASK_ROUTER_HOME unset) ran
# `python3 scripts/router_spawn.py`, resolved MR to the scratch dir, found no
# quota-state.json there — and got a fail-closed zero-chain with 137
# `policy-gate-missing-row` exclusions naming a file the isolated dir never
# received. One implementation, called with the dir the READER will use, so the
# writer and the reader cannot disagree again.
# --------------------------------------------------------------------------

def quota_state_path(state_dir: Any=None):
    """<state_dir>/quota-state.json (state_dir defaults to the resolved one)."""
    return os.path.join(state_dir or resolve_state_dir(), QUOTA_STATE_NAME)


def provider_ids(providers_path: Any=None, repo_root: Any=None):
    """Provider ids from a providers.jsonl table; [] on any problem.

    providers_path may be a single path or a list of candidates (first existing
    wins) — router_spawn passes ROUTING_DATA_DIR first, then <repo>/data/tables,
    because a live byte-copy of the script can sit outside the checkout.
    Never raises.
    """
    cands = []
    if isinstance(providers_path, (list, tuple)):
        cands.extend([p for p in providers_path if p])
    elif providers_path:
        cands.append(providers_path)
    if repo_root:
        cands.append(os.path.join(str(repo_root), 'data', 'tables',
                                  'providers.jsonl'))
    cands.append(os.path.join(os.path.dirname(os.path.dirname(
        os.path.realpath(__file__))), 'data', 'tables', 'providers.jsonl'))
    for path in cands:
        try:
            if not os.path.isfile(path):
                continue
            ids = []
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pid = json.loads(line).get('id')
                    except Exception:
                        continue
                    if pid:
                        ids.append(pid)
            if ids:
                return ids
        except Exception:
            continue
    return []


def bootstrap_quota_state(state_dir: Any, providers_path: Any=None,
                          repo_root: Any=None, warn: Any=None,
                          provider_ids_list: Any=None):
    """Write an all-OPEN starter quota-state.json into an EMPTY state dir.

    The CALLER decides whether bootstrapping is appropriate (router_spawn.py
    only does it for an explicitly isolated state dir; the shared production dir
    keeps its fail-closed semantics). This function only mechanically makes the
    requested dir self-sufficient:

      - never overwrites an existing file (byte-stable for live deployments),
      - never raises, never writes stdout (fail-open is sacred),
      - writes atomically (tmp + os.replace) so concurrent first runs do not
        leave a half-written policy plane behind.

    provider_ids_list (TR-345): the caller's authoritative provider set — the
    resolver enumerates lanes from the REGISTRY, which can name providers the
    committed providers.jsonl does not list (measured: deepseek-duckbrain-sync
    was registry-only, so seeding from the table alone still left one
    policy-gate-missing-row exclusion and the isolated dir was not
    self-sufficient). When given, it wins over providers_path.

    Returns {'written', 'path', 'providers', 'reason'} — reason explains a
    no-write ('exists', 'no provider table to seed from', 'no state dir', …) so
    callers can report it instead of guessing.
    """
    out = {'written': False, 'path': None, 'providers': 0, 'reason': None}
    try:
        if not state_dir:
            out['reason'] = 'no state dir'
            return out
        path = os.path.join(str(state_dir), QUOTA_STATE_NAME)
        out['path'] = path
        if os.path.exists(path):
            out['reason'] = 'exists'
            return out
        ids = []
        for pid in (provider_ids_list or []):
            if pid and pid not in ids:
                ids.append(pid)
        if not ids:
            ids = provider_ids(providers_path=providers_path, repo_root=repo_root)
        if not ids:
            out['reason'] = 'no provider table to seed from'
            return out
        os.makedirs(str(state_dir), exist_ok=True)
        doc = {'updated': 'bootstrap',
               'providers': {pid: {'status': 'open'} for pid in ids},
               'note': BOOTSTRAP_NOTE}
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh, indent=1)
        os.replace(tmp, path)
        out.update(written=True, providers=len(ids))
    except Exception as e:  # noqa: BLE001 — bootstrap is best-effort
        out['reason'] = f'{type(e).__name__}: {e}'
        try:
            hook = warn or _default_warn
            hook(f'router: state bootstrap skipped: {e}')
        except Exception:
            pass
    return out
