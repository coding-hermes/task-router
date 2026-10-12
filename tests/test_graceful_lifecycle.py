"""Graceful restart contract: stop admission, report not-ready, drain boundedly."""
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from router_drain import DrainController  # noqa: E402


def test_starts_ready_and_counts_inflight_requests():
    lifecycle = DrainController()
    assert lifecycle.ready is True
    assert lifecycle.inflight == 0

    lifecycle.request_started()
    assert lifecycle.inflight == 1
    lifecycle.request_finished()
    assert lifecycle.inflight == 0


def test_begin_drain_flips_readiness_before_waiting_for_requests():
    lifecycle = DrainController()
    lifecycle.request_started()

    lifecycle.begin_drain()

    assert lifecycle.ready is False
    assert lifecycle.draining is True
    assert lifecycle.inflight == 1


def test_request_arriving_after_drain_is_counted_but_not_admitted():
    lifecycle = DrainController()
    lifecycle.begin_drain()
    assert lifecycle.request_started() is False
    assert lifecycle.inflight == 1
    lifecycle.request_finished()
    assert lifecycle.inflight == 0


def test_wait_for_idle_returns_after_the_last_inflight_request_finishes():
    lifecycle = DrainController()
    lifecycle.request_started()
    lifecycle.begin_drain()

    def finish_later():
        time.sleep(0.03)
        lifecycle.request_finished()

    worker = threading.Thread(target=finish_later)
    worker.start()
    assert lifecycle.wait_for_idle(timeout_s=1.0) is True
    worker.join(timeout=1.0)
    assert lifecycle.inflight == 0


def test_wait_for_idle_is_bounded_and_reports_timeout_without_faking_idle():
    lifecycle = DrainController()
    lifecycle.request_started()
    lifecycle.begin_drain()

    started = time.monotonic()
    assert lifecycle.wait_for_idle(timeout_s=0.05) is False
    assert time.monotonic() - started < 0.5
    assert lifecycle.inflight == 1


def test_request_count_cannot_go_negative():
    lifecycle = DrainController()
    with pytest.raises(RuntimeError, match="without a matching start"):
        lifecycle.request_finished()


def test_http_readyz_is_200_while_serving_and_503_after_drain():
    """Live handlers and deploy tooling need a machine-readable ready signal."""
    from router_server import RouterApplication

    app = RouterApplication("read-only", "")
    code, ready = app.dispatch("GET", "/readyz")
    assert code == 200
    assert ready["ready"] is True
    assert ready["draining"] is False
    assert ready["inflight"] == 0

    app.lifecycle.begin_drain()
    code, draining = app.dispatch("GET", "/readyz")
    assert code == 503
    assert draining["ready"] is False
    assert draining["draining"] is True


def test_sigterm_drains_real_inflight_http_request():
    """The installed SIGTERM path stops accepting, drains, and exits cleanly."""
    import signal
    from router_server import RouterApplication, RouterHTTPServer, _install_graceful_signals

    entered = threading.Event()
    release = threading.Event()
    app = RouterApplication("read-only", "")
    original = app.dispatch

    def dispatch(method, path, **kwargs):
        if path == "/slow-test":
            entered.set()
            assert release.wait(3.0)
            return 200, {"ok": True}
        return original(method, path, **kwargs)

    app.dispatch = dispatch
    server = RouterHTTPServer(("127.0.0.1", 0), app)
    serving, previous_signals = _install_graceful_signals(server)
    response = {}

    def request():
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/slow-test" % server.server_address[1],
                                        timeout=4) as r:
                response["status"] = r.status
                response["body"] = json.loads(r.read())
        except Exception as exc:
            response["error"] = repr(exc)

    client = threading.Thread(target=request)
    client.start()

    def signal_after_handler_starts():
        if not entered.wait(3.0):
            response["error"] = "slow handler never started"
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)
        release.set()

    trigger = threading.Thread(target=signal_after_handler_starts, daemon=True)
    trigger.start()
    try:
        serving.set()
        server.serve_forever()
        server.server_close()
        assert app.lifecycle.wait_for_idle(timeout_s=1.0) is True
    finally:
        release.set()
        for sig, previous in previous_signals.items():
            signal.signal(sig, previous)
        server.server_close()
        client.join(timeout=2.0)
        trigger.join(timeout=2.0)

    assert response == {"status": 200, "body": {"ok": True}}
    assert app.lifecycle.draining is True
    assert app.lifecycle.inflight == 0


def test_health_reports_loaded_build_identity():
    """Deploy verification must prove which source vintage the process loaded."""
    from router_server import RouterApplication

    app = RouterApplication("read-only", "")
    code, health = app.dispatch("GET", "/health")
    assert code == 200
    assert health["runtime"]["pid"] == os.getpid()
    assert health["runtime"]["source_revision"]
    assert health["runtime"]["source_dirty"] in (True, False)
    assert len(health["runtime"]["source_digest"]) == 64
    assert health["runtime"]["draining"] is False
