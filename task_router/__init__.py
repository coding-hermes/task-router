"""task_router — installable task-router CLI package (TR-016).

Thin installable wrapper around the repo's scripts/ tools:
  - task_router.paths — TASK_ROUTER_HOME / XDG data-home resolution
  - task_router.cli   — `router` entry point dispatching to scripts/router_*.py
"""

from task_router import paths  # noqa: F401  (re-exported namespace)

__all__ = ["paths"]
__version__ = "0.1.0"
