"""task_router.paths — data-home resolution for the task-router (TR-016).

Resolution order (highest priority first):
  1. TASK_ROUTER_HOME       — explicit override (scheduler/CI/tests)
  2. XDG_DATA_HOME          — $XDG_DATA_HOME/task-router
  3. default                — ~/.local/share/task-router

All per-file helpers (registry_path, circuit_state_path, ledger_path,
health_state_path) live inside resolve_data_home(). The repo itself keeps its
repo-relative defaults (scripts/ read/write <repo>/registry.json and
~/.hermes/model-router state); the CLI layer (task_router.cli) exports
ROUTING_* env overrides derived from this data home before dispatching to a
script, so an installed `router` and the scheduler's repo-relative subprocess
path share one data location without touching fail-open script logic.
"""

import os

__all__ = [
    "ENV_HOME",
    "ENV_XDG",
    "DEFAULT_SUBDIR",
    "resolve_data_home",
    "registry_path",
    "circuit_state_path",
    "ledger_path",
    "health_state_path",
]

ENV_HOME = "TASK_ROUTER_HOME"
ENV_XDG = "XDG_DATA_HOME"
DEFAULT_SUBDIR = "task-router"


def resolve_data_home(create=True):
    """Return the task-router data home directory.

    TASK_ROUTER_HOME wins over XDG_DATA_HOME; XDG wins over the XDG default
    (~/.local/share). With create=True (default) the directory (and parents)
    is created if missing — idempotent, stdlib only.
    """
    home = os.environ.get(ENV_HOME)
    if home:
        path = os.path.abspath(os.path.expanduser(home))
    else:
        xdg = os.environ.get(ENV_XDG)
        if xdg:
            path = os.path.join(os.path.abspath(os.path.expanduser(xdg)),
                                DEFAULT_SUBDIR)
        else:
            path = os.path.join(os.path.expanduser("~"),
                                ".local", "share", DEFAULT_SUBDIR)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def registry_path():
    """Path to registry.json inside the data home."""
    return os.path.join(resolve_data_home(), "registry.json")


def circuit_state_path():
    """Path to circuit-state.json inside the data home."""
    return os.path.join(resolve_data_home(), "circuit-state.json")


def ledger_path():
    """Path to ledger.jsonl inside the data home."""
    return os.path.join(resolve_data_home(), "ledger.jsonl")


def health_state_path():
    """Path to health-state.json inside the data home."""
    return os.path.join(resolve_data_home(), "health-state.json")
