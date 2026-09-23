"""TR-REVIEW-001 — the canary-grade /health probe.

`scripts/router_health_probe.py` is the thing that actually FAILS on a
deliberately stale registry (acceptance criterion 2). Two layers are covered:

- the pure judgement function, against every payload shape it must classify
  (healthy / stale / red gate / unrunnable gate / unknown commit);
- the probe end-to-end against a REAL server process, whose registry home this
  suite ages BY HAND — so the "fail on stale" claim is demonstrated against a
  serving instance, not asserted against a fixture dict.

Exit-code contract (mirrored from the script docstring):
  0 PASS | 1 FAIL (answered and unhealthy) | 2 CANNOT RUN (no answer).
"""
import contextlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if Path("/home/kara/.hermes/venvs/board/bin/python3").exists()
    else sys.executable
)
PROBE = SCRIPTS / "router_health_probe.py"
SERVER = SCRIPTS / "router_server.py"

sys.path.insert(0, str(SCRIPTS))
import router_health_probe as probe  # noqa: E402

_MODEL_ROW = {
    "provider": "fakeprov", "model": "fake-model", "normalized_price": 1.0,
    "plan_tier": 1, "token_factor": 1.0, "data_class": "zdr",
    "disabled": False, "archive": False, "valid_to": None,
}


# ---------------------------------------------------------------------------
# Layer 1 — the judgement function
# ---------------------------------------------------------------------------

HEALTHY = {
    "status": "ok", "commit": "df592c4c2d09",
    "gate": {"valid": True, "failed_checks": [], "checks": 13},
    "registry_age": {"exists": True, "stale": False, "age_h": 1.0,
                     "path": "/repo/registry.json", "lag_s": 0.3},
}


def test_judge_passes_a_healthy_payload():
    failures, obs = probe.judge(dict(HEALTHY))
    assert failures == []
    assert obs["commit"] == "df592c4c2d09"
    assert obs["gate_valid"] is True
    assert obs["stale"] is False


def test_judge_fails_a_stale_registry():
    payload = {**HEALTHY,
               "registry": {},
               "registry_age": {**HEALTHY["registry_age"], "stale": True,
                                "lag_s": 37855.0, "content_match": False,
                                "newest_table": "models.jsonl"}}
    failures, _ = probe.judge(payload)
    assert len(failures) == 1
    assert "STALE" in failures[0]
    assert "37855" in failures[0]


def test_judge_fails_a_red_gate():
    payload = {**HEALTHY,
               "gate": {"valid": False, "failed_checks": ["freshness"],
                        "issues": ["freshness: stale registry"]}}
    failures, _ = probe.judge(payload)
    assert len(failures) == 1
    assert "gate INVALID" in failures[0]
    assert "freshness" in failures[0]


def test_judge_distinguishes_an_unrunnable_gate_from_a_red_one():
    """"could not run" and "the data is bad" are different failures, and the
    probe must not collapse them into one message."""
    payload = {**HEALTHY,
               "gate": {"valid": None, "error": "validator exploded"}}
    failures, _ = probe.judge(payload)
    assert len(failures) == 1
    assert "could not run" in failures[0]

    payload = {**HEALTHY, "gate": {"valid": None, "failed_checks": []}}
    failures, _ = probe.judge(payload)
    assert len(failures) == 1
    assert "did not run" in failures[0]


def test_judge_fails_an_unknown_commit():
    payload = {**HEALTHY, "commit": "unknown"}
    failures, _ = probe.judge(payload)
    assert any("commit unresolved" in f for f in failures)


def test_judge_fails_a_missing_registry():
    payload = {**HEALTHY,
               "registry_age": {"exists": False, "stale": None,
                                "path": "/repo/registry.json"}}
    failures, _ = probe.judge(payload)
    assert any("registry missing" in f for f in failures)
    # ... and absent is NOT reported as stale (the distinctions must stay apart)
    assert not any("STALE" in f for f in failures)


def test_judge_reports_every_broken_block_at_once():
    payload = {"commit": "unknown",
               "gate": {"valid": False, "failed_checks": ["registry.exists"]},
               "registry_age": {"exists": False}}
    failures, _ = probe.judge(payload)
    assert len(failures) == 3, failures


def test_judge_enforces_the_absolute_age_cap():
    payload = {**HEALTHY,
               "registry_age": {**HEALTHY["registry_age"], "age_h": 80.0}}
    failures, _ = probe.judge(payload, max_age_h=48)
    assert len(failures) == 1 and "exceeds the 48.00h cap" in failures[0]
    # no cap -> the same payload passes (the cap is opt-in, never implied)
    assert probe.judge(payload)[0] == []


def test_judge_fails_on_malformed_blocks():
    failures, _ = probe.judge({"commit": "abc1234", "gate": None,
                               "registry_age": "nope"})
    assert len(failures) == 2
    assert any("gate block" in f for f in failures)
    assert any("registry_age block" in f for f in failures)


# ---------------------------------------------------------------------------
# Layer 2 — the probe against a REAL server, with a deliberately stale registry
# ---------------------------------------------------------------------------

def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _touch(path, age_s=0.0):
    ts = time.time() - age_s
    path.touch()
    import os
    os.utime(path, (ts, ts))


def _registry_home(tmp_path):
    """A registry home whose validate gate is green (see test_health_plane)."""
    data_dir = tmp_path / "tables"
    data_dir.mkdir(parents=True)
    (data_dir / "task_profiles.jsonl").write_text(
        json.dumps({"id": "P0_TEST", "title": "fixture profile"}) + "\n")
    (data_dir / "task_profile_requirements.jsonl").write_text(
        json.dumps({"task_id": "P0_TEST", "category": "reasoning", "level": 3}) + "\n")
    (data_dir / "models.jsonl").write_text(json.dumps(_MODEL_ROW) + "\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({
        "version": 3, "generated_at": "2026-09-01T00:00:00+00:00",
        "tables": {"models": [_MODEL_ROW]},
    }))
    _touch(reg)
    return {"ROUTING_REGISTRY": str(reg), "ROUTING_DATA_DIR": str(data_dir),
            "ROUTER_STATE_DIR": str(state_dir)}, reg


@contextlib.contextmanager
def _serve(env, port):
    import os
    proc = subprocess.Popen(
        [PY, str(SERVER), "--mode", "read-only", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=REPO, env={**os.environ, **env},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import urllib.error
        import urllib.request
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"server exited {proc.returncode}: {out} {err}")
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/status", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        else:
            pytest.fail("server did not become ready")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _run_probe(url, *extra):
    return subprocess.run(
        [PY, str(PROBE), "--url", url, "--json", *extra],
        cwd=REPO, capture_output=True, text=True, timeout=60)


def test_probe_passes_against_a_healthy_instance(tmp_path):
    env, _ = _registry_home(tmp_path)
    port = _free_port()
    with _serve(env, port):
        proc = _run_probe(f"http://127.0.0.1:{port}")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["status"] == "pass"
    assert payload["failures"] == []
    assert payload["commit"] != "unknown"


def test_probe_fails_when_the_registry_is_deliberately_made_stale(tmp_path):
    """AC2, end to end: start healthy, confirm PASS, age the registry on the
    running home, restart against the same home, confirm FAIL.

    The restart is deliberate: the freshness verdict is read per request, but
    the server resolves its registry path at start, so the fixture mutates the
    FILES the already-resolved home points at — the same home, a stale tree."""
    env, reg = _registry_home(tmp_path)
    data_dir = Path(env["ROUTING_DATA_DIR"])
    port = _free_port()

    with _serve(env, port):
        healthy = _run_probe(f"http://127.0.0.1:{port}")
    assert healthy.returncode == 0, (healthy.stdout, healthy.stderr)

    # --- break freshness deliberately ---------------------------------------
    _touch(reg, age_s=7200)                       # registry two hours old
    models = data_dir / "models.jsonl"
    models.write_text(json.dumps(_MODEL_ROW) + "\n"
                      + json.dumps({**_MODEL_ROW, "model": "m2"}) + "\n")
    _touch(models)                                # tables rewritten now

    with _serve(env, port):
        stale = _run_probe(f"http://127.0.0.1:{port}")
    assert stale.returncode == 1, (stale.stdout, stale.stderr)
    payload = json.loads(stale.stdout)
    assert payload["status"] == "fail"
    assert payload["registry_stale"] is True
    assert payload["gate_valid"] is False
    assert any("STALE" in f for f in payload["failures"]), payload["failures"]
    assert any("gate INVALID" in f for f in payload["failures"]), payload["failures"]


def test_probe_fails_on_the_absolute_age_cap(tmp_path):
    """--max-age-h is the 'reseed daily' knob: a non-stale but old registry."""
    env, reg = _registry_home(tmp_path)
    _touch(reg, age_s=3 * 3600)                   # 3h old, content still matches
    port = _free_port()
    with _serve(env, port):
        url = f"http://127.0.0.1:{port}"
        relaxed = _run_probe(url, "--max-age-h", "48")
        strict = _run_probe(url, "--max-age-h", "1")
    assert relaxed.returncode == 0, relaxed.stdout
    assert strict.returncode == 1, strict.stdout
    payload = json.loads(strict.stdout)
    assert any("exceeds the 1.00h cap" in f for f in payload["failures"])
    # ... and it is NOT reported as stale — age caps and staleness are separate
    assert payload["registry_stale"] is False


def test_probe_reports_cannot_run_when_nothing_is_listening():
    port = _free_port()          # bound then released: nothing is listening
    proc = _run_probe(f"http://127.0.0.1:{port}")
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["status"] == "cannot_run"
    assert payload["reason"]


def test_probe_human_output_is_key_value_lines(tmp_path):
    """Without --json the output is the key=value dialect the canary's shell
    helpers parse (grep '^key=')."""
    env, _ = _registry_home(tmp_path)
    port = _free_port()
    with _serve(env, port):
        proc = subprocess.run(
            [PY, str(PROBE), "--url", f"http://127.0.0.1:{port}"],
            cwd=REPO, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert any(ln.startswith("commit=") for ln in lines)
    assert any(ln.startswith("gate_valid=") for ln in lines)
    assert any(ln.startswith("registry_stale=") for ln in lines)
