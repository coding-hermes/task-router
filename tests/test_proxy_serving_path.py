"""TR-274: the bounded serving path.

The router's serving surface used to be one shape only: ThreadingHTTPServer
with a daemon thread per accepted connection. The thread count was bounded
only INDIRECTLY (the proxy admission semaphore bounds upstream-blocking work),
and nothing named the serving path anywhere an operator could see it.

This row adds an explicitly bounded alternative path and keeps the legacy one:

  * option parsing — `--serving` with explicit choices (async | thread) and
    the legacy default, plus the ROUTER_SERVING_MODE env override (CLI wins;
    an unknown env value fails closed to the default, loudly);
  * default preservation — the legacy ThreadingHTTPServer path is untouched
    and remains what a bare boot selects;
  * health mode — the ACTIVE serving field names the path, and the bounded
    path's pool carries its explicit bounds on /health;
  * bounded serving — 40 in-flight proxied requests complete with the pool
    holding its explicit worker bound (strictly below the request count),
    while every request gets an answer shaped exactly as the admission
    contract defines (served, or refused 429 with a ledger row — never a
    silent drop).

No network: the upstream hop is injected, the same seam the failure-envelope
and stream-client tests use.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import router_server as rsrv  # noqa: E402

PY = (
    "/home/kara/.hermes/venvs/board/bin/python3"
    if os.path.exists("/home/kara/.hermes/venvs/board/bin/python3")
    else sys.executable
)
SERVER = os.path.join(REPO, "scripts", "router_server.py")


# --------------------------------------------------------------------------- AC1

def test_help_prints_serving_flag_with_explicit_choices_and_thread_default():
    """`--help` names the serving flag with (async | thread); thread default."""
    proc = subprocess.run(
        [PY, SERVER, "--help"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    text = proc.stdout + proc.stderr
    assert "--serving" in text
    # The choices must be EXPLICIT on the flag line: {async,thread}.
    assert "{async,thread}" in text


def test_default_serving_mode_is_the_legacy_thread_path():
    assert rsrv.DEFAULT_SERVING_MODE == "thread"
    # A bare boot (no flag, no env) selects thread and reports it as ACTIVE.
    assert rsrv._serving_mode_from_cli_or_env(None) == "thread"
    assert rsrv._serving_mode_from_cli_or_env("thread") == "thread"
    assert rsrv._serving_mode_from_cli_or_env("async") == "async"


def test_default_boot_builds_the_legacy_thread_server(tmp_path, monkeypatch):
    """main() selects RouterHTTPServer (ThreadingHTTPServer) when unflagged.

    The boot is cut short right before serve_forever: the classifier
    self-check and the upstream probe are stubbed so no network is touched,
    and the server constructor is captured instead of run.
    """
    import importlib
    mod = importlib.import_module("router_server")

    captured = {}
    real_thread_server = mod.RouterHTTPServer
    real_async_server = mod.RouterHTTPServerAsync

    class _Stop(Exception):
        pass

    def _thread_cls(address, app):
        captured["cls"] = "thread"
        captured["server"] = real_thread_server(("127.0.0.1", 0), app)
        raise _Stop()

    def _async_cls(address, app):
        captured["cls"] = "async"
        captured["server"] = real_async_server(("127.0.0.1", 0), app)
        raise _Stop()

    def _run_main(monkeypatch_local, argv):
        # No classifier env -> the self-check short-circuits True (no network).
        monkeypatch_local.delenv("ROUTER_CLASSIFIER_BASE_URL", raising=False)
        # The upstream capabilities probe is advisory; stub it (no network).
        monkeypatch_local.setattr(mod, "_hermes_capabilities_metadata",
                                  lambda base: {"error": "skipped"})
        monkeypatch_local.setattr(mod, "RouterHTTPServer", _thread_cls)
        monkeypatch_local.setattr(mod, "RouterHTTPServerAsync", _async_cls)
        raised = None
        try:
            mod.main(argv)
        except _Stop as exc:
            raised = exc
        except SystemExit as exc:  # argparse/validation exits are failures here
            raise AssertionError(f"main() exited {exc}") from exc
        assert raised is not None, "main() reached serve_forever without a server"

    # default -> legacy server
    monkeypatch.delenv("ROUTER_SERVING_MODE", raising=False)
    _run_main(monkeypatch, ["--port", "0"])
    assert captured["cls"] == "thread"
    captured["server"].server_close()

    # --serving async -> bounded server
    _run_main(monkeypatch, ["--port", "0", "--serving", "async"])
    assert captured["cls"] == "async"
    captured["server"].server_close()

    # env override -> bounded server (CLI absent)
    monkeypatch.setenv("ROUTER_SERVING_MODE", "async")
    _run_main(monkeypatch, ["--port", "0"])
    assert captured["cls"] == "async"
    captured["server"].server_close()


def test_unknown_env_serving_mode_fails_closed_to_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ROUTER_SERVING_MODE", "greenlet-magic")
    assert rsrv._serving_mode_from_cli_or_env(None) == rsrv.DEFAULT_SERVING_MODE
    err = capsys.readouterr().err
    assert "greenlet-magic" in err and "thread" in err


# --------------------------------------------------------------------------- AC2

def test_health_serving_field_names_the_active_path(tmp_path, monkeypatch):
    """The active serving field names the path, on BOTH paths."""
    def _dispatch():
        # The /health branch lives in RouterApplication.dispatch; drive it
        # through a real RouterApplication to prove the wiring, with the
        # expensive health read stubbed.
        real_app = rsrv.RouterApplication.__new__(rsrv.RouterApplication)
        real_app.mode = "read-only"
        real_app.lifecycle = rsrv.router_drain.DrainController()
        real_app.runtime_identity = {"pid": os.getpid()}
        real_app.hermes_capabilities = {}
        real_app.openapi = rsrv.OPENAPI
        real_app.operations = real_app._operation_map()
        status, payload = real_app.dispatch("GET", "/health")
        assert status == 200
        return payload

    monkeypatch.setattr(rsrv.router_health, "health", lambda mode=None: {"ok": True})

    # legacy: field names thread, pool is honestly null
    rsrv._SERVING_MODE["name"] = None
    rsrv._SERVING_STATE["server"] = None
    payload = _dispatch()
    assert payload["serving"]["mode"] == "thread"
    assert payload["serving"]["pool"] is None
    # the admission ceiling stays observable beside it (AC4's second half)
    assert payload["admission"]["max_inflight"] >= 1

    # bounded: field names async, pool carries its explicit bounds
    rsrv._SERVING_MODE["name"] = "async"
    app = rsrv.RouterApplication.__new__(rsrv.RouterApplication)
    app.mode = "read-only"
    app.lifecycle = rsrv.router_drain.DrainController()
    server = rsrv.RouterHTTPServerAsync(("127.0.0.1", 0), app,
                                        pool_size=3, queue_bound=7)
    try:
        payload = _dispatch()
        assert payload["serving"]["mode"] == "async"
        assert payload["serving"]["pool"]["max_workers"] == 3
        assert payload["serving"]["pool"]["queue_bound"] == 7
        assert payload["serving"]["busy_workers"] == 0
    finally:
        server.server_close()
        rsrv._SERVING_STATE["server"] = None


# --------------------------------------------------------------------------- AC4

@pytest.fixture()
def proxy_env(tmp_path, monkeypatch):
    monkeypatch.setattr(rsrv, '_proxy_requirements', lambda b, h, p: (
        'declared', {'profile_id': 'P1_CODING', 'matrix': None,
                     'complexity_sig': None, 'problems': []}))
    monkeypatch.setattr(rsrv, '_proxy_record', lambda *a, **k: None)
    # AC4 wants the SERVING bound measured, not admission: raise the
    # admission ceiling above the pool under test and rebuild the lazily
    # created module semaphore so no earlier test's size leaks in.
    monkeypatch.setenv('ROUTER_PROXY_MAX_INFLIGHT', '32')
    monkeypatch.setenv('ROUTER_PROXY_QUEUE_MAX', '64')
    rsrv._ADMISSION_SEM = None
    return tmp_path


def _chain(*pairs):
    return {'chain': [{'hop': i + 1, 'provider': p, 'model': m, 'usd_1m': 0.1 * (i + 1),
                       'outcomes': {'stats_fallback': 'unconditioned'}}
                      for i, (p, m) in enumerate(pairs)],
            'sort': 'price'}


def test_40_inflight_requests_hold_fewer_than_40_serving_threads(
        proxy_env, monkeypatch):
    """AC4: 40 in-flight proxied requests, strictly fewer upstream-blocked
    serving threads.

    Each fake hop holds its slot ~0.25s and the gauge records the PEAK number
    concurrently upstream-blocked. With a pool of 8 and 40 client requests in
    flight, the peak must stay at or below the pool — never one thread per
    request — while every request still receives a shaped answer (nothing
    silently dropped).
    """
    REQUESTS = 40
    POOL = 8
    HOP_HOLD_S = 0.25

    gauge_lock = threading.Lock()
    gauge = {"active": 0, "peak": 0, "entered": 0}

    def upstream(path, body, headers):
        with gauge_lock:
            gauge["entered"] += 1
            gauge["active"] += 1
            gauge["peak"] = max(gauge["peak"], gauge["active"])
        try:
            time.sleep(HOP_HOLD_S)  # hold in flight
            return 200, {'choices': [{'index': 0, 'message': {'role': 'assistant',
                                                              'content': 'ok'},
                                      'finish_reason': 'stop'}],
                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}
        finally:
            with gauge_lock:
                gauge["active"] -= 1

    monkeypatch.setattr(rsrv, '_proxy_chain', lambda reqs, **k: _chain(('p1', 'm1')))
    # The handler carries no upstream arg, so the ladder falls to the module
    # seam — inject the fake hop there or requests would leave for the real
    # configured gateway.
    monkeypatch.setattr(rsrv, '_UPSTREAM_CALL', upstream)

    app = rsrv.RouterApplication.__new__(rsrv.RouterApplication)
    app.mode = "read-only"
    app.lifecycle = rsrv.router_drain.DrainController()
    app.runtime_identity = {"pid": os.getpid()}
    app.hermes_capabilities = {}
    app.openapi = rsrv.OPENAPI
    app.operations = app._operation_map()
    # The HTTP surface gates POSTs (TR-067): mirror-passthrough makes the
    # caller's request itself the credential for proxy paths only — the same
    # setting the live mirror recipe documents (docs/integration.md).
    monkeypatch.setenv('ROUTER_PROXY_AUTH', 'passthrough')
    server = rsrv.RouterHTTPServerAsync(("127.0.0.1", 0), app,
                                        pool_size=POOL, queue_bound=256)
    try:
        port = server.server_address[1]
        accept = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        accept.start()

        def one(i, out):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=json.dumps({'messages': [{'role': 'user', 'content': 'x'}]}).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    out[i] = (resp.status, json.loads(resp.read()))
            except Exception as exc:  # noqa: BLE001 — a transport miss is a result
                out[i] = ('err', repr(exc))

        out = {}
        clients = [threading.Thread(target=one, args=(i, out)) for i in range(REQUESTS)]
        t0 = time.monotonic()
        for c in clients:
            c.start()
        for c in clients:
            c.join(timeout=60)
        elapsed = time.monotonic() - t0
        assert not any(c.is_alive() for c in clients), "clients wedged"

        # Every request got an answer shaped by the existing contract.
        assert len(out) == REQUESTS, f"only {len(out)}/{REQUESTS} answered"
        for i, (status, body) in out.items():
            assert status == 200, (i, status, body)
            assert body.get("choices"), (i, body)

        # AC4 proper: with 40 requests in flight, upstream-blocked serving
        # threads stayed at the explicit pool bound — never one per request.
        assert gauge["entered"] == REQUESTS
        assert gauge["peak"] <= POOL, (
            f"peak upstream-blocked serving threads {gauge['peak']} > pool {POOL}")
        assert POOL < REQUESTS
        # The whole burst could not have finished faster than the pool allows
        # (a per-request-thread path would drain it in ~one hop time).
        assert elapsed >= HOP_HOLD_S * (REQUESTS // POOL) * 0.8, (
            f"burst drained in {elapsed:.2f}s — pool bound not actually applied")
        # No serving thread leaked past the bound.
        live = [t for t in threading.enumerate()
                if t.name.startswith("router-serve")]
        assert len(live) <= POOL, (
            f"{len(live)} serving threads exist for pool {POOL}")
    finally:
        server.shutdown()
        server.server_close()
        rsrv._SERVING_STATE["server"] = None


def test_queue_bound_overflow_is_closed_at_accept_not_queued_forever():
    """The enforced queue bound: beyond pool+queue slots, accept CLOSES.

    Fill every slot with wedged handlers, then fire one more connection: it
    must be closed by the server (recv -> b'' / ConnectionReset) rather than
    sitting queued forever — and the slot is released when the wedged
    handler finishes, so the bound is a real bound, not a label.
    """
    app = rsrv.RouterApplication.__new__(rsrv.RouterApplication)
    app.mode = "read-only"
    app.lifecycle = rsrv.router_drain.DrainController()
    hold = threading.Event()
    real_dispatch = rsrv.RouterApplication.dispatch

    def blocking_dispatch(self, method, path, **kw):
        if path == "/wedged":
            hold.wait(10.0)
            return 200, {"ok": True}
        return real_dispatch(self, method, path, **kw)

    server = rsrv.RouterHTTPServerAsync(("127.0.0.1", 0), app,
                                        pool_size=1, queue_bound=1)
    app.dispatch = blocking_dispatch.__get__(app, rsrv.RouterApplication)
    try:
        port = server.server_address[1]
        accept = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        accept.start()

        def _fire(path, timeout=10.0):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
                    return r.status, r.read()
            except Exception as exc:  # noqa: BLE001 — the closure IS the result
                return "closed", repr(exc)

        # Slot 1: running (wedged). Slot 2: queued.
        t_running = threading.Thread(target=_fire, args=("/wedged",))
        t_running.start()
        t_queued = threading.Thread(target=_fire, args=("/wedged",))
        t_queued.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with server._busy_lock:
                busy = server._busy
            queued = (server._pool_slots._initial_value -  # noqa: SLF001
                      server._pool_slots._value)  # noqa: SLF001
            if busy >= 1 and queued >= 2:
                break
            time.sleep(0.02)
        # Both slots consumed: one running, one queued (the in-flight HTTP
        # request itself occupies a third slot only after its handler starts;
        # the two /wedged fires are slots 1 and 2).
        with server._busy_lock:
            busy = server._busy
        queued = (server._pool_slots._initial_value -  # noqa: SLF001
                  server._pool_slots._value)  # noqa: SLF001
        assert busy >= 1 and busy + queued >= 2, (busy, queued)

        # Slot capacity (pool+queue = 2) is exhausted by the request itself
        # riding the connection. Fire past the bound: MUST be closed fast.
        t0 = time.monotonic()
        status, detail = _fire("/wedged", timeout=5.0)
        overflow_latency = time.monotonic() - t0
        assert status == "closed", (status, detail)
        assert overflow_latency < 5.0, "overflow wedged instead of failing fast"

        hold.set()
        t_running.join(timeout=15)
        t_queued.join(timeout=15)
        # Slots came back: the server still serves after the flood recedes.
        status, body = _fire("/status", timeout=10.0)
        assert status == 200
    finally:
        hold.set()
        server.shutdown()
        server.server_close()
        rsrv._SERVING_STATE["server"] = None


def test_async_server_pool_limits_default_to_admission_ceiling_plus_margin(monkeypatch):
    monkeypatch.setenv("ROUTER_PROXY_MAX_INFLIGHT", "5")
    size, qbound = rsrv._server_pool_limits("async")
    assert size == 5 + 4
    # The queue bound is a REAL bound (enforced at accept): strictly positive.
    assert qbound >= 1
    monkeypatch.setenv("ROUTER_POOL_SIZE", "11")
    monkeypatch.setenv("ROUTER_POOL_QUEUE_MAX", "13")
    assert rsrv._server_pool_limits("async") == (11, 13)
    # A non-integer env value falls back to the derived default, loudly enough.
    monkeypatch.setenv("ROUTER_POOL_QUEUE_MAX", "not-a-number")
    size2, qbound2 = rsrv._server_pool_limits("async")
    assert size2 == 11 and qbound2 == 11 + 11 // 2
