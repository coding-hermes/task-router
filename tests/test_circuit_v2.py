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
import importlib.util
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


def _state_or_none(tmp_path):
    """State as written, or None when nothing was ever written (the file's
    absence is itself evidence that no circuit event landed)."""
    p = os.path.join(str(tmp_path), "circuit-state.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


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


# ==================================================================== TR-190 ==
# The failure-class mapping as a TEST TABLE, not a manual falsifier: the table
# below is generated from FAILURE_CLASS_MAP itself, so a row added in the map
# without a test (or a test without a map row) is a visible failure, and every
# row's blast radius (class + window) is asserted from the same table.
# -----------------------------------------------------------------------------

_RC = None


def _load_circuit_module():
    """Import the real script once (test_circuit_hardening.py TR-078 pattern)."""
    global _RC
    if _RC is None:
        spec = importlib.util.spec_from_file_location(
            "router_circuit_under_test_tr190", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RC = mod
    return _RC


# kind -> (class, window_s). Every kind the proxy can emit (the classification
# seam's own vocabulary) with the class and window it must produce.
EXPECTED_KIND_MAP = {
    "timed out": ("overload", 120),
    "hop-wall-timeout": ("overload", 120),
    "idle-timeout": ("overload", 120),
    "idle-timeout (no bytes for 180s)": ("overload", 120),
    "429": ("quota_window", 300),
    "429 from upstream": ("quota_window", 300),
    "rate limit exceeded": ("quota_window", 300),
    "connection refused": ("api_down", 1800),
}


def test_failure_class_table_map_exists_and_covers_expected_kinds():
    """The centralized map exists, is frozen, and covers every expected kind."""
    rc = _load_circuit_module()
    mapping = getattr(rc, "FAILURE_CLASS_MAP", None)
    assert isinstance(mapping, dict) and mapping, \
        "router_circuit must expose a centralized FAILURE_CLASS_MAP"
    assert mapping == EXPECTED_KIND_MAP, (
        f"map drifted from the expected table:\n"
        f"  missing kinds: {sorted(set(EXPECTED_KIND_MAP) - set(mapping))}\n"
        f"  extra kinds:   {sorted(set(mapping) - set(EXPECTED_KIND_MAP))}")


def test_failure_class_table_every_row_class_and_window():
    """Rendered-table assertion: per kind, class + window (blast radius)."""
    rc = _load_circuit_module()
    rendered = []
    for kind in sorted(EXPECTED_KIND_MAP):
        fclass, window_s = rc.failure_class_for(kind)
        rendered.append(f"{kind:<34} -> {fclass:<13} window={window_s}s")
        assert fclass == EXPECTED_KIND_MAP[kind][0], kind
        assert window_s == EXPECTED_KIND_MAP[kind][1], kind
        # the window must be the class cooldown the circuit actually applies
        assert rc.CLASS_COOLDOWN_S[fclass] == window_s, kind
    print("\nTR-190 rendered mapping table:")
    print("  kind                                 -> class         window")
    for line in rendered:
        print("  " + line)


def test_failure_class_table_end_to_end_per_row(tmp_path):
    """Each kind, driven through the real CLI, lands as its table row: class
    on the pair, cooldown_s == window, and blast radius from the class."""
    env = _env(tmp_path)
    rc = _load_circuit_module()
    provider = "prov-table"
    for i, kind in enumerate(sorted(EXPECTED_KIND_MAP)):
        model = f"m{i}"
        p = run("record-failure", provider, model, "--kind", kind, "row", env_extra=env)
        assert p.returncode == 0, (kind, p.stderr)
        c = _state(tmp_path)["pairs"][f"{provider}/{model}"]
        fclass, window_s = EXPECTED_KIND_MAP[kind]
        assert c["class"] == fclass, kind
        assert c["cooldown_s"] == window_s, kind
        # blast radius cross-check against the live module taxonomy: the hard
        # class is the one that can open provider-wide breakers.
        assert rc.HARD_CLASSES == frozenset(("api_down", "out_of_credit"))
        if fclass in rc.HARD_CLASSES:
            assert rc.CLASS_COOLDOWN_S[fclass] == 1800 and fclass == "api_down", kind


def test_unmapped_failure_kind_fails_loudly_not_hard_default():
    """An unmapped kind must be an actionable error, never the hard default.

    The old behavior coerced unknown classes to api_down: a typo or a new kind
    silently opened PROVIDER-WIDE breakers — the 2026-09-25 lockup mechanism.
    """
    rc = _load_circuit_module()
    with pytest.raises(ValueError) as ei:
        rc.failure_class_for("socket hung up mid-stream")
    msg = str(ei.value)
    assert "socket hung up mid-stream" in msg, "error must name the unmapped kind"
    assert "FAILURE_CLASS_MAP" in msg, "error must point at the map to extend"


def test_cli_unmapped_kind_is_actionable_error_not_api_down(tmp_path):
    """The CLI surfaces the unmapped kind as exit 1 + message, and records
    nothing (no api_down pair, no provider breaker)."""
    env = _env(tmp_path)
    p = run("record-failure", "prov-typo", "m1", "--kind", "flaky-nic",
            env_extra=env)
    assert p.returncode == 1, (p.returncode, p.stdout, p.stderr)
    assert "flaky-nic" in (p.stderr + p.stdout)
    assert "FAILURE_CLASS_MAP" in (p.stderr + p.stdout)
    st = _state_or_none(tmp_path)
    assert not (st or {}).get("pairs"), "nothing may be recorded for an unmapped kind"
    assert not (st or {}).get("v2", {}).get("provider_breakers", {}), \
        "no provider breaker may open for an unmapped kind"


def test_no_hop_ledger_row_can_never_open_a_provider_breaker(tmp_path):
    """PRODUCTION INCIDENT GUARD (TR-182 lockup): a ledger row with no hop
    (provider=none, model=none) must never open a provider-wide breaker,
    no matter what class or reason reaches the state writer."""
    env = _env(tmp_path)
    # Seed an unrelated pair so the state file exists and the assertions below
    # always run against real written state (never vacuous).
    p = run("record-failure", "real-prov", "real-model", "--class", "overload",
            "seed", env_extra=env)
    assert p.returncode == 0, p.stderr
    for fclass in ("api_down", "out_of_credit", "overload", "quota_window"):
        p = run("record-failure", "none", "none", "--class", fclass,
                "no open hop for this request", env_extra=env)
        assert p.returncode == 0, (fclass, p.stderr)
        assert "SKIPPED" in p.stdout, (fclass, p.stdout)
    st = _state_or_none(tmp_path)
    assert st is not None, "the seeded pair must have written state"
    assert "real-prov/real-model" in st.get("pairs", {}), \
        "the guard must not interfere with real lanes"
    assert "none/none" not in st.get("pairs", {}), \
        "a no-hop row recorded a pair event"
    assert "none" not in (st.get("v2", {}).get("provider_breakers") or {}), \
        "a no-hop row opened a provider-wide breaker"
    assert (st.get("v2", {}).get("classes") or {}).get("none") is None, \
        "a no-hop row recorded class events"


def test_failure_class_for_is_case_insensitive_and_strips(kind="Timed Out "):
    """Reason text arrives lowercased/stripped already, but the map must not
    depend on that: mixed case and stray spaces resolve to the same row."""
    rc = _load_circuit_module()
    assert rc.failure_class_for(kind) == ("overload", 120)
