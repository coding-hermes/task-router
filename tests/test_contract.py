"""TR-029 contract tests — fast, deterministic, subprocess-based.

These tests lock in CLI contracts that are easy to break by accident:
pure JSON output, --help safety, input validation, circuit concurrency,
reprice non-interference, and the documented snapshot one-liner.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)


def _run(script_argv, env=None, cwd=REPO, timeout=30):
    """Run a scripts/ script under the board venv, return CompletedProcess."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [PY, os.path.join(REPO, "scripts", script_argv[0])] + list(script_argv[1:]),
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.parametrize("argv", [
    ("router_circuit.py", "status", "--json"),
    ("router_gaps.py", "--json"),
    ("router_ledger.py", "status", "--json"),
    ("router_plan_sweep.py", "--json"),
    ("router_pricing.py", "--json", "--dry-run"),
])
def test_json_purity_matrix(argv):
    """Every script that claims --json emits parseable JSON on stdout."""
    proc = _run(argv)
    assert proc.returncode == 0, f"{argv[0]} exited {proc.returncode}: {proc.stderr[:300]}"
    # No stray non-JSON lines on stdout
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(f"{argv[0]} --json stdout is not pure JSON: {e}\n{proc.stdout[:400]}")
    assert isinstance(data, dict), f"{argv[0]} --json root is not a dict: {type(data)}"


def test_profile_req_invalid_input():
    """Invalid --profile-req produces a clean JSON error, not a traceback."""
    proc = _run(("router_spawn.py", "--profile-req", "foobar"))
    assert proc.returncode == 0, "router_spawn must fail-open to exit 0"
    data = json.loads(proc.stdout)
    assert data.get("error"), f"expected error object, got {data.keys()}"
    assert "INVALID_REQUIREMENT" in (data.get("code") or "")
    # stderr must not contain a Python traceback
    assert "Traceback" not in proc.stderr, f"traceback leaked to stderr: {proc.stderr[:400]}"


def test_router_seed_help_is_safe(tmp_path):
    """router_seed.py --help exits 0 and writes nothing (TR-029)."""
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 3, "tables": {}}))
    env = {"ROUTING_REGISTRY": str(reg)}
    mtime0 = os.path.getmtime(reg)
    size0 = os.path.getsize(reg)
    proc = _run(("router_seed.py", "--help"), env=env)
    assert proc.returncode == 0, f"--help failed: {proc.stderr[:300]}"
    assert "Seed the task-router" in proc.stdout, f"unexpected help: {proc.stdout[:200]}"
    assert os.path.getmtime(reg) == mtime0, "registry.json mtime changed"
    assert os.path.getsize(reg) == size0, "registry.json size changed"


def test_provider_health_probe_help_is_safe(tmp_path):
    """provider_health_probe.py --help exits 0 with no network or state writes."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = {"ROUTER_STATE_DIR": str(state_dir)}
    t0 = time.time()
    proc = _run(("provider_health_probe.py", "--help"), env=env, timeout=5)
    elapsed = time.time() - t0
    assert proc.returncode == 0, f"--help failed: {proc.stderr[:300]}"
    assert "Provider health probe" in proc.stdout, f"unexpected help: {proc.stdout[:200]}"
    # --help must be essentially instant; >2s implies network or heavy work
    assert elapsed < 2.0, f"--help took {elapsed:.2f}s — likely hit network"
    assert not any(state_dir.iterdir()), "probe --help wrote state files"


def test_circuit_concurrency_no_corruption(tmp_path):
    """Two concurrent record-failure writers do not corrupt circuit-state.json."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = {"ROUTER_STATE_DIR": str(state_dir)}
    # start clean
    _run(("router_circuit.py", "clear", "--all"), env=env)
    errors = []

    def writer(tag):
        for _ in range(20):
            r = _run(("router_circuit.py", "record-failure", "test-provider",
                      f"model-{tag}", "overload"), env=env)
            if r.returncode != 0:
                errors.append((tag, r.stderr[:200]))

    t1 = threading.Thread(target=writer, args=(1,))
    t2 = threading.Thread(target=writer, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"concurrent record-failure returned errors: {errors[:3]}"
    state = json.load(open(state_dir / "circuit-state.json"))
    pairs = state["pairs"]
    assert "test-provider/model-1" in pairs
    assert "test-provider/model-2" in pairs


def _seed_scratch_registry(tmp_path, reg):
    """registry.json is gitignored live state - derive it in CI via seed."""
    src = os.path.join(REPO, "registry.json")
    if os.path.isfile(src):
        reg.write_text(open(src).read())
        return
    import subprocess
    env = dict(os.environ, ROUTING_REGISTRY=str(reg))
    subprocess.run([sys.executable, os.path.join(REPO, "scripts", "router_seed.py")],
                   capture_output=True, text=True, env=env, cwd=REPO, timeout=240)
    assert reg.exists(), "seed failed to produce scratch registry.json"


def test_reprice_dry_run_does_not_change_heads(tmp_path):
    """Dry-run reprice against a scratch registry leaves fixed-profile heads alone."""
    reg = tmp_path / "registry.json"
    data_dir = tmp_path / "data" / "tables"
    data_dir.mkdir(parents=True)
    # copy committed data tables
    for fn in os.listdir(os.path.join(REPO, "data", "tables")):
        if fn.endswith(".jsonl"):
            src = os.path.join(REPO, "data", "tables", fn)
            (data_dir / fn).write_text(open(src).read())
    _seed_scratch_registry(tmp_path, reg)

    # deterministic no-op spot-check (returns no parsable prices -> fail-open)
    noop_spot = tmp_path / "noop_spot.py"
    noop_spot.write_text("#!/usr/bin/env python3\nprint('TOTAL_MODELS: 0')\n")
    noop_spot.chmod(0o755)

    env = {
        "ROUTING_REGISTRY": str(reg),
        "ROUTING_DATA_DIR": str(data_dir),
        "ROUTING_SPOT_CHECK": str(noop_spot),
    }
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import router_spawn as rs

    rs.REGISTRY = str(reg)
    before = rs.resolve(profile_id="P4_SECURITY")["head"]
    proc = _run(("router_maintain.py", "reprice", "--dry-run"), env=env, timeout=60)
    assert proc.returncode == 0, f"reprice dry-run failed: {proc.stderr[:400]}"
    # The noop spot-check produces no prices; reprice logs the skip to stderr
    # and continues fail-open.
    assert "spot-check returned no parsable" in proc.stderr, (
        f"reprice did not report noop spot-check: {proc.stderr[:400]}"
    )
    rs.REGISTRY = str(reg)
    after = rs.resolve(profile_id="P4_SECURITY")["head"]
    assert before == after, f"reprice dry-run changed P4_SECURITY head: {before} -> {after}"


def test_snapshot_one_liner_produces_file(tmp_path):
    """The documented snapshot one-liner writes a valid chains snapshot file."""
    reg = tmp_path / "registry.json"
    docs_dir = tmp_path / "docs"
    _seed_scratch_registry(tmp_path, reg)
    env = {
        "ROUTING_REGISTRY": str(reg),
        "ROUTING_DOCS_DIR": str(docs_dir),
    }
    proc = _run(("router_maintain.py", "snapshot"), env=env, timeout=60)
    assert proc.returncode == 0, f"snapshot failed: {proc.stderr[:400]}"
    snaps = list(docs_dir.glob("chains-*.md")) if docs_dir.exists() else []
    assert snaps, "snapshot did not write docs/chains-<date>.md"
    text = snaps[0].read_text()
    assert text.startswith("# Chains snapshot"), "snapshot file missing expected header"
    assert "## P0_FORE" in text, "snapshot missing P0_FORE section"
    assert "## P4_SECURITY" in text, "snapshot missing P4_SECURITY section"
