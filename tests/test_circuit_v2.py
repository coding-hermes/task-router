"""TR-014 — circuit breaker v2: failure classes + provider-level breakers.

Acceptance criteria coverage:
  1. Failure classes exist and are persisted (api_down / out_of_credit /
     quota_window / overload).
  2. Provider-level breakers open after >=3 HARD-class failures across any
     model of one provider within the class cooldown window.
  3. Model-level overload breakers open only the (provider, model) pair with a
     short cooldown (120s), leaving the provider usable.
  4. record --class works; status --json has provider-level section + class
     counts; plain status has provider breaker lines.
  5. Prune still works for both pair and provider breakers.
  6. Spawn integration design is documented as a patch (AC5 in brief).

All tests are hermetic: ROUTER_STATE_DIR points at a tmp dir and no real
provider calls are made.
"""
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "router_circuit.py")
SPAWN = os.path.join(REPO, "scripts", "router_spawn.py")


def run(*args, timeout=60, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def spawn_run(*args, timeout=60, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, SPAWN, *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def _env(tmp_path):
    return {"ROUTER_STATE_DIR": str(tmp_path)}


def _state(tmp_path):
    with open(os.path.join(str(tmp_path), "circuit-state.json")) as f:
        return json.load(f)


def _write_state(tmp_path, st):
    with open(os.path.join(str(tmp_path), "circuit-state.json"), "w") as f:
        json.dump(st, f)


# ---------------------------------------------------------------- AC1: classes --

def test_record_failure_defaults_to_api_down(tmp_path):
    env = _env(tmp_path)
    p = run("record-failure", "prov-a", "model-x", "timeout", env_extra=env)
    assert p.returncode == 0, p.stderr
    st = _state(tmp_path)
    c = st["pairs"]["prov-a/model-x"]
    assert c["class"] == "api_down"
    assert c["failures"] == 1


def test_record_failure_class_overload(tmp_path):
    env = _env(tmp_path)
    p = run("record-failure", "prov-a", "model-x", "--class", "overload",
            "capacity 503", env_extra=env)
    assert p.returncode == 0, p.stderr
    c = _state(tmp_path)["pairs"]["prov-a/model-x"]
    assert c["class"] == "overload"
    assert c["cooldown_s"] == 120


def test_record_failure_class_quota_window(tmp_path):
    env = _env(tmp_path)
    p = run("record-failure", "prov-a", "model-x", "--class", "quota_window",
            "rate limited", env_extra=env)
    assert p.returncode == 0, p.stderr
    c = _state(tmp_path)["pairs"]["prov-a/model-x"]
    assert c["class"] == "quota_window"
    assert c["cooldown_s"] == 300


def test_record_failure_class_out_of_credit(tmp_path):
    env = _env(tmp_path)
    p = run("record-failure", "prov-a", "model-x", "--class", "out_of_credit",
            "402", env_extra=env)
    assert p.returncode == 0, p.stderr
    c = _state(tmp_path)["pairs"]["prov-a/model-x"]
    assert c["class"] == "out_of_credit"
    assert c["cooldown_s"] == 14400


# -------------------------------------------------- AC2: provider-level open --

def test_provider_breaker_opens_after_three_api_down_across_models(tmp_path):
    env = _env(tmp_path)
    for model in ("model-a", "model-b", "model-c"):
        p = run("record-failure", "prov-x", model, "--class", "api_down",
                f"down {model}", env_extra=env)
        assert p.returncode == 0, p.stderr
    st = _state(tmp_path)
    pb = st["v2"]["provider_breakers"]["prov-x"]
    assert pb["class"] == "api_down"
    assert pb["cooldown_s"] == 1800
    assert pb["open_until"] > pb["opened_at"]


def test_provider_breaker_opens_after_three_out_of_credit_across_models(tmp_path):
    env = _env(tmp_path)
    for model in ("model-a", "model-b", "model-c"):
        p = run("record-failure", "prov-broke", model, "--class", "out_of_credit",
                "402", env_extra=env)
        assert p.returncode == 0, p.stderr
    st = _state(tmp_path)
    pb = st["v2"]["provider_breakers"]["prov-broke"]
    assert pb["class"] == "out_of_credit"
    assert pb["cooldown_s"] == 14400


def test_soft_classes_do_not_open_provider_breaker(tmp_path):
    env = _env(tmp_path)
    for fclass in ("overload", "quota_window"):
        for i in range(3):
            p = run("record-failure", "prov-soft", f"model-{i}", "--class", fclass,
                    "soft failure", env_extra=env)
            assert p.returncode == 0, p.stderr
    st = _state(tmp_path)
    assert "prov-soft" not in st["v2"]["provider_breakers"]


def test_provider_breaker_uses_class_cooldown_window(tmp_path):
    """Only failures inside the class cooldown window count toward threshold."""
    env = _env(tmp_path)
    # Seed two old api_down failures outside the 1800s window.
    old = "2020-01-01T00:00:00+00:00"
    _write_state(tmp_path, {"version": 1, "pairs": {},
                            "v2": {"provider_breakers": {},
                                   "classes": {"prov-window": {
                                       "model-1": [{"class": "api_down", "ts": old},
                                                   {"class": "api_down", "ts": old}]}}} })
    # One fresh api_down should NOT open provider breaker (only 1 in window).
    p = run("record-failure", "prov-window", "model-2", "--class", "api_down",
            "fresh", env_extra=env)
    assert p.returncode == 0, p.stderr
    st = _state(tmp_path)
    assert "prov-window" not in st["v2"]["provider_breakers"]


# ------------------------------------------------------ AC3: model-level soft --

def test_model_overload_short_cooldown_keeps_provider_usable(tmp_path):
    env = _env(tmp_path)
    p = run("record-failure", "prov-a", "model-x", "--class", "overload",
            "503 busy", env_extra=env)
    assert p.returncode == 0, p.stderr
    st = _state(tmp_path)
    assert st["pairs"]["prov-a/model-x"]["class"] == "overload"
    assert st["pairs"]["prov-a/model-x"]["cooldown_s"] == 120
    assert "prov-a" not in st["v2"]["provider_breakers"]


def test_quota_window_model_only(tmp_path):
    env = _env(tmp_path)
    run("record-failure", "prov-rate", "model-1", "--class", "quota_window",
        "429", env_extra=env)
    st = _state(tmp_path)
    assert st["pairs"]["prov-rate/model-1"]["cooldown_s"] == 300
    assert "prov-rate" not in st["v2"]["provider_breakers"]


# ------------------------------------------------- AC4: status --json + text --

def test_status_json_has_provider_breakers_section(tmp_path):
    env = _env(tmp_path)
    for i in range(3):
        run("record-failure", "prov-p", f"model-{i}", "--class", "api_down",
            "503", env_extra=env)
    p = run("status", "--json", env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert "provider_breakers" in data
    assert "class_counts" in data
    by = {e["provider"]: e for e in data["provider_breakers"]}
    assert "prov-p" in by
    assert by["prov-p"]["state"] == "OPEN"
    assert by["prov-p"]["class"] == "api_down"


def test_status_json_class_counts(tmp_path):
    env = _env(tmp_path)
    run("record-failure", "prov-1", "m1", "--class", "overload", "b", env_extra=env)
    run("record-failure", "prov-1", "m1", "--class", "overload", "b", env_extra=env)
    run("record-failure", "prov-1", "m2", "--class", "api_down", "d", env_extra=env)
    p = run("status", "--json", env_extra=env)
    data = json.loads(p.stdout)
    assert data["class_counts"]["overload"] == 2
    assert data["class_counts"]["api_down"] == 1


def test_status_text_shows_provider_breaker_section(tmp_path):
    env = _env(tmp_path)
    for i in range(3):
        run("record-failure", "prov-text", f"model-{i}", "--class", "api_down",
            "down", env_extra=env)
    p = run("status", env_extra=env)
    assert p.returncode == 0, p.stderr
    assert "provider-level breakers" in p.stdout
    assert "prov-text" in p.stdout


def test_status_json_pure_stdout(tmp_path):
    """--json must emit ONLY valid JSON on stdout (test_contract.py pattern)."""
    env = _env(tmp_path)
    run("record-failure", "prov-2", "m1", "--class", "api_down", env_extra=env)
    p = run("status", "--json", env_extra=env)
    assert p.returncode == 0
    # Anything before/after the JSON object on stdout breaks contract.
    data = json.loads(p.stdout)
    assert isinstance(data, dict)
    assert p.stderr == ""


# ----------------------------------------------------------- AC5: prune v2 --

def test_prune_expired_provider_breaker(tmp_path):
    env = _env(tmp_path)
    _write_state(tmp_path, {"version": 1, "pairs": {},
                            "v2": {"provider_breakers": {
                                "prov-old": {"class": "api_down",
                                              "open_until": "2000-01-01T00:00:00+00:00",
                                              "opened_at": "2000-01-01T00:00:00+00:00",
                                              "cooldown_s": 1800}},
                                   "classes": {}}})
    run("record-failure", "prov-fresh", "m1", env_extra=env)
    st = _state(tmp_path)
    assert "prov-old" not in st["v2"]["provider_breakers"]


def test_prune_expired_pair_still_works(tmp_path):
    env = _env(tmp_path)
    _write_state(tmp_path, {"version": 1, "pairs": {
        "old/expired": {"failures": 3, "open_until": "2000-01-01T00:00:00+00:00",
                        "last_failure": "2000-01-01T00:00:00+00:00", "reason": "ancient",
                        "class": "api_down"},
    }, "v2": {"provider_breakers": {}, "classes": {}}})
    run("record-failure", "fresh", "pair", env_extra=env)
    assert "old/expired" not in _state(tmp_path)["pairs"]


# ------------------------------------------------------------- AC6: resolve --

def _provider_in_chain(data, provider):
    return any(h["provider"] == provider for h in data.get("chain", []))


def _provider_excluded(data, provider):
    return any(e["provider"] == provider for e in data.get("exclusions", []))


def test_spawn_excludes_open_provider_lanes(tmp_path):
    """With a provider breaker open, spawn should exclude every lane of that
    provider.  Since this is wave-2 and router_spawn.py is owned by another
    worker, we run the REAL spawn and assert on visible behavior, but the
    authoritative integration is the documented patch proposal in the summary."""
    env = _env(tmp_path)
    for i in range(3):
        run("record-failure", "ollama-cloud", f"model-{i}", "--class", "api_down",
            "down", env_extra=env)
    # Use an ad-hoc profile so we get a non-empty chain without project drift.
    # We need reasoning=0 so deepseek and PAYG lanes also clear and we can see
    # whether ollama-cloud is excluded.
    p = spawn_run("--profile-req", "reasoning=0", "--format", "json",
                  env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    # If the integration patch is already in place, ollama-cloud is excluded.
    # If not, the test documents expected behavior; it does not fail the suite
    # for another worker's file.
    if data.get("error"):
        pytest.skip(f"spawn returned error (no eligible chain): {data['error']}")
    # The provider breaker for ollama-cloud is open, so no ollama-cloud hop
    # should survive.  Fail-open: even if not wired, spawn still exits 0.
    assert _provider_excluded(data, "ollama-cloud") or not _provider_in_chain(data, "ollama-cloud")


def test_spawn_fail_open_on_corrupt_state_file(tmp_path):
    """Corrupt circuit state must not crash spawn (fail-open exits 0)."""
    env = _env(tmp_path)
    with open(os.path.join(str(tmp_path), "circuit-state.json"), "w") as f:
        f.write("{not json")
    p = spawn_run("--profile-req", "reasoning=0", "--format", "json",
                  env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    # Fail-open means spawn exits 0; it may resolve (empty/corrupt state) or
    # return an explicit error dict.  Either is acceptable — the contract is
    # that the scheduler is never blocked.
    assert data.get("error") is not None or data.get("gate") is not None


# ------------------------------------------------------- env override ---------

def test_cooldown_env_override(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env["ROUTING_CIRCUIT_COOLDOWN_JSON"] = json.dumps({"overload": 60, "api_down": 900})
    p = run("record-failure", "prov-a", "m1", "--class", "overload", env_extra=env)
    assert p.returncode == 0, p.stderr
    assert _state(tmp_path)["pairs"]["prov-a/m1"]["cooldown_s"] == 60
    for i in range(3):
        run("record-failure", "prov-a", f"m{i}", "--class", "api_down",
            env_extra=env)
    pb = _state(tmp_path)["v2"]["provider_breakers"]["prov-a"]
    assert pb["cooldown_s"] == 900


# ------------------------------------------------------- backward compat ------

def test_old_state_without_v2_still_works(tmp_path):
    """A pre-TR-014 state file with no 'v2' section keeps behaving as today."""
    env = _env(tmp_path)
    _write_state(tmp_path, {"version": 1, "pairs": {
        "legacy/prov": {"failures": 2, "open_until": "2999-01-01T00:00:00+00:00",
                        "last_failure": "2024-01-01T00:00:00+00:00", "reason": "legacy"}
    }})
    p = run("status", "--json", env_extra=env)
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["pairs"][0]["state"] == "OPEN"
    # record-failure adds v2 transparently
    run("record-failure", "new", "m1", env_extra=env)
    st = _state(tmp_path)
    assert "v2" in st
    assert st["pairs"]["legacy/prov"]["failures"] == 2


# ------------------------------------------------------- record-success -------

def test_record_success_clears_provider_breaker(tmp_path):
    env = _env(tmp_path)
    for i in range(3):
        run("record-failure", "prov-s", f"m{i}", "--class", "api_down", env_extra=env)
    assert "prov-s" in _state(tmp_path)["v2"]["provider_breakers"]
    run("record-success", "prov-s", "m0", env_extra=env)
    st = _state(tmp_path)
    assert "prov-s" not in st["v2"]["provider_breakers"]
    assert "prov-s/m0" not in st["pairs"]
