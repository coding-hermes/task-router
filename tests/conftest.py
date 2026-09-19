"""Shared test configuration for task-router.

Why this file exists (TR-068): several tests drive the duckdb SEED pipeline in a
subprocess. Measured baseline on an idle box: ~11s. The fleet saturates this
machine routinely (loadavg in the hundreds), and under that load the same seed
stretches past a 120s per-call default — which surfaced as a full-suite-only
`subprocess.TimeoutExpired` and looked like flakiness or a state leak.

Rule: a subprocess budget must be sized for the WORST load the box actually
sees, not the idle case. Seed/duckdb pipelines get SEED_TIMEOUT; light JSON
tools (status, circuit, probefix) keep their tighter defaults.
"""
import os
import subprocess
import sys
import pytest


#: duckdb seed budget. 11s idle measured -> ~55x margin even at heavy load.
SEED_TIMEOUT = 600

#: Pipeline scripts that load the registry through duckdb.
SEED_PIPELINES = ("router_seed.py", "router_pricing.py")

# ---------------------------------------------------------------------------
# TR-077 (Bane 2026-09-19): shared session seed for READ-ONLY consumers.
#
# Rule (from the circuit-suite conversion, TR-078): spend a subprocess only
# where the thing under test needs a fresh build. Tests that mutate the seed's
# INPUTS (context variants, capability retags, lifecycle overlays) keep private
# builds. Tests that only READ the registry produced from the committed
# data/tables share ONE build per pytest session — Bane: "run the script once
# with the database loaded once and then it can cover multiple tests."
#
# Consumers get a COPIED registry + data dir per test (hermetic: a test can
# still trash its copy), but the ~5s duckdb build happens once per session
# instead of once per test.
import shutil as _shutil
import shutil
import tempfile as _tempfile

_SESSION_SEED = {"done": False, "registry": None, "data": None, "error": None}


@pytest.fixture(scope="session")
def shared_seed_registry():
    """One real seed build per session from the committed tables (repo DATA_DIR).

    Returns (registry_path, data_dir) in a session tempdir. Not reused directly:
    tests copy what they need via seed_registry_copy() so per-test mutation
    stays impossible on the shared build.
    """
    if not _SESSION_SEED["done"]:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = _tempfile.mkdtemp(prefix="shared-seed-")
        data = os.path.join(base, "data", "tables")
        os.makedirs(os.path.dirname(data))  # parent only: copytree creates `data`
        _shutil.copytree(os.path.join(repo, "data", "tables"), data)
        reg = os.path.join(base, "registry.json")
        env = dict(os.environ, ROUTING_REGISTRY=reg, ROUTING_DATA_DIR=data,
                   ROUTING_NS=os.path.join(base, "ns"))
        p = subprocess.run([sys.executable, os.path.join(repo, "scripts", "router_seed.py")],
                           capture_output=True, text=True, env=env, timeout=SEED_TIMEOUT)
        _SESSION_SEED.update(done=True, registry=reg, data=os.path.join(base, "data"),
                             error=None if p.returncode == 0 else p.stderr[-800:])
    if _SESSION_SEED["error"]:
        pytest.fail(f"shared session seed failed: {_SESSION_SEED['error']}")
    return _SESSION_SEED["registry"], _SESSION_SEED["data"]


@pytest.fixture()
def seed_registry_copy(shared_seed_registry, tmp_path):
    """A per-test hermetic COPY of the session build (registry + data tree).

    Read-only consumers use this: same bytes as a private build (the suite's
    own idempotency test proves seed output is deterministic), one build cost
    per session. Copying (not sharing the path) keeps even accidental writes
    from leaking between tests.
    """
    reg_src, data_src = shared_seed_registry
    reg_dst = tmp_path / "registry.json"
    shutil.copy2(reg_src, reg_dst)
    data_dst = tmp_path / "data"
    shutil.copytree(data_src, data_dst)
    return {"ROUTING_REGISTRY": str(reg_dst),
            "ROUTING_DATA_DIR": str(data_dst),
            "ROUTER_STATE_DIR": str(tmp_path / "state")}
