"""TR-255 — the stale watchdog: the thing that finally CONSUMES /health's code.stale.

TR-141 made every serving instance report its deploy parity honestly; nothing
read it, so :9092/:9391 served 2026-09-30 code for days with `stale: true`
printed on every /health and no alert anywhere. This suite pins the consumer
contract end to end:

- the pure judgement (evaluate_code): OK / STALE / indeterminate, the
  conservative resolution when the flag and the fields disagree (stale side
  wins — an extra restart is trivial, unnoticed drift is not), and the
  never-fabricate rule (a null verdict or a missing identity half is
  UNREACHABLE with the reason spelled out, not silence);
- the fetch layer (check) against a REAL local HTTP server: healthy payload,
  stale payload, non-JSON body, non-object JSON, missing `code` block,
  HTTP 404, and a dead port — every failure class becomes a line, never an
  exception;
- the CLI (main) exit codes: 0 all OK, 1 any STALE, 2 any UNREACHABLE,
  --report-only always 0, --json shape, and the URL resolution order
  (--url flags > ROUTER_STALE_URLS env > defaults).
"""
import http.server
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"

sys.path.insert(0, str(SCRIPTS))
try:
    import router_stale_watchdog as watchdog  # noqa: E402
except Exception as exc:
    pytest.skip(f"router_stale_watchdog not available: {exc}", allow_module_level=True)


# ---------------------------------------------------------------------------
# Fixtures: a real HTTP server thread serving canned /health payloads.
# ---------------------------------------------------------------------------

class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Canned /health responder. server.routes maps path -> (status, body)."""

    def do_GET(self):
        status, body = self.server.routes.get(
            self.path, (404, b'{"error": "not found"}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture()
def health_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    server.routes = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}", server
    server.shutdown()
    server.server_close()


#: What a FRESH instance reports: both halves of each comparison identical.
LOADED_COMMIT = "40d261c6cdfc"
LOADED_SHA = "7724f0277cdd4a02"
#: What moved past it in the measured 2026-10-02 incident (the STALE half).
MOVED_COMMIT = "c4687d655bb7"
MOVED_SHA = "51e035eb1eefe597"


def _payload(stale, loaded=LOADED_COMMIT, repo=LOADED_COMMIT,
             loaded_sha=LOADED_SHA, live_sha=LOADED_SHA,
             loaded_at="2026-09-30T18:38:01+00:00", error=None):
    """A /health code block shaped exactly like router_health.code_identity()."""
    return {
        "loaded_commit": loaded, "loaded_source_sha": loaded_sha,
        "loaded_at": loaded_at, "repo_commit": repo,
        "live_source_sha": live_sha, "stale": stale, "error": error,
    }


def _route(server, path, status, obj):
    server.routes[path] = (status, json.dumps(obj).encode())


def _fresh_port():
    """A port the OS just gave us and we gave back — nothing listens on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Layer 1 — the judgement function
# ---------------------------------------------------------------------------

def test_evaluate_ok_when_flag_and_fields_agree():
    verdict, detail = watchdog.evaluate_code(_payload(False))
    assert verdict == "OK"
    assert LOADED_COMMIT in detail and LOADED_SHA in detail
    assert "2026-09-30" in detail  # loaded_at is always part of the line


def test_evaluate_stale_when_flag_true():
    verdict, detail = watchdog.evaluate_code(
        _payload(True, repo=MOVED_COMMIT, live_sha=MOVED_SHA))
    assert verdict == "STALE"
    assert "restart" in detail.lower()
    assert "2026-09-30" in detail  # loaded_at — WHEN the drift started


def test_evaluate_stale_by_derived_fields_even_if_flag_true():
    """Same as above through the derived path: loaded sha != live sha."""
    verdict, _ = watchdog.evaluate_code(_payload(True, loaded_sha="a" * 16))
    assert verdict == "STALE"


def test_evaluate_conservative_when_flag_lies():
    """A flag claiming fresh while the fields disagree must NOT pass."""
    verdict, detail = watchdog.evaluate_code(_payload(False, repo="different"))
    assert verdict == "STALE"
    assert "UNDER-REPORTED" in detail


def test_evaluate_conservative_when_flag_overreports():
    """The inverse lie: flag stale but fields identical — still STALE.

    Deliberately over-strict: the cost of an extra restart is trivial, and a
    false-OK here would be the exact silence this watchdog exists to end.
    """
    verdict, _ = watchdog.evaluate_code(
        _payload(True, loaded=MOVED_COMMIT, repo=MOVED_COMMIT,
                 loaded_sha=MOVED_SHA, live_sha=MOVED_SHA))
    assert verdict == "STALE"


def test_evaluate_null_verdict_is_unreachable_with_reason():
    """router_health guarantees stale=None carries its `error` — surface it."""
    verdict, detail = watchdog.evaluate_code(_payload(None, error="loaded commit unknown at boot"))
    assert verdict == "UNREACHABLE"
    assert "indeterminate" in detail and "loaded commit unknown at boot" in detail


def test_evaluate_null_verdict_fields_disagree_is_stale():
    """null stale but comparable halves that disagree: stale wins, stated."""
    verdict, detail = watchdog.evaluate_code(
        _payload(None, repo=MOVED_COMMIT, live_sha=MOVED_SHA))
    assert verdict == "STALE"
    assert "null" in detail


def test_evaluate_no_identity_fields_flag_false_is_ok():
    """A bare `stale: false` with no fields to cross-check is the only OK."""
    verdict, _ = watchdog.evaluate_code({"stale": False})
    assert verdict == "OK"


def test_evaluate_no_identity_fields_null_is_unreachable():
    verdict, detail = watchdog.evaluate_code({})
    assert verdict == "UNREACHABLE"
    assert "no identity fields" in detail


# ---------------------------------------------------------------------------
# Layer 2 — URL configuration
# ---------------------------------------------------------------------------

def test_default_urls(monkeypatch):
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    assert watchdog.configured_urls([]) == list(watchdog.DEFAULT_URLS)


def test_env_override_urls(monkeypatch):
    monkeypatch.setenv(watchdog.ENV_URLS, " http://a:1, ,http://b:2 ")
    assert watchdog.configured_urls([]) == ["http://a:1", "http://b:2"]


def test_flag_urls_beat_env(monkeypatch):
    monkeypatch.setenv(watchdog.ENV_URLS, "http://a:1")
    assert watchdog.configured_urls(["http://b:2"]) == ["http://b:2"]


def test_env_blank_entries_drop_to_empty(monkeypatch):
    """An env that strips to nothing yields NO urls (not the defaults) —
    the operator asked for a specific list and it is empty; main() then
    refuses rather than silently checking the wrong hosts."""
    monkeypatch.setenv(watchdog.ENV_URLS, " , , ")
    assert watchdog.configured_urls([]) == []


# ---------------------------------------------------------------------------
# Layer 3 — the fetch layer against the real server
# ---------------------------------------------------------------------------

def test_check_ok(health_server):
    base, server = health_server
    _route(server, "/health", 200, {"code": _payload(False), "mode": "read-only"})
    result = watchdog.check(base, timeout_s=5)
    assert result["verdict"] == "OK"
    assert result["code"]["stale"] is False


def test_check_stale(health_server):
    base, server = health_server
    _route(server, "/health", 200, {"code": _payload(True)})
    result = watchdog.check(base, timeout_s=5)
    assert result["verdict"] == "STALE"
    assert "restart" in result["detail"].lower()


def test_check_http_404_names_missing_health_surface(health_server):
    base, server = health_server
    result = watchdog.check(base, timeout_s=5)
    assert result["verdict"] == "UNREACHABLE"
    assert "HTTP 404" in result["detail"]
    assert "no /health surface" in result["detail"]


def test_check_non_json_body(health_server):
    base, server = health_server
    server.routes["/health"] = (200, b"<html>gateway error</html>")
    result = watchdog.check(base, timeout_s=5)
    assert result["verdict"] == "UNREACHABLE"
    assert "not JSON" in result["detail"]


def test_check_non_object_json(health_server):
    base, server = health_server
    server.routes["/health"] = (200, b'["a", "list"]')
    result = watchdog.check(base, timeout_s=5)
    assert result["verdict"] == "UNREACHABLE"
    assert "list" in result["detail"]


def test_check_missing_code_block(health_server):
    base, server = health_server
    server.routes["/health"] = (200, json.dumps({"status": "ok"}).encode())
    result = watchdog.check(base, timeout_s=5)
    assert result["verdict"] == "UNREACHABLE"
    assert "code" in result["detail"] and "status" in result["detail"]


def test_check_dead_port_is_unreachable():
    base = f"http://127.0.0.1:{_fresh_port()}"
    result = watchdog.check(base, timeout_s=5)
    assert result["verdict"] == "UNREACHABLE"
    assert result["detail"]  # a reason, never an empty one


# ---------------------------------------------------------------------------
# Layer 4 — the CLI: exit codes and output shape
# ---------------------------------------------------------------------------

def test_main_exit_0_all_ok(health_server, monkeypatch):
    base, server = health_server
    _route(server, "/health", 200, {"code": _payload(False)})
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    assert watchdog.main(["--url", base]) == 0


def test_main_exit_1_on_stale(health_server, monkeypatch):
    base, server = health_server
    _route(server, "/health", 200, {"code": _payload(True)})
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    assert watchdog.main(["--url", base]) == 1


def test_main_exit_2_on_unreachable(health_server, monkeypatch):
    base, server = health_server  # no /health route registered -> 404
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    assert watchdog.main(["--url", base]) == 2


def test_main_stale_outranks_unreachable(health_server, monkeypatch):
    """STALE is the actionable contract signal — it must win the exit code."""
    base, server = health_server
    dead = f"http://127.0.0.1:{_fresh_port()}"
    _route(server, "/health", 200, {"code": _payload(True)})
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    assert watchdog.main(["--url", base, "--url", dead]) == 1


def test_main_report_only_always_zero(health_server, monkeypatch):
    base, server = health_server
    _route(server, "/health", 200, {"code": _payload(True)})
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    assert watchdog.main(["--url", base, "--report-only"]) == 0


def test_main_json_shape(health_server, monkeypatch, capsys):
    base, server = health_server
    _route(server, "/health", 200, {"code": _payload(False)})
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    rc = watchdog.main(["--url", base, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["report_only"] is False
    assert len(out["results"]) == 1
    row = out["results"][0]
    assert row["url"] == base
    assert row["verdict"] == "OK"
    assert isinstance(row["detail"], str) and row["detail"]


def test_main_trailing_slash_base(health_server, monkeypatch):
    """base_url.rstrip('/') + '/health' — a config with a trailing slash works."""
    base, server = health_server
    _route(server, "/health", 200, {"code": _payload(False)})
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    assert watchdog.main(["--url", base + "/"]) == 0


def test_main_requires_a_url_when_nothing_configured(monkeypatch):
    """With NO --url and NO env, main falls back to the DEFAULT fleet URLs
    (that is the right behavior) — so the only way main() can refuse is an
    env that strips to nothing: an operator asked for a list, got blanks,
    and must not have the defaults silently substituted."""
    monkeypatch.setenv(watchdog.ENV_URLS, " , ")
    with pytest.raises(SystemExit):
        watchdog.main([])


def test_main_line_output_has_verdict_and_url(health_server, monkeypatch, capsys):
    base, server = health_server
    _route(server, "/health", 200, {"code": _payload(True)})
    monkeypatch.delenv(watchdog.ENV_URLS, raising=False)
    watchdog.main(["--url", base])
    out = capsys.readouterr().out
    assert "STALE" in out and base in out
