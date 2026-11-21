"""t_neuralwatt_probe_coverage — the health plane must NAME every routable
provider it cannot see.

The defect this pins (measured 2026-11-21): `provider_health_probe.py`
enumerates from the CURATED `probe_providers.jsonl`, while `router_spawn.py`
treats an ABSENT provider in `health-state.json` as un-gated (fail-open). So a
provider with live lanes and no probe row can serve traffic with nothing to
filter on. neuralwatt answered HTTP 402 on every chat call and 38 lanes stayed
in chains (83 hops across 9 of 11 profiles) from 2026-11-16 until the lanes
were disabled by hand.

A check that cannot fail is worthless, so every arm is exercised in BOTH
directions: the uncovered shape must FAIL and name the provider, the covered
shape must PASS, and a provider that is active but NOT routable (policy-gated,
or absent from quota-state.json) must never be reported as a gap — that is the
false-alarm direction, and it is what keeps the check trustworthy.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)
CHECK = os.path.join(REPO, "scripts", "health_coverage_check.py")
PROBE = os.path.join(REPO, "scripts", "provider_health_probe.py")
sys.path.insert(0, os.path.join(REPO, "scripts"))
import health_coverage_check as hcc  # noqa: E402

TODAY = "2026-11-21"


# ---------------------------------------------------------------------------
# pure layer — active lanes
# ---------------------------------------------------------------------------
def test_active_lane_counts_skips_disabled_archived_and_retired():
    models = [
        {"provider": "live", "model": "a"},
        {"provider": "live", "model": "b"},
        {"provider": "off", "model": "a", "disabled": True},
        {"provider": "gone", "model": "a", "archive": True},
        {"provider": "old", "model": "a", "valid_to": "2026-11-01"},
        {"provider": "future", "model": "a", "valid_to": "2027-01-01"},
        {"provider": "archprov", "model": "a"},
    ]
    providers = [{"id": "archprov", "archive": True}, {"id": "live"}]
    active, dropped = hcc.active_lane_counts(models, providers, TODAY)
    assert active == {"live": 2, "future": 1}
    assert dropped["off"] == "all lanes disabled"
    assert dropped["gone"] == "all lanes archived"
    assert dropped["old"] == "all lanes retired (valid_to past)"
    assert dropped["archprov"] == "provider row archived"
    # a provider with one live lane is never in the dropped map
    assert "live" not in dropped


def test_retirement_is_a_date_compare_not_a_presence_test():
    """A FUTURE valid_to must not retire the lane (the announced-decommission
    case would otherwise hide a live lane weeks early)."""
    assert hcc.is_retired({"valid_to": "2026-11-20"}, TODAY) is True
    assert hcc.is_retired({"valid_to": "2026-11-21"}, TODAY) is True
    assert hcc.is_retired({"valid_to": "2026-11-22"}, TODAY) is False
    assert hcc.is_retired({}, TODAY) is False


# ---------------------------------------------------------------------------
# pure layer — the policy gate (what makes a lane actually routable)
# ---------------------------------------------------------------------------
def test_routable_requires_an_open_quota_row():
    active = {"open-prov": 3, "gated-prov": 1, "missing-prov": 1}
    quota = {"providers": {"open-prov": {"status": "open"},
                           "gated-prov": {"status": "gated"}}}
    routable, excluded = hcc.routable_providers(active, quota["providers"])
    assert routable == {"open-prov": 3}
    assert "policy-gate-missing-row" in excluded["missing-prov"]
    assert excluded["gated-prov"] == "policy gate gated"


# ---------------------------------------------------------------------------
# pure layer — the report, both directions
# ---------------------------------------------------------------------------
def _report(models, providers, probe_rows, health, quota):
    return hcc.coverage_report(models, providers, probe_rows, health, quota, TODAY)


def test_uncovered_routable_provider_is_a_named_gap():
    """The neuralwatt shape: routable lanes, no health entry -> FAIL + named."""
    report = _report(
        [{"provider": "neuralwatt", "model": "nw-flash"}],
        [{"id": "neuralwatt", "api_base_url": "https://api.neuralwatt.com/v1",
          "api_key_env": "NEURALWATT_API_KEY"}],
        [{"id": "deepseek", "base_url": "https://api.deepseek.com/v1"}],
        {"providers": {"deepseek": {"status": "OK"}}},
        {"providers": {"neuralwatt": {"status": "open"}}})
    assert report["status"] == hcc.FAIL
    assert [g["provider"] for g in report["gaps"]] == ["neuralwatt"]
    assert report["gaps"][0]["lanes"] == 1
    assert report["gaps"][0]["kind"] == "registry-resolvable"
    text = "\n".join(hcc.render(report))
    assert "ACTIVE BUT UNPROBED (1)" in text and "neuralwatt" in text


def test_covered_provider_passes():
    report = _report(
        [{"provider": "deepseek", "model": "deepseek-flash"}],
        [{"id": "deepseek"}],
        [{"id": "deepseek", "base_url": "https://api.deepseek.com/v1"}],
        {"providers": {"deepseek": {"status": "OK"}}},
        {"providers": {"deepseek": {"status": "open"}}})
    assert report["status"] == hcc.PASS
    assert report["gaps"] == []
    assert "every routable provider has a health entry" in "\n".join(hcc.render(report))


def test_active_but_policy_excluded_provider_is_not_a_gap():
    """The false-alarm direction (measured 2026-11-21: aws-bedrock had 50 active
    lanes and no health entry, but its quota row is `gated`, so the router never
    routes to it — reporting it as a gap would train the reader to ignore the
    check)."""
    report = _report(
        [{"provider": "aws-bedrock", "model": "amazon.nova-lite-v1:0"},
         {"provider": "gw-deepseek", "model": "deepseek-flash"}],
        [{"id": "aws-bedrock"}, {"id": "gw-deepseek", "archive": True}],
        [],
        {"providers": {}},
        {"providers": {"aws-bedrock": {"status": "gated"}}})
    assert report["status"] == hcc.PASS
    assert report["gaps"] == []
    assert report["policy_excluded"]["aws-bedrock"] == "policy gate gated"
    # the archived-provider lane is named too, never silently dropped
    assert report["inactive"]["gw-deepseek"] == "provider row archived"
    text = "\n".join(hcc.render(report))
    assert "aws-bedrock" in text and "gw-deepseek" in text


def test_stale_health_entries_are_reported_not_counted_as_gaps():
    report = _report(
        [{"provider": "deepseek", "model": "deepseek-flash"}],
        [{"id": "deepseek"}],
        [],
        {"providers": {"deepseek": {"status": "OK"}, "crof": {"status": "DISABLED"}}},
        {"providers": {"deepseek": {"status": "open"}}})
    assert report["status"] == hcc.PASS
    assert report["stale_health_entries"] == ["crof"]


# ---------------------------------------------------------------------------
# pure layer — gap classification (the fix has to be obvious)
# ---------------------------------------------------------------------------
def test_classify_gap_kinds():
    providers = [
        {"id": "aliasprov", "api_base_url": "https://api.meta.ai/v1",
         "api_key_env": "META_MODEL_API_KEY"},
        {"id": "resolvable", "api_base_url": "https://api.example.com/v1",
         "api_key_env": "EXAMPLE_KEY"},
        {"id": "sigv4"},
    ]
    probe_rows = [{"id": "meta-model", "base_url": "https://api.meta.ai/v1"},
                  {"id": "already-probed", "base_url": "https://x/v1"}]
    assert hcc.classify_gap("already-probed", providers, probe_rows)["kind"] == "probe-row"
    alias = hcc.classify_gap("aliasprov", providers, probe_rows)
    assert alias["kind"] == "alias" and "meta-model" in alias["detail"]
    assert hcc.classify_gap("resolvable", providers, probe_rows)["kind"] == "registry-resolvable"
    assert hcc.classify_gap("sigv4", providers, probe_rows)["kind"] == "unprobeable"


# ---------------------------------------------------------------------------
# CLI — exit codes (a check that cannot look never reports PASS)
# ---------------------------------------------------------------------------
def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _cli_fixture(tmp_path, models, providers, probe_rows, health, quota):
    data_dir = tmp_path / "data" / "tables"
    _write_jsonl(str(data_dir / "models.jsonl"), models)
    _write_jsonl(str(data_dir / "providers.jsonl"), providers)
    _write_jsonl(str(data_dir / "probe_providers.jsonl"), probe_rows)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "health-state.json").write_text(json.dumps(health))
    (state / "quota-state.json").write_text(json.dumps(quota))
    return ["--data-dir", str(data_dir),
            "--health-state", str(state / "health-state.json"),
            "--quota-state", str(state / "quota-state.json"),
            "--today", TODAY]


def _run(argv, timeout=60):
    return subprocess.run([PY, CHECK] + argv, cwd=REPO, capture_output=True,
                          text=True, timeout=timeout)


def test_cli_exits_1_and_names_the_gap(tmp_path):
    argv = _cli_fixture(
        tmp_path,
        [{"provider": "muse-code", "model": "muse-spark-1.2"}],
        [{"id": "muse-code", "api_base_url": "https://api.meta.ai/v1",
          "api_key_env": "META_MODEL_API_KEY"}],
        [{"id": "meta-model", "base_url": "https://api.meta.ai/v1"}],
        {"providers": {"meta-model": {"status": "OK"}}},
        {"providers": {"muse-code": {"status": "open"}}})
    proc = _run(argv)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "muse-code" in proc.stdout and "[alias]" in proc.stdout


def test_cli_exits_0_when_covered(tmp_path):
    argv = _cli_fixture(
        tmp_path,
        [{"provider": "deepseek", "model": "deepseek-flash"}],
        [{"id": "deepseek"}],
        [{"id": "deepseek", "base_url": "https://api.deepseek.com/v1"}],
        {"providers": {"deepseek": {"status": "OK"}}},
        {"providers": {"deepseek": {"status": "open"}}})
    proc = _run(argv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "every routable provider has a health entry" in proc.stdout


def test_cli_exits_2_when_it_cannot_look(tmp_path):
    """No health-state.json = the gate input is missing. That is NOT a pass."""
    data_dir = tmp_path / "data" / "tables"
    _write_jsonl(str(data_dir / "models.jsonl"),
                 [{"provider": "deepseek", "model": "deepseek-flash"}])
    proc = _run(["--data-dir", str(data_dir),
                 "--health-state", str(tmp_path / "nope.json"),
                 "--quota-state", str(tmp_path / "nope-quota.json")])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "could not look" in proc.stdout
    # ... and a missing models table is the same class
    proc = _run(["--data-dir", str(tmp_path / "empty"),
                 "--health-state", str(tmp_path / "nope.json")])
    assert proc.returncode == 2


def test_cli_json_is_machine_readable(tmp_path):
    argv = _cli_fixture(
        tmp_path,
        [{"provider": "muse-code", "model": "muse-spark-1.2"}],
        [{"id": "muse-code"}],
        [],
        {"providers": {}},
        {"providers": {"muse-code": {"status": "open"}}})
    proc = _run(argv + ["--json"])
    assert proc.returncode == 1
    doc = json.loads(proc.stdout)
    assert doc["status"] == "fail"
    assert doc["gaps"][0]["provider"] == "muse-code"


# ---------------------------------------------------------------------------
# the hourly probe asserts coverage too (the surface that actually runs)
# ---------------------------------------------------------------------------
class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        blob = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


@pytest.fixture()
def fake_carrier():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _probe_fixture(tmp_path, base, models, providers, probe_rows, quota):
    data_dir = tmp_path / "data" / "tables"
    _write_jsonl(str(data_dir / "models.jsonl"), models)
    _write_jsonl(str(data_dir / "providers.jsonl"), providers)
    _write_jsonl(str(data_dir / "probe_providers.jsonl"),
                 [{"base_url": base, "enabled": True, **row} for row in probe_rows])
    state = tmp_path / "mr-state"
    state.mkdir(exist_ok=True)
    (state / "quota-state.json").write_text(json.dumps(quota))
    (tmp_path / ".hermes").mkdir(exist_ok=True)
    (tmp_path / ".hermes" / ".env").write_text("TCOV_KEY=sk-cov\n")
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path), "ROUTING_DATA_DIR": str(data_dir),
                "ROUTER_STATE_DIR": str(state),
                "ROUTING_REGISTRY": str(tmp_path / "no-such-registry.json")})
    return env, str(tmp_path / "out" / "health-state.json")


def test_probe_report_names_the_unprobed_routable_provider(tmp_path, fake_carrier):
    env, out_file = _probe_fixture(
        tmp_path, fake_carrier,
        [{"provider": "covered", "model": "m-c"},
         {"provider": "unprobed", "model": "m-u"}],
        [{"id": "covered"}, {"id": "unprobed"}],
        [{"id": "covered", "key_env": "TCOV_KEY", "default_model": "m-c"}],
        {"providers": {"covered": {"status": "open"},
                       "unprobed": {"status": "open"}}})
    proc = subprocess.run([PY, PROBE, "--output", out_file], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[:600]
    assert "ACTIVE BUT UNPROBED (1)" in proc.stdout, proc.stdout[-1500:]
    assert "unprobed" in proc.stdout
    state = json.load(open(out_file))
    assert [g["provider"] for g in state["unprobed_routable"]] == ["unprobed"]
    # the probed provider is covered, so it is not in the gap list
    assert "covered" in state["providers"]


def test_probe_report_says_covered_when_nothing_is_missing(tmp_path, fake_carrier):
    env, out_file = _probe_fixture(
        tmp_path, fake_carrier,
        [{"provider": "covered", "model": "m-c"}],
        [{"id": "covered"}],
        [{"id": "covered", "key_env": "TCOV_KEY", "default_model": "m-c"}],
        {"providers": {"covered": {"status": "open"}}})
    proc = subprocess.run([PY, PROBE, "--output", out_file], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[:600]
    assert "health coverage: all 1 routable providers have a health entry" in proc.stdout
    assert json.load(open(out_file))["unprobed_routable"] == []
