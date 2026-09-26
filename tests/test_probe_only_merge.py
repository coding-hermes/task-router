"""TR-164 regression tests — probe --only merges state (never replaces) +
opencode session-header invariants + quota/budget error fidelity.

health-state.json is a GATE input (router_spawn drops every hop whose provider
is DOWN and reads absent providers as fail-open), so a partial `--only` run
must not delete the fleet's gate state.

Hermetic (same rules as test_probe_flags): fake providers on a loopback HTTP
server, HOME/ROUTING_DATA_DIR/--output under tmp_path, no real network, no
repo writes.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable  # CI / fresh clone: no Bane-host venv
)
PROBE = os.path.join(REPO, "scripts", "provider_health_probe.py")
PROBE_PROVIDERS = os.path.join(REPO, "data", "tables", "probe_providers.jsonl")
sys.path.insert(0, os.path.join(REPO, "scripts"))
import provider_health_probe as php  # noqa: E402


# ---------------------------------------------------------------------------
# loopback fake provider: 200 pong / 429 budget-exceeded, keyed by bearer key
# ---------------------------------------------------------------------------

class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        auth = self.headers.get("Authorization", "")
        _SERVER["requests"].append({
            "path": self.path,
            "auth": auth,
            "opencode_session": self.headers.get("x-opencode-session"),
        })
        key = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if key == "sk-healthy":
            self._respond(200, {"choices": [{"message": {"content": "pong"}}]})
        elif key == "sk-budget":
            self._respond(429, {"error": {"message": "Account budget exceeded"}})
        elif key in ("sk-oc", "sk-oc2"):
            # the REAL carrier contract with the session header wired (data fix,
            # TR-164): the gate answers with its quota condition, not a 400
            # MissingSessionID config error.
            self._respond(429, {"error": {"message": "Account budget exceeded"}})
        else:
            self._respond(401, {"error": {"message": "bad key"}})

    def _respond(self, code, obj):
        blob = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


_SERVER = {"httpd": None, "port": None, "requests": []}


def _start_fake():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    _SERVER["httpd"] = httpd
    _SERVER["port"] = httpd.server_address[1]
    _SERVER["base"] = f"http://127.0.0.1:{_SERVER['port']}"
    return _SERVER["base"]


def _stop_fake():
    if _SERVER["httpd"] is not None:
        _SERVER["httpd"].shutdown()
        _SERVER["httpd"].server_close()
        _SERVER["httpd"] = None
    _SERVER["requests"] = []


def _fixture(tmp_path, providers, extra_rows=()):
    """probe_providers.jsonl with the given fake provider rows + a .env with
    every key. base_url points at the loopback fake. extra_rows are appended
    verbatim (enabled carriers for unprobed providers)."""
    base = _SERVER["base"]
    data_dir = tmp_path / "data" / "tables"
    data_dir.mkdir(parents=True)
    with open(data_dir / "probe_providers.jsonl", "w") as f:
        for row in providers:
            # row's own enabled flag wins; default True (base_url always forced)
            f.write(json.dumps({"base_url": base, "enabled": True, **row}) + "\n")
        for row in extra_rows:
            f.write(json.dumps({"base_url": base, "enabled": True, **row}) + "\n")
    hermes = tmp_path / ".hermes"
    hermes.mkdir(exist_ok=True)
    (hermes / ".env").write_text(
        "T164_HEALTHY_KEY=sk-healthy\n"
        "T164_BUDGET_KEY=sk-budget\n"
        "T164_OC_KEY=sk-oc\n"
        "T164_OC2_KEY=sk-oc2\n"
        "T164_PLAIN_TKN=sk-plain\n")
    return {
        "HOME": str(tmp_path),
        "ROUTING_DATA_DIR": str(data_dir),
        "ROUTER_STATE_DIR": str(tmp_path / "mr-state"),
        "ROUTING_REGISTRY": str(tmp_path / "no-such-registry.json"),
    }


def _run(argv, env, timeout=30):
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(argv, cwd=REPO, env=full_env, capture_output=True,
                          text=True, timeout=timeout)


def _out(tmp_path):
    return str(tmp_path / "out" / "health-state.json")


def _seed_state(out_file, entries):
    """Pre-existing health-state.json as a full run would have left it."""
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    prev = {"updated": "2026-09-26T00:00:00+00:00", "probe_version": 3,
            "providers": entries}
    with open(out_file, "w") as f:
        json.dump(prev, f, indent=1)
    return prev


_GHOST = {"status": "OK", "model": "ghost-model", "latency_ms": 1234,
          "error": None, "models": {"ghost-model": {"status": "OK",
                                                    "latency_ms": 1234,
                                                    "ts": "2026-09-26T00:00:00+00:00"}},
          "model_stats": {"ok": 1, "slow": 0, "overloaded": 0, "timeout": 0,
                          "down": 0, "total": 1},
          "credits": {"source": "none", "note": "no balance endpoint"},
          "ts": "2026-09-26T00:00:00+00:00"}


def _jsonl_rows(out_file):
    path = out_file.rsplit(".", 1)[0] + ".jsonl"
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------------------
# AC1 — --only merges into the previous provider map, never replaces it
# ---------------------------------------------------------------------------

def test_only_run_merges_unprobed_providers_into_state(tmp_path):
    out_file = _out(tmp_path)
    ghosts = {f"ghost-{i}": dict(_GHOST) for i in range(3)}
    _seed_state(out_file, ghosts)
    env = _fixture(tmp_path, [
        {"id": "healthy", "key_env": "T164_HEALTHY_KEY", "default_model": "m-h"},
    ], extra_rows=[
        # the real incident shape: the other 25 providers are IN the data file,
        # just not selected by --only — and they must survive the run
        {"id": f"ghost-{i}", "key_env": "T164_PLAIN_TKN", "default_model": "m-g"}
        for i in range(3)
    ])
    proc = _run([PY, PROBE, "--only", "healthy", "--output", out_file], env)
    assert proc.returncode == 0, proc.stderr[:400]
    state = json.load(open(out_file))
    provs = state["providers"]
    # every unprobed provider survives, byte-for-byte (JSON-semantic identity)
    for name, prev_entry in ghosts.items():
        assert name in provs, f"--only deleted gate state for {name}"
        assert (json.dumps(provs[name], sort_keys=True)
                == json.dumps(prev_entry, sort_keys=True)), (
            f"unprobed provider {name} was rewritten")
    # ... and the probed one was refreshed
    assert provs["healthy"]["status"] in ("OK", "SLOW"), provs["healthy"]
    assert provs["healthy"]["ts"] != _GHOST["ts"]


def test_only_run_jsonl_append_carries_unprobed_providers(tmp_path):
    """The jsonl history row is what router_probefix scans; a partial run's row
    must carry the carried-forward providers too (not just the probed ones)."""
    out_file = _out(tmp_path)
    ghosts = {"ghost-a": dict(_GHOST)}
    _seed_state(out_file, ghosts)
    env = _fixture(tmp_path, [
        {"id": "healthy", "key_env": "T164_HEALTHY_KEY", "default_model": "m-h"},
    ], extra_rows=[
        {"id": "ghost-a", "key_env": "T164_PLAIN_TKN", "default_model": "m-g"},
    ])
    proc = _run([PY, PROBE, "--only", "healthy", "--output", out_file], env)
    assert proc.returncode == 0, proc.stderr[:400]
    rows = _jsonl_rows(out_file)
    # a manual seed writes only health-state.json (the probe appends the run row)
    assert len(rows) == 1, f"expected exactly the run row in the jsonl, got {len(rows)}"
    latest = rows[-1]["providers"]
    assert (json.dumps(latest["ghost-a"], sort_keys=True)
            == json.dumps(_GHOST, sort_keys=True))
    assert latest["healthy"]["status"] in ("OK", "SLOW")


def test_only_error_path_never_writes_state(tmp_path):
    """Fail-closed: an --only typo exits 2 and leaves the previous state file
    untouched (no probing, no writing)."""
    out_file = _out(tmp_path)
    _seed_state(out_file, {"ghost-a": dict(_GHOST)})
    before = open(out_file, "rb").read()
    env = _fixture(tmp_path, [
        {"id": "healthy", "key_env": "T164_HEALTHY_KEY", "default_model": "m-h"},
    ])
    proc = _run([PY, PROBE, "--only", "not-a-provider", "--output", out_file], env)
    assert proc.returncode == 2
    assert open(out_file, "rb").read() == before, "--only error path modified state"
    assert _jsonl_rows(out_file) == []


def test_full_run_without_previous_state_bootstraps(tmp_path):
    """No previous state -> the merged map IS the fresh result (no regression
    to a broken bootstrap)."""
    env = _fixture(tmp_path, [
        {"id": "healthy", "key_env": "T164_HEALTHY_KEY", "default_model": "m-h"},
        {"id": "plainprov", "key_env": "T164_PLAIN_TKN", "default_model": "m-p"},
    ])
    out_file = _out(tmp_path)
    proc = _run([PY, PROBE, "--output", out_file], env)
    assert proc.returncode == 0, proc.stderr[:400]
    provs = json.load(open(out_file))["providers"]
    assert set(provs) == {"healthy", "plainprov"}
    assert provs["healthy"]["status"] in ("OK", "SLOW")
    assert len(_jsonl_rows(out_file)) == 1


# ---------------------------------------------------------------------------
# AC3 — opencode-go session header invariants (data>code, kept tested)
# ---------------------------------------------------------------------------

def test_opencode_probe_rows_carry_required_session_header():
    """The carrier answers HTTP 400 MissingSessionID without x-opencode-session;
    both opencode-go rows must carry it in the DATA file and the probe must
    map it through load_provider_headers()."""
    rows = {}
    with open(PROBE_PROVIDERS) as f:
        for line in f:
            line = line.strip()
            if line:
                row = json.loads(line)
                rows[row["id"]] = row
    for pid in ("opencode-go", "opencode-go-2"):
        row = rows.get(pid)
        assert row is not None, f"{pid} missing from probe_providers.jsonl"
        assert row.get("enabled") is not False, f"{pid} disabled"
        hdr = row.get("headers") or {}
        assert hdr.get("x-opencode-session"), f"{pid} missing x-opencode-session"
    headers = php.load_provider_headers()
    for pid in ("opencode-go", "opencode-go-2"):
        assert headers.get(pid, {}).get("x-opencode-session"), (
            f"load_provider_headers() did not map {pid}'s session header")


def test_probe_sends_session_header_and_reports_quota_not_missing_session(tmp_path):
    """End to end on a loopback carrier: the header reaches the wire, and a 429
    budget condition is reported as itself — never as MissingSessionID."""
    _SERVER["requests"] = []
    env = _fixture(tmp_path, [
        {"id": "opencode-go", "key_env": "T164_OC_KEY",
         "default_model": "glm-5.3-flash",
         "headers": {"x-opencode-session": "ses_router_health_probe"}},
        {"id": "opencode-go-2", "key_env": "T164_OC2_KEY",
         "default_model": "glm-5.3-flash",
         "headers": {"x-opencode-session": "ses_router_health_probe"}},
    ])
    out_file = _out(tmp_path)
    proc = _run([PY, PROBE, "--only", "opencode-go,opencode-go-2",
                 "--output", out_file], env)
    assert proc.returncode == 0, proc.stderr[:400]
    # header hit the wire
    assert _SERVER["requests"], "probe never reached the fake carrier"
    for req in _SERVER["requests"]:
        assert req["path"] == "/chat/completions"
        assert req["opencode_session"] == "ses_router_health_probe", (
            f"x-opencode-session not sent for {req['auth']}")
    # the REAL condition (quota/budget) is on the record, not a config error
    state = json.load(open(out_file))
    for pid in ("opencode-go", "opencode-go-2"):
        mm = state["providers"][pid]["models"]["glm-5.3-flash"]
        assert mm["status"] == "DOWN", mm
        err = (mm.get("error") or "").lower()
        assert "429" in err, f"{pid}: status code not reported: {mm['error']}"
        assert "budget" in err, f"{pid}: quota condition not reported: {mm['error']}"
        assert "missingsessionid" not in err, (
            f"{pid}: config-error leak: {mm['error']}")


def test_ping_reports_http_body_on_non_5xx_error():
    """The pure layer: HTTPError bodies (quota/budget text) must survive into
    the DOWN error; 503 keeps its dedicated OVERLOADED contract."""
    class _FakeErr(Exception):
        pass

    class _HTTPErr(urllib.error.HTTPError):
        pass

    def fake_req(base, key, model, params, timeout, extra_headers=None):
        return {"status": "HTTPERR", "code": 429,
                "http_body": '{"error":{"message":"Account budget exceeded"}}',
                "latency_ms": 5}

    orig = php._req
    php._req = fake_req
    try:
        r = php.ping("http://x", "k", "m")
    finally:
        php._req = orig
    assert r["status"] == "DOWN"
    assert "429" in r["error"] and "budget exceeded" in r["error"].lower()

    def fake_req_503(base, key, model, params, timeout, extra_headers=None):
        return {"status": "HTTPERR", "code": 503, "latency_ms": 5,
                "http_body": "overloaded"}

    php._req = fake_req_503
    try:
        r = php.ping("http://x", "k", "m")
    finally:
        php._req = orig
    assert r["status"] == "OVERLOADED" and "overloaded" in r["error"]

    def fake_req_nobody(base, key, model, params, timeout, extra_headers=None):
        return {"status": "HTTPERR", "code": 429, "latency_ms": 5}

    php._req = fake_req_nobody
    try:
        r = php.ping("http://x", "k", "m")
    finally:
        php._req = orig
    assert r["status"] == "DOWN" and r["error"] == "HTTP 429"


def test_removed_provider_is_pruned_from_state(tmp_path):
    """The one legitimate deletion: a provider removed/disabled in the data file
    must not keep a stale gate entry alive via merge (it would gate forever on
    a dead row). Removal is data-file truth, never a side effect of --only."""
    out_file = _out(tmp_path)
    _seed_state(out_file, {"ghost-a": dict(_GHOST),
                           "healthy": {**_GHOST, "status": "DOWN"}})
    env = _fixture(tmp_path, [
        {"id": "healthy", "key_env": "T164_HEALTHY_KEY", "default_model": "m-h"},
    ])
    proc = _run([PY, PROBE, "--only", "healthy", "--output", out_file], env)
    assert proc.returncode == 0, proc.stderr[:400]
    provs = json.load(open(out_file))["providers"]
    assert "ghost-a" not in provs, "removed provider survived the merge"
    assert provs["healthy"]["status"] in ("OK", "SLOW")


def test_disabled_data_row_is_pruned_from_state(tmp_path):
    """enabled=false in the data file = removed for gating purposes."""
    out_file = _out(tmp_path)
    _seed_state(out_file, {"ghost-a": dict(_GHOST)})
    env = _fixture(tmp_path, [
        {"id": "healthy", "key_env": "T164_HEALTHY_KEY", "default_model": "m-h"},
        {"id": "retired", "key_env": "T164_PLAIN_TKN", "default_model": "m-r",
         "enabled": False},
    ], extra_rows=[
        {"id": "ghost-a", "key_env": "T164_PLAIN_TKN", "default_model": "m-g"},
    ])
    proc = _run([PY, PROBE, "--output", out_file], env)
    assert proc.returncode == 0, proc.stderr[:400]
    provs = json.load(open(out_file))["providers"]
    assert "ghost-a" in provs, "unprobed-but-present provider was deleted"
    assert "retired" not in provs, "disabled data row kept a live gate entry"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fake_carrier():
    """Start the loopback fake provider for every test in this module."""
    _start_fake()
    try:
        yield
    finally:
        _stop_fake()
