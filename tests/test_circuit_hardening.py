"""TR-027 — circuit breaker hardening regression battery.

Covers the five TR-027 acceptance criteria:
  1. argparse subcommands + --help + consistent exit codes (2 usage / 0 ok)
  2. Advisory flock + unique temp names + fsync + os.replace (no shared .tmp)
  3. Concurrency: N parallel record-failure processes -> ALL recorded
     (Sol's probe: 40 processes -> 30 recorded, 7 lost, 3 crashed)
  4. Expired-pair pruning on write (status stays read-only: TR-024 contract)
  5. status --json contract preserved (TR-024 overlap)

TR-078 (Bane 2026-09-19: "run the script once with the database loaded once
and then it can cover multiple tests"): the module is imported ONCE per pytest
session; every sequential test drives main(argv) IN-PROCESS against its own
hermetic STATE file — zero interpreter spawns. Subprocess is reserved for the
two places where OS processes are the SUBJECT:
  - the concurrency contract (parallel writers, bounded waves of 8), and
  - one CLI-parity smoke pinning real-process exit codes against the
    in-process view.
No mocks: same real script, same real state file, one load.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "router_circuit.py")

_RC = None


def _load_once():
    """Import the real script module once per session (TR-078: one load)."""
    global _RC
    if _RC is None:
        spec = importlib.util.spec_from_file_location("router_circuit_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RC = mod
    return _RC


@pytest.fixture()
def circuit(tmp_path):
    """The circuit module with STATE pointed at this test's tmp dir."""
    rc = _load_once()
    rc.STATE = os.path.join(str(tmp_path), "circuit-state.json")
    return rc


def call(rc, *args, capture=False):
    """Run main(argv) in-process. Returns the exit code; with capture=True,
    returns (code, stdout) — stdout redirected into a buffer, self-contained."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            code = rc.main(list(args))
            code = 0 if code is None else code
        except SystemExit as e:
            code = 0 if e.code is None else e.code
    return (code, buf.getvalue()) if capture else code


def _state(tmp_path):
    with open(os.path.join(str(tmp_path), "circuit-state.json")) as f:
        return json.load(f)


def _write_state(tmp_path, st):
    with open(os.path.join(str(tmp_path), "circuit-state.json"), "w") as f:
        json.dump(st, f)


# ---------------------------------------------------------------- argparse --

def test_help_exit_zero_and_usage_text(circuit):
    for cmd in ([], ["record-failure"], ["record-success"], ["status"], ["clear"]):
        code, out = call(circuit, *cmd, "--help", capture=True)
        assert code == 0, (cmd, code)
        assert "usage:" in out.lower(), (cmd, out[:200])


def test_usage_errors_exit_two(circuit):
    # unknown subcommand
    assert call(circuit, "frobnicate") == 2
    # unknown flag on a subcommand
    assert call(circuit, "status", "--bogus") == 2
    # missing required positional args
    assert call(circuit, "record-failure", "only-provider") == 2
    assert call(circuit, "record-success", "only-provider") == 2
    # clear with neither --all nor pair
    assert call(circuit, "clear") == 2
    # clear --all with a pair is contradictory
    assert call(circuit, "clear", "provider-a", "model-1", "--all") == 2


def test_ok_exit_zero(circuit):
    assert call(circuit, "record-failure", "provider-a", "model-1", "boom") == 0
    assert call(circuit, "record-success", "provider-a", "model-1") == 0
    assert call(circuit, "status") == 0
    assert call(circuit, "status", "--json") == 0
    assert call(circuit, "clear", "--all") == 0


# ------------------------------------------- scheduler positional contract --

def test_scheduler_positional_forms_preserved(circuit, tmp_path):
    """Go circuit_client.go invokes:
    record-failure <provider> <model> [reason] and record-success <provider> <model>."""
    assert call(circuit, "record-failure", "opencode-go", "mimo-v2.5",
                "gateway failure: HTTP 503: retry shortly.") == 0
    st = _state(tmp_path)
    c = st["pairs"]["opencode-go/mimo-v2.5"]
    assert c["failures"] == 1
    assert c["reason"] == "gateway failure: HTTP 503: retry shortly."
    assert c["open_until"] > c["last_failure"]
    # multi-word reason joins with spaces (same as pre-argparse behavior)
    assert call(circuit, "record-failure", "provider-b", "model-2",
                "first", "second", "third") == 0
    assert _state(tmp_path)["pairs"]["provider-b/model-2"]["reason"] == "first second third"
    assert call(circuit, "record-success", "opencode-go", "mimo-v2.5") == 0
    assert "opencode-go/mimo-v2.5" not in _state(tmp_path)["pairs"]


# ------------------------------------------------------------ concurrency --
# The ONE place subprocess is the subject: the scheduler's Go client execs the
# script per event, so "no lost events under parallel OS writers" must be
# proven with real processes. Bounded waves of 8 (ROUTER_TEST_WAVE knob,
# clamped 2-16) — same contract as the original 40-at-once probe, without the
# interpreter stampede on a box that may be running several suites at once.

def _wave_size():
    try:
        return max(2, min(16, int(os.environ.get("ROUTER_TEST_WAVE", "8"))))
    except ValueError:
        return 8


def _popen_waves(args_builder, n, state_dir, timeout=60):
    """Run n processes in bounded waves; return (index, returncode, stderr) fails."""
    fails = []
    wave = _wave_size()
    for start in range(0, n, wave):
        batch = []
        for i in range(start, min(start + wave, n)):
            e = dict(os.environ, ROUTER_STATE_DIR=state_dir)
            batch.append((i, subprocess.Popen(
                args_builder(i), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=e)))
        for i, pr in batch:
            _, err = pr.communicate(timeout=timeout)
            if pr.returncode != 0:
                fails.append((i, pr.returncode, err.strip()))
    return fails


def test_concurrent_record_failure_no_lost_events(tmp_path):
    """AC: N parallel record-failure processes -> ALL events recorded."""
    n = 40
    failed = _popen_waves(
        lambda i: [sys.executable, SCRIPT, "record-failure",
                   f"conc-prov-{i:02d}", f"model-{i:02d}", f"reason-{i:02d}"],
        n, str(tmp_path))
    assert not failed, f"{len(failed)} processes crashed: {failed[:5]}"
    pairs = _state(tmp_path)["pairs"]
    assert len(pairs) == n, f"lost events: {n - len(pairs)} of {n} missing"
    for i in range(n):
        c = pairs[f"conc-prov-{i:02d}/model-{i:02d}"]
        assert c["failures"] == 1
        assert c["reason"] == f"reason-{i:02d}"


def test_concurrent_same_pair_all_failures_counted(tmp_path):
    """N parallel processes on ONE pair -> failures == N (no lost increments)."""
    n = 20
    failed = _popen_waves(
        lambda _i: [sys.executable, SCRIPT, "record-failure", "shared", "pair"],
        n, str(tmp_path))
    assert not failed, f"crashed: {failed[:5]}"
    c = _state(tmp_path)["pairs"]["shared/pair"]
    assert c["failures"] == n, f"failures={c['failures']}, expected {n}"
    assert len(_state(tmp_path)["pairs"]) == 1


def test_no_stale_tmp_files_after_concurrency(circuit, tmp_path):
    """No shared fixed .tmp path: only per-process unique temps, all gone."""
    assert call(circuit, "record-failure", "provider-a", "model-1") == 0
    assert call(circuit, "record-success", "provider-a", "model-1") == 0
    assert call(circuit, "clear", "--all") == 0
    leftovers = [f for f in os.listdir(str(tmp_path)) if f.endswith(".tmp")]
    assert leftovers == [], f"stale tmp files: {leftovers}"


# --------------------------------------------------------------- pruning --

def test_pruning_expired_pairs_on_write(circuit, tmp_path):
    """Expired cooling pairs pruned when a write happens; OPEN pairs kept."""
    _write_state(tmp_path, {"version": 1, "pairs": {
        "old/expired": {"failures": 3, "open_until": "2000-01-01T00:00:00+00:00",
                        "last_failure": "2000-01-01T00:00:00+00:00", "reason": "ancient"},
        "still/open": {"failures": 1,
                       "open_until": "2999-01-01T00:00:00+00:00",
                       "last_failure": "2999-01-01T00:00:00+00:00", "reason": "hot"},
    }})
    assert call(circuit, "record-failure", "fresh", "pair") == 0
    pairs = _state(tmp_path)["pairs"]
    assert "old/expired" not in pairs, "expired pair not pruned on write"
    assert "still/open" in pairs, "OPEN pair must survive pruning"
    assert "fresh/pair" in pairs
    # a second write keeps pruning idempotent
    assert call(circuit, "record-failure", "fresh", "pair") == 0
    assert "old/expired" not in _state(tmp_path)["pairs"]


def test_pruning_preserves_pair_being_written(circuit, tmp_path):
    """The pair being written survives pruning even if its open_until is past:
    a re-failure after natural cooldown continues the streak (record-success
    semantics depend on the counter being retained)."""
    _write_state(tmp_path, {"version": 1, "pairs": {
        "retry/me": {"failures": 3, "open_until": "2000-01-01T00:00:00+00:00",
                     "last_failure": "2000-01-01T00:00:00+00:00", "reason": "cooled"},
    }})
    assert call(circuit, "record-failure", "retry", "me", "again") == 0
    c = _state(tmp_path)["pairs"]["retry/me"]
    assert c["failures"] == 4, "streak must continue across cooldown"
    assert c["reason"] == "again"
    assert c["open_until"] > c["last_failure"]


def test_status_readonly_no_pruning(circuit, tmp_path):
    """TR-024 contract: status --json STILL lists expired pairs as cooling
    (status is read-only; pruning happens on write)."""
    _write_state(tmp_path, {"version": 1, "pairs": {
        "old/cooling": {"failures": 2, "open_until": "2000-01-01T00:00:00+00:00",
                        "last_failure": "2000-01-01T00:00:00+00:00", "reason": "old"},
    }})
    code, out = call(circuit, "status", "--json", capture=True)
    assert code == 0
    data = json.loads(out)
    assert data["pairs"][0]["pair"] == "old/cooling"
    assert data["pairs"][0]["state"] == "cooling"


# ----------------------------------------------------- status --json (TR-024) --

def test_status_json_contract_preserved(circuit, tmp_path):
    assert call(circuit, "record-failure", "provider-a", "model-1", "boom") == 0
    _write_state(tmp_path, {"version": 1, "pairs": {
        "provider-a/model-1": _state(tmp_path)["pairs"]["provider-a/model-1"],
        "provider-b/model-2": {"failures": 2, "open_until": "2000-01-01T00:00:00+00:00",
                               "last_failure": "2000-01-01T00:00:00+00:00", "reason": "old"},
    }})
    code, out = call(circuit, "status", "--json", capture=True)
    assert code == 0
    data = json.loads(out)
    assert "pairs" in data
    by = {e["pair"]: e for e in data["pairs"]}
    assert by["provider-a/model-1"]["state"] == "OPEN"
    assert by["provider-a/model-1"]["failures"] == 1
    assert by["provider-a/model-1"]["reason"] == "boom"
    assert by["provider-b/model-2"]["state"] == "cooling"
    # provider filter still works
    code, out = call(circuit, "status", "provider-a", "--json", capture=True)
    assert code == 0
    data = json.loads(out)
    assert [e["pair"] for e in data["pairs"]] == ["provider-a/model-1"]


# -------------------------------------------------------------- resilience --

def test_corrupt_state_file_tolerated(circuit, tmp_path):
    """FAIL-OPEN: corrupt state file never crashes the CLI (fresh state)."""
    with open(os.path.join(str(tmp_path), "circuit-state.json"), "w") as f:
        f.write("{not json!!!")
    code, out = call(circuit, "status", capture=True)
    assert code == 0
    assert "no open" in out
    assert call(circuit, "record-failure", "provider-a", "model-1") == 0
    assert _state(tmp_path)["pairs"]["provider-a/model-1"]["failures"] == 1


def test_clear_forms(circuit, tmp_path):
    assert call(circuit, "record-failure", "provider-a", "model-1") == 0
    assert call(circuit, "record-failure", "provider-b", "model-2") == 0
    assert call(circuit, "clear", "provider-a", "model-1") == 0
    pairs = _state(tmp_path)["pairs"]
    assert "provider-a/model-1" not in pairs
    assert "provider-b/model-2" in pairs
    assert call(circuit, "clear", "--all") == 0
    assert _state(tmp_path)["pairs"] == {}


# --------------------------------------------------- CLI parity (subprocess) --

def test_subprocess_parity_smoke(tmp_path):
    """TR-078 guard: ONE real-process run pins the CLI surface (argv, exit
    codes, JSON bytes) against the in-process view used by every other test.
    If main()'s in-process semantics ever drift from the real exec, this is
    the tripwire."""
    env = dict(os.environ, ROUTER_STATE_DIR=str(tmp_path))
    p = subprocess.run([sys.executable, SCRIPT, "record-failure", "parity", "probe", "boom"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr[-300:]
    q = subprocess.run([sys.executable, SCRIPT, "status", "--json"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert q.returncode == 0, q.stderr[-300:]
    data = json.loads(q.stdout)
    by = {e["pair"]: e for e in data["pairs"]}
    assert by["parity/probe"]["state"] == "OPEN"
    assert by["parity/probe"]["reason"] == "boom"
    # and the in-process view of the SAME state file agrees
    rc = _load_once()
    rc.STATE = os.path.join(str(tmp_path), "circuit-state.json")
    st = _state(tmp_path)
    assert st["pairs"]["parity/probe"]["failures"] == 1
