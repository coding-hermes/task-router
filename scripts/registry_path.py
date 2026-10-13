#!/usr/bin/env python3
"""registry_path.py — THE one shared resolver for the router registry path.

TR-REV-1012-1 (review 2026-10-12): the seed step and the validate step still
disagreed on where registry.json lives on a fresh clone — but not between the
bare scripts (they already share the <repo>/registry.json default, worker
2026-09-21). The surviving divergence was BETWEEN INVOCATION SCHOOLS:

  make seed       -> scripts/router_seed.py directly  -> <repo>/registry.json
  make validate   -> `router validate` (the CLI)      -> ROUTING_REGISTRY
                     exported = <TASK_ROUTER_HOME or XDG data home>/registry.json

A fresh-clone user following the README ran `make seed` then `make validate`
and got exit 1 ("missing ~/.local/share/task-router/registry.json") against a
registry.json sitting in the repo root. scripts/fresh_clone_smoke.sh step 6
documents exactly this trap; the README told users to "use the make targets
or export ROUTING_REGISTRY" — a workaround, not a fix.

This module gives every consumer ONE resolution order (highest first):

  1. ROUTING_REGISTRY env     — authoritative per-invocation override
                                (tests, maintenance, the CLI's data-home
                                export). NEVER removed.
  2. TASK_ROUTER_HOME env     — <home>/registry.json (container/hermetic
                                override; same precedence as
                                task_router/paths.py resolve_data_home()).
  3. <repo>/registry.json     — THE default. The repo artifact the seed
                                writes and spawn/status/validate all read;
                                keeps a checkout self-contained.
  4. XDG data home            — <XDG_DATA_HOME or ~/.local/share>/task-router/
                                registry.json. Last resort for callers with no
                                discoverable repo (embedded installs); in a
                                checkout this tier only fires when someone set
                                XDG_DATA_HOME, which is itself a deliberate
                                environment choice.

TASK_ROUTER_HOME above the repo default means a hermetic harness can relocate
the whole registry plane with ONE variable and no per-hook exports — and the
fresh-clone `make seed && make validate` sequence works with NO env at all.

House rules (mirrors scripts/state_dir.py): stdlib only (scripts stay
bare-venv importable — the 2026-09-19 CI lesson), never raises, never writes
to stdout, and degrades to the old two-tier behavior when a stale live copy
of this module is missing (sync_runtime.sh byte-copies this file next to the
importing scripts).

Usage (inside a router script):

    try:
        import registry_path as _registry_path_mod
    except ImportError:          # live byte-copy not yet synced: fail open
        _registry_path_mod = None

    if _registry_path_mod is not None:
        REGISTRY = _registry_path_mod.resolve_registry_path(repo_root=_REPO)
    else:
        REGISTRY = os.environ.get('ROUTING_REGISTRY',
                                  os.path.join(_REPO, 'registry.json'))
"""
import os

__all__ = ["resolve_registry_path"]


def _data_home():
    """task_router/paths.py precedence, inlined (stdlib-only scripts layer)."""
    home = (os.environ.get('TASK_ROUTER_HOME') or '').strip()
    if home:
        return os.path.abspath(os.path.expanduser(home))
    xdg = os.environ.get('XDG_DATA_HOME')
    if xdg:
        return os.path.join(os.path.abspath(os.path.expanduser(xdg)),
                            'task-router')
    return os.path.join(os.path.expanduser('~'), '.local', 'share',
                        'task-router')


def resolve_registry_path(repo_root):
    """Registry.json path: ROUTING_REGISTRY > TASK_ROUTER_HOME > <repo> > XDG.

    repo_root is the caller's <repo> (its scripts/ dir's parent). Never
    raises; the final fallback inside the try is the repo default, so even a
    broken env value (unwritable ~, expanduser failure) cannot error a
    resolve — fail-open is sacred.
    """
    try:
        env = os.environ.get('ROUTING_REGISTRY')
        if env:
            return env
        home = (os.environ.get('TASK_ROUTER_HOME') or '').strip()
        if home:
            return os.path.join(os.path.abspath(os.path.expanduser(home)),
                                'registry.json')
        if repo_root and os.path.isdir(repo_root):
            return os.path.join(repo_root, 'registry.json')
        return os.path.join(_data_home(), 'registry.json')
    except Exception:
        return os.path.join(repo_root or '', 'registry.json')
