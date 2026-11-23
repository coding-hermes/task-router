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
_RS = None


def _load_once():
    """Import the real script module once per session (TR-078: one load)."""
    global _RC
    if _RC is None:
        spec = importlib.util.spec_from_file_location("router_circuit_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RC = mod
    return _RC


def _rs_once():
    """Import the real server module once per session (TR-360 sections)."""
    global _RS
    if _RS is None:
        path = os.path.join(REPO, "scripts", "router_server.py")
        spec = importlib.util.spec_from_file_location("router_server_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RS = mod
    return _RS


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


def _state_or_empty(tmp_path):
    """The circuit state after a test where recording may have been refused
    entirely (no record-* call ever runs -> no state file is written)."""
    st_path = os.path.join(str(tmp_path), "circuit-state.json")
    if not os.path.exists(st_path):
        return {"pairs": {}, "v2": {"provider_breakers": {}}}
    with open(st_path) as f:
        return json.load(f)


def _breaker_providers(st):
    """Provider-level breaker ids currently held in state (`v2` may be absent
    on a state file a legacy writer produced)."""
    v2 = st.get("v2") or {}
    return set((v2.get("provider_breakers") or {}).keys())


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


# ------------------------------------------------ TR-360 gateway concurrency --
#
# The gateway's own concurrency limiter ("Too many concurrent runs (max 10)")
# poisoned the circuit store when the scheduler saturated the shared gateway
# (scheduler max-concurrent == gateway cap 10): every saturated (provider,
# model) pair's 429 was recorded as its own pair event and FOUR provider-level
# breakers opened while the providers were healthy. The gateway arm now
# classifies NOTHING into pair or provider breakers; a genuine provider-side
# 429 with a different body still records exactly as before, and every
# existing TR-340 marker keeps its behaviour.
#
# Subject: router_server._proxy_record / _demote_on_taxonomy /
# _classify_hop_failure (same subject as tests/test_proxy_circuit_hygiene.py).
# The circuit CLI is re-routed onto the REAL router_circuit module through the
# SAME in-process `call` harness this file owns, with STATE pointed at the
# test's tmp dir — so every assertion below reads a real circuit-state.json.

_GATEWAY_BODY = {
    "error": "Gateway Error: 'Too many concurrent runs (max 10)' "
             "for this gateway slot pool"
}
_PROVIDER_BODY = {
    "error": "upstream HTTP 429: provider monthly cap reached"
}


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """router_server with its circuit shell-out re-routed in-process.

    The ledger row is stubbed away (router_outcomes writes real files; the
    circuit, not the ledger, is the subject here — same split
    test_proxy_circuit_hygiene pins as 'the ledger row is still written').
    Everything _proxy_record/_demote_on_taxonomy would have exec'd as
    `router_circuit.py ...` runs through the real circuit module instead and
    lands in this test's ROUTER_STATE_DIR / circuit-state.json.
    """
    rs = _rs_once()
    import router_outcomes as ro

    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(ro, 'append_row_fast', lambda *a, **k: None)
    monkeypatch.setattr(ro, 'accumulate_row', lambda *a, **k: None)
    monkeypatch.setattr(rs, 'router_outcomes', ro, raising=False)

    rc = _load_once()
    rc.STATE = os.path.join(str(tmp_path), "circuit-state.json")

    def in_process_circuit(script, args, timeout=60):
        assert script == "router_circuit.py", (script, args)
        return call(rc, *args)

    monkeypatch.setattr(rs, '_subprocess_text', in_process_circuit)
    return rs


# --- the classifier helper itself (no recording involved) ---

def test_gateway_concurrency_marker_normalised_matching(server):
    rs = server
    gw = rs._GATEWAY_CONCURRENCY_MARKER
    assert gw == 'too many concurrent runs'
    assert rs._is_gateway_concurrency('Too many concurrent runs (max 10)')
    assert rs._is_gateway_concurrency(_GATEWAY_BODY["error"])
    # case/whitespace normalisation: re-wrapped and shouted bodies still match
    assert rs._is_gateway_concurrency('TOO   many\nconcurrent\tRUNS (max 10)')
    # and NOTHING else is exempted — a provider-side 429 must not match
    assert not rs._is_gateway_concurrency(_PROVIDER_BODY["error"])
    assert not rs._is_gateway_concurrency('rate limit exceeded, retry after 5s')
    assert not rs._is_gateway_concurrency('Too many concurrent REQUESTS (max 10)')
    assert not rs._is_gateway_concurrency('')
    assert rs._is_gateway_concurrency(None) is False
    # TR-340 markers are UNCHANGED by the new predicate's existence
    for marker in ('no open hop', 'unservable-rating', 'registry-missing',
                   'no eligible model'):
        assert rs._is_router_generated(marker), marker
        assert not rs._is_gateway_concurrency(marker), marker


def test_gateway_429_body_classifies_gateway_capacity_not_provider(server):
    """The hop classifier routes the gateway body to its own pair-scoped arm
    — NOT to the shared 429 taxonomy whose classes demote state."""
    rs = server
    code, detail = rs._classify_hop_failure(status=429, body=dict(_GATEWAY_BODY))
    assert code == rs._HOP_GATEWAY_CONCURRENCY, (code, detail)
    assert 'gateway concurrency cap' in detail
    # control: the SAME status with a genuine provider body still taxonomises
    ctrl_code, _ = rs._classify_hop_failure(status=429, body=dict(_PROVIDER_BODY))
    assert ctrl_code != rs._HOP_GATEWAY_CONCURRENCY
    assert ctrl_code.startswith('429'), ctrl_code


def test_gateway_arm_opens_zero_new_pairs_and_zero_provider_breakers(
        server, tmp_path):
    """RED-to-GREEN: the buggy behaviour recorded every saturated pair's 429
    as an api_down/pair event (and 3-of-a-class opened provider breakers).
    The gateway arm must leave the circuit store untouched."""
    rs = server
    # two saturated pairs + the demote arm, mirroring the live multi-pair arm
    for prov, model in (("gw-prov-a", "model-a"), ("gw-prov-b", "model-b")):
        rs._proxy_record(prov, model, False, {"matrix": {}},
                         reason=_GATEWAY_BODY["error"][:200],
                         latency_s=0.5, route_outcome='failed', hops_attempted=1)
        code, detail = rs._classify_hop_failure(status=429, body=dict(_GATEWAY_BODY))
        rs._demote_on_taxonomy(prov, model, code, detail)
    st = _state_or_empty(tmp_path)
    assert st.get("pairs", {}) == {}, \
        f"gateway-concurrency 429 must open NO pair, saw: {list(st.get('pairs', {}))}"
    assert _breaker_providers(st) == set(), \
        f"gateway-concurrency 429 must open NO provider breaker, saw: {_breaker_providers(st)}"


def test_control_arm_genuine_429_still_opens_its_pair(server, tmp_path):
    """Control: the SAME code path with a provider-limit body (no gateway
    text) still records the pair — the exemption is not a blanket 429 rule."""
    rs = server
    rs._proxy_record("ctl-prov", "ctl-model", False, {"matrix": {}},
                     reason=_PROVIDER_BODY["error"][:200],
                     latency_s=0.4, route_outcome='failed', hops_attempted=1)
    st = _state(tmp_path)
    pair = st["pairs"]["ctl-prov/ctl-model"]
    assert pair["failures"] >= 1
    assert pair["open_until"] > pair["last_failure"], "the pair must be OPEN"


def test_control_arm_429_taxonomy_still_demotes_pair(server, tmp_path):
    """Control: a genuine 429-capacity code with a provider body still walks
    the taxonomy demotion path and records the (provider, model) pair."""
    rs = server
    code, detail = rs._classify_hop_failure(status=429, body=dict(_PROVIDER_BODY))
    rs._demote_on_taxonomy("ctl-tax-prov", "ctl-tax-model", code, detail)
    st = _state(tmp_path)
    pair = st["pairs"]["ctl-tax-prov/ctl-tax-model"]
    assert pair["failures"] >= 1


def test_gateway_arm_demote_records_nothing(server, tmp_path):
    """The demote path fed by the REAL classify output of the gateway body
    must not shell out to the circuit (pair or provider) either."""
    rs = server
    code, detail = rs._classify_hop_failure(status=429, body=dict(_GATEWAY_BODY))
    rs._demote_on_taxonomy("gw-d-prov", "gw-d-model", code, detail)
    st = _state_or_empty(tmp_path)
    assert st.get("pairs", {}) == {}
    assert _breaker_providers(st) == set()
    # and the raw-text arm ('Too many concurrent runs' inside the detail of a
    # legacy taxonomy code) is refused too — the demote guard checks BOTH
    rs._demote_on_taxonomy("gw-e-prov", "gw-e-model", "429-capacity",
                           "upstream HTTP 429: Too many concurrent runs (max 10)")
    st = _state_or_empty(tmp_path)
    assert st.get("pairs", {}) == {}
    assert _breaker_providers(st) == set()


def test_control_demote_legacy_code_raw_gateway_text_is_refused(server, tmp_path):
    """The refusal is marker-scoped, not code-scoped: a LEGACY taxonomy code
    whose detail happens to carry the gateway marker is also refused, which
    is exactly how the live arm reached the demote path pre-fix."""
    rs = server
    # proof the refusal lives in the TEXT match, not only the code name:
    # the same legacy code with a NON-gateway detail still records
    rs._demote_on_taxonomy("ctl-x-prov", "ctl-x-model", "429-capacity",
                           "upstream HTTP 429: provider monthly cap reached")
    assert "ctl-x-prov/ctl-x-model" in _state(tmp_path)["pairs"]
    # and swapping ONLY the marker-bearing token suppresses the record
    rs._demote_on_taxonomy("ctl-y-prov", "ctl-y-model", "429-capacity",
                           "upstream HTTP 429: Too many concurrent runs (max 10)")
    assert "ctl-y-prov/ctl-y-model" not in _state_or_empty(tmp_path).get("pairs", {})


def test_tr340_still_refused_via_new_gateway_paths(server, tmp_path):
    """No existing TR-340 marker behaviour changed: the 'no open hop' body
    still classifies none-generated, still records nothing, and the new
    predicate never claims it."""
    rs = server
    code, detail = rs._classify_hop_failure(status=503, body={
        "error": "no open hop for this request"})
    assert code == "none-generated", (code, detail)
    rs._proxy_record("tr340-prov", "tr340-model", False, {"matrix": {}},
                     reason="no open hop for this request",
                     route_outcome='failed', hops_attempted=1)
    st = _state_or_empty(tmp_path)
    assert st.get("pairs", {}) == {}
    assert _breaker_providers(st) == set()
