#!/usr/bin/env python3
"""Dogfood probe: replicate `router validate` dispatch from the parent cwd and
print what cli.py actually resolves, so the cwd-dependent ImportError has a
mechanism attached instead of a description."""
import os, runpy, sys

os.chdir("/home/kara/.hermes/cache/scratch/tr-dogfood-20261124a")
print("cwd:", os.getcwd())
sys.path.insert(0, "/home/kara/.hermes/cache/scratch/tr-dogfood-20261124a/consumer/.venv/lib/python3.12/site-packages")
import task_router.cli as cli
print("REPO:", cli.REPO)
print("SCRIPTS_DIR:", cli.SCRIPTS_DIR, "exists:", os.path.isdir(cli.SCRIPTS_DIR))
print("lifecycle_gate in SCRIPTS_DIR:", os.path.isfile(os.path.join(cli.SCRIPTS_DIR, "lifecycle_gate.py")))
print("sys.path[0:3]:", sys.path[:3])
try:
    cli.dispatch("validate", ["--json"])
except SystemExit as e:
    print("SystemExit:", e.code)
except Exception as e:
    import traceback
    print("EXC:", type(e).__name__, e)
    traceback.print_exc()
