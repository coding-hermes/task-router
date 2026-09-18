"""2026-09-18 — router_probefix classification tests (provider_rules explanations,
dead-id vs unexplained vs catalog-unavailable).

Hermetic: tmp data dir (ROUTING_DATA_DIR) + tmp state dir (ROUTER_STATE_DIR)
holding a synthetic health.jsonl. No network: every provider row points at
http://127.0.0.1:9 (connection refused) so the live-catalog fetch fails fast.

The point of the change under test: the daily scan must not present a KNOWN
probe artifact (opencode-go's missing x-opencode-session header) as "unexplained
work", and must not claim "the catalog serves the id" when it could not fetch a
catalog at all. Both classes are decided by DATA (provider_rules.jsonl rows
carrying `explains_probe`) — the code only reads it.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = ("/home/kara/.hermes/venvs/board/bin/python3"
      if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3") else sys.executable)
SCRIPT = os.path.join(REPO, "scripts", "router_probefix.py")


def _write(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _env(tmp_path, rules, health_runs, providers=None, keys=""):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    _write(data / "provider_rules.jsonl", rules)
    _write(data / "probe_fixes.jsonl", [])
    _write(data / "probe_excludes.jsonl", [])
    _write(data / "probe_gaps.jsonl", [])
    _write(data / "probe_providers.jsonl", providers or [])
    _write(state / "health.jsonl", health_runs)
    env_file = tmp_path / "env-file"
    env_file.write_text(keys)
    env = dict(os.environ)
    env.update({"ROUTING_DATA_DIR": str(data), "ROUTER_STATE_DIR": str(state),
                "MODELSDEV_CACHE": str(tmp_path / "no-such-cache.json"),
                "ROUTER_ENV_FILE": str(env_file)})
    return env, data, state


def _run(env, *args):
    return subprocess.run([PY, SCRIPT, *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)


def _run_with(providers_dict):
    return {"ts": "2026-09-18T00:00:00+00:00", "probe_version": 3,
            "providers": providers_dict}


def _down(model, err):
    return {"models": {model: {"status": "DOWN", "latency_ms": None,
                               "error": err, "probed_as": None, "note": None}}}


def test_explains_probe_row_classifies_artifact_and_writes_no_gap(tmp_path):
    """A provider_rules row with explains_probe:['400'] silences the class (data-driven)."""
    rules = [{"provider": "fake-go", "rule": "probe-400-artifact",
              "explains_probe": ["\\b400\\b"], "detail": "session header required",
              "valid_from": "2026-09-18", "valid_to": None}]
    health = [_run_with({"fake-go": _down("some-model", "HTTP 400")})]
    env, data, _ = _env(tmp_path, rules, health)
    p = _run(env, "--runs", "1")
    assert p.returncode == 0, p.stderr
    assert "EXPLAINED fake-go some-model" in p.stdout
    assert "1 explained by provider_rules" in p.stdout
    assert "0 unexplained" in p.stdout
    with open(data / "probe_gaps.jsonl") as f:
        assert f.read().strip() == "", "explained artifacts must not become gap rows"


def test_expired_rule_stops_explaining(tmp_path):
    """A contract with valid_to in the past no longer explains anything."""
    rules = [{"provider": "fake-go", "rule": "probe-400-artifact",
              "explains_probe": ["\\b400\\b"], "detail": "old contract",
              "valid_from": "2026-08-01", "valid_to": "2026-08-02"}]
    health = [_run_with({"fake-go": _down("some-model", "HTTP 400")})]
    env, data, _ = _env(tmp_path, rules, health)
    p = _run(env, "--runs", "1")
    assert p.returncode == 0, p.stderr
    assert "EXPLAINED" not in p.stdout
    assert "0 explained by provider_rules" in p.stdout


def test_unreachable_catalog_is_not_reported_as_serving_the_id(tmp_path):
    """No catalog fetch -> CATALOG-N/A, never the false 'catalog serves the id'."""
    providers = [{"id": "fake-go", "base_url": "http://127.0.0.1:9/v1",
                  "key_env": "FAKE_GO_KEY", "enabled": True, "default_model": "some-model"}]
    health = [_run_with({"fake-go": _down("some-model", "HTTP 404")})]
    env, data, _ = _env(tmp_path, [], health, providers=providers,
                        keys="FAKE_GO_KEY=dummy\n")
    p = _run(env, "--runs", "1")
    assert p.returncode == 0, p.stderr
    assert "CATALOG-N/A fake-go some-model" in p.stdout
    assert "catalog serves the id" not in p.stdout
    rows = [json.loads(l) for l in open(data / "probe_gaps.jsonl") if l.strip()]
    assert rows and rows[0]["action"].startswith("provider-catalog-unavailable")


def test_scan_reports_dead_id_separately(tmp_path):
    """Live catalog fetched WITHOUT the probed id -> DEAD-ID class, not UNEXPLAINED."""
    import http.server
    import threading
    import functools

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"data": [{"id": "other-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        providers = [{"id": "fake-go", "base_url": f"http://127.0.0.1:{port}/v1",
                      "key_env": "FAKE_GO_KEY", "enabled": True, "default_model": "gone-model"}]
        health = [_run_with({"fake-go": _down("gone-model", "HTTP 404")})]
        env, data, _ = _env(tmp_path, [], health, providers=providers,
                            keys="FAKE_GO_KEY=dummy\n")
        p = _run(env, "--runs", "1")
        assert p.returncode == 0, p.stderr
        assert "DEAD-ID fake-go gone-model" in p.stdout
        assert "1 dead-id(s)" in p.stdout
        assert "0 unexplained" in p.stdout
        rows = [json.loads(l) for l in open(data / "probe_gaps.jsonl") if l.strip()]
        assert rows and rows[0]["action"].startswith("id-absent-from-provider-catalog")
    finally:
        srv.shutdown()
