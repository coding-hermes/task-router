"""TR-027 — circuit breaker hardening regression battery.

Covers the five TR-027 acceptance criteria:
  1. argparse subcommands + --help + consistent exit codes (2 usage / 0 ok)
  2. Advisory flock + unique temp names + fsync + os.replace (no shared .tmp)
  3. Concurrency: N parallel record-failure processes -> ALL recorded
     (Sol's probe: 40 processes -> 30 recorded, 7 lost, 3 crashed)
  4. Expired-pair pruning on write (status stays read-only: TR-024 contract)
  5. status --json contract preserved (TR-024 overlap)

All tests run the REAL script via subprocess with a hermetic ROUTER_STATE_DIR
(no mocks). The scheduler Go client invokes record-failure/record-success
positionally — those forms are pinned here too.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "router_circuit.py")


def run(*args, timeout=60, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def _env(tmp_path):
    d = str(tmp_path)
    return {"ROUTER_STATE_DIR": d}


def _state(tmp_path):
    with open(os.path.join(str(tmp_path), "circuit-state.json")) as f:
        return json.load(f)


def _write_state(tmp_path, st):
    with open(os.path.join(str(tmp_path), "circuit-state.json"), "w") as f:
        json.dump(st, f)


# ---------------------------------------------------------------- argparse --

def test_help_exit_zero_and_usage_text():
    for cmd in ([], ["record-failure"], ["record-success"], ["status"], ["clear"]):
        p = run(*cmd, "--help")
        assert p.returncode == 0, (cmd, p.stderr)
        assert "usage:" in p.stdout.lower(), (cmd, p.stdout)


def test_usage_errors_exit_two():
    # unknown subcommand
    p = run("frobnicate")
    assert p.returncode == 2
    # unknown flag on a subcommand
    p = run("status", "--bogus")
    assert p.returncode == 2
    # missing required positional args
    p = run("record-failure", "only-provider")
    assert p.returncode == 2
    p = run("record-success", "only-provider")
    assert p.returncode == 2
    # clear with neither --all nor pair
    p = run("clear")
    assert p.returncode == 2
    # clear --all with a pair is contradictory
    p = run("clear", "provider-a", "model-1", "--all")
    assert p.returncode == 2


def test_ok_exit_zero(tmp_path):
    env = _env(tmp_path)
    assert run("record-failure", "provider-a", "model-1", "boom",
               env_extra=env).returncode == 0
    assert run("record-success", "provider-a", "model-1",
               env_extra=env).returncode == 0
    assert run("status", env_extra=env).returncode == 0
    assert run("status", "--json", env_extra=env).returncode == 0
    assert run("clear", "--all", env_extra=env).returncode == 0


# ------------------------------------------- scheduler positional contract --

def test_scheduler_positional_forms_preserved(tmp_path):
    """Go circuit_client.go invokes:
    record-failure <provider> <model> [reason] and record-success <provider> <model>."""
    env = _env(tmp_path)
    p = run("record-failure", "opencode-go", "mimo-v2.5",
            "gateway failure: HTTP 503: retry shortly.", env_extra=env)
    assert p.returncode == 0
    st = _state(tmp_path)
    c = st["pairs"]["opencode-go/mimo-v2.5"]
    assert c["failures"] == 1
    assert c["reason"] == "gateway failure: HTTP 503: retry shortly."
    assert c["open_until"] > c["last_failure"]
    # multi-word reason joins with spaces (same as pre-argparse behavior)
    p = run("record-failure", "provider-b", "model-2", "first", "second", "third",
            env_extra=env)
    assert p.returncode == 0
    assert _state(tmp_path)["pairs"]["provider-b/model-2"]["reason"] == "first second third"
    p = run("record-success", "opencode-go", "mimo-v2.5", env_extra=env)
    assert p.returncode == 0
    assert "opencode-go/mimo-v2.5" not in _state(tmp_path)["pairs"]


# ------------------------------------------------------------ concurrency --

def test_concurrent_record_failure_no_lost_events(tmp_path):
    """AC: N parallel record-failure processes -> ALL events recorded.

    Reproduces Sol's probe shape (40 concurrent processes) minus the crash:
    every process must exit 0 and every event must land.
    """
    env = _env(tmp_path)
    n = 40
    procs = []
    for i in range(n):
        e = dict(os.environ)
        e.update(env)
        procs.append(subprocess.Popen(
            [sys.executable, SCRIPT, "record-failure",
             f"conc-prov-{i:02d}", f"model-{i:02d}", f"reason-{i:02d}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e))
    failed = []
    for i, pr in enumerate(procs):
        out, err = pr.communicate(timeout=60)
        if pr.returncode != 0:
            failed.append((i, pr.returncode, err.strip()))
    assert not failed, f"{len(failed)} processes crashed: {failed[:5]}"
    pairs = _state(tmp_path)["pairs"]
    assert len(pairs) == n, f"lost events: {n - len(pairs)} of {n} missing"
    for i in range(n):
        c = pairs[f"conc-prov-{i:02d}/model-{i:02d}"]
        assert c["failures"] == 1
        assert c["reason"] == f"reason-{i:02d}"


def test_concurrent_same_pair_all_failures_counted(tmp_path):
    """N parallel processes on ONE pair -> failures == N (no lost increments)."""
    env = _env(tmp_path)
    n = 20
    procs = []
    for _ in range(n):
        e = dict(os.environ)
        e.update(env)
        procs.append(subprocess.Popen(
            [sys.executable, SCRIPT, "record-failure", "shared", "pair"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e))
    for pr in procs:
        out, err = pr.communicate(timeout=60)
        assert pr.returncode == 0, err
    c = _state(tmp_path)["pairs"]["shared/pair"]
    assert c["failures"] == n, f"failures={c['failures']}, expected {n}"
    assert len(_state(tmp_path)["pairs"]) == 1


def test_no_stale_tmp_files_after_concurrency(tmp_path):
    """No shared fixed .tmp path: only per-process unique temps, all gone."""
    env = _env(tmp_path)
    run("record-failure", "provider-a", "model-1", env_extra=env)
    run("record-success", "provider-a", "model-1", env_extra=env)
    run("clear", "--all", env_extra=env)
    leftovers = [f for f in os.listdir(str(tmp_path)) if f.endswith(".tmp")]
    assert leftovers == [], f"stale tmp files: {leftovers}"


# --------------------------------------------------------------- pruning --

def test_pruning_expired_pairs_on_write(tmp_path):
    """Expired cooling pairs pruned when a write happens; OPEN pairs kept."""
    env = _env(tmp_path)
    _write_state(tmp_path, {"version": 1, "pairs": {
        "old/expired": {"failures": 3, "open_until": "2000-01-01T00:00:00+00:00",
                        "last_failure": "2000-01-01T00:00:00+00:00", "reason": "ancient"},
        "still/open": {"failures": 1,
                       "open_until": "2999-01-01T00:00:00+00:00",
                       "last_failure": "2999-01-01T00:00:00+00:00", "reason": "hot"},
    }})
    p = run("record-failure", "fresh", "pair", env_extra=env)
    assert p.returncode == 0
    pairs = _state(tmp_path)["pairs"]
    assert "old/expired" not in pairs, "expired pair not pruned on write"
    assert "still/open" in pairs, "OPEN pair must survive pruning"
    assert "fresh/pair" in pairs
    # a second write keeps pruning idempotent
    run("record-failure", "fresh", "pair", env_extra=env)
    pairs = _state(tmp_path)["pairs"]
    assert "old/expired" not in pairs


def test_pruning_preserves_pair_being_written(tmp_path):
    """The pair being written survives pruning even if its open_until is past:
    a re-failure after natural cooldown continues the streak (record-success
    semantics depend on the counter being retained)."""
    env = _env(tmp_path)
    _write_state(tmp_path, {"version": 1, "pairs": {
        "retry/me": {"failures": 3, "open_until": "2000-01-01T00:00:00+00:00",
                     "last_failure": "2000-01-01T00:00:00+00:00", "reason": "cooled"},
    }})
    p = run("record-failure", "retry", "me", "again", env_extra=env)
    assert p.returncode == 0
    c = _state(tmp_path)["pairs"]["retry/me"]
    assert c["failures"] == 4, "streak must continue across cooldown"
    assert c["reason"] == "again"
    assert c["open_until"] > c["last_failure"]


def test_status_readonly_no_pruning(tmp_path):
    """TR-024 contract: status --json STILL lists expired pairs as cooling
    (status is read-only; pruning happens on write)."""
    env = _env(tmp_path)
    _write_state(tmp_path, {"version": 1, "pairs": {
        "old/cooling": {"failures": 2, "open_until": "2000-01-01T00:00:00+00:00",
                        "last_failure": "2000-01-01T00:00:00+00:00", "reason": "old"},
    }})
    p = run("status", "--json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert data["pairs"][0]["pair"] == "old/cooling"
    assert data["pairs"][0]["state"] == "cooling"


# ----------------------------------------------------- status --json (TR-024) --

def test_status_json_contract_preserved(tmp_path):
    env = _env(tmp_path)
    p = run("record-failure", "provider-a", "model-1", "boom", env_extra=env)
    assert p.returncode == 0
    _write_state(tmp_path, {"version": 1, "pairs": {
        "provider-a/model-1": _state(tmp_path)["pairs"]["provider-a/model-1"],
        "provider-b/model-2": {"failures": 2, "open_until": "2000-01-01T00:00:00+00:00",
                               "last_failure": "2000-01-01T00:00:00+00:00", "reason": "old"},
    }})
    p = run("status", "--json", env_extra=env)
    assert p.returncode == 0
    data = json.loads(p.stdout)
    assert "pairs" in data
    by = {e["pair"]: e for e in data["pairs"]}
    assert by["provider-a/model-1"]["state"] == "OPEN"
    assert by["provider-a/model-1"]["failures"] == 1
    assert by["provider-a/model-1"]["reason"] == "boom"
    assert by["provider-b/model-2"]["state"] == "cooling"
    # provider filter still works
    p = run("status", "provider-a", "--json", env_extra=env)
    data = json.loads(p.stdout)
    assert [e["pair"] for e in data["pairs"]] == ["provider-a/model-1"]


# -------------------------------------------------------------- resilience --

def test_corrupt_state_file_tolerated(tmp_path):
    """FAIL-OPEN: corrupt state file never crashes the CLI (fresh state)."""
    env = _env(tmp_path)
    with open(os.path.join(str(tmp_path), "circuit-state.json"), "w") as f:
        f.write("{not json!!!")
    p = run("status", env_extra=env)
    assert p.returncode == 0
    assert "no open" in p.stdout
    p = run("record-failure", "provider-a", "model-1", env_extra=env)
    assert p.returncode == 0
    assert _state(tmp_path)["pairs"]["provider-a/model-1"]["failures"] == 1


def test_clear_forms(tmp_path):
    env = _env(tmp_path)
    run("record-failure", "provider-a", "model-1", env_extra=env)
    run("record-failure", "provider-b", "model-2", env_extra=env)
    p = run("clear", "provider-a", "model-1", env_extra=env)
    assert p.returncode == 0
    pairs = _state(tmp_path)["pairs"]
    assert "provider-a/model-1" not in pairs
    assert "provider-b/model-2" in pairs
    p = run("clear", "--all", env_extra=env)
    assert p.returncode == 0
    assert _state(tmp_path)["pairs"] == {}
