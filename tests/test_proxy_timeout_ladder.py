"""TR-241: the proxy's timeout budgets must form a LADDER.

The router sits between two budgets it does not own: the caller (the
scheduler, whose per-turn tolerance defaults to 30m via
SCHEDULER_GATEWAY_RESPONSE_TIMEOUT) above it, and the gateway (which allows
3600s) below it. A middle layer must always answer BEFORE the caller gives
up: an inner wall-clock budget may never reach the caller's patience, or the
router kills work the client was still waiting for and reports a hop failure
for a turn that would have succeeded.

Before TR-241 the file computed that budget in two places with two formulas:
the /v1/responses caller used max(hop_timeout, hermes_idle) while the default
gateway caller used hop_timeout alone for buffered hops — so the same hop
shape got 300s at one site and 180s at the other. One helper
(`_hop_budget_s`) now decides for both sites: streamed hops keep the TR-138
idle budget + wall backstop unchanged; buffered hops are the configured hop
timeout clamped strictly below the caller's patience (they may rise toward
but never reach 1800s).
"""

import io
import json
import os
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import router_server as rsrv  # noqa: E402


def _json_resp(obj, ctype="application/json"):
    """A urlopen double that answers buffered JSON (and records its timeout)."""

    class _Resp(io.BytesIO):
        status = 200

        def __init__(self, body):
            super().__init__(body)
            self.headers = {"Content-Type": ctype}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(json.dumps(obj).encode())


def _sse_resp(*events):
    """A urlopen double answering a /v1/responses SSE stream to completion."""
    frames = "".join(
        "data: " + json.dumps({"type": e, "response": {"id": "r"}}) + "\n\n"
        for e in events
    )
    frames += (
        "data: "
        + json.dumps({"type": "response.completed", "response": {"id": "r"}})
        + "\n\n"
    )
    body = frames.encode()

    class _Sse(io.BytesIO):
        status = 200

        def __init__(self):
            super().__init__(body)
            self.headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Sse()


# ---------- the helper is the single source ----------


def test_one_helper_computes_the_budget_for_both_upstream_sites(monkeypatch):
    """Both upstream callers must ask the SAME function, not hand-roll numbers.

    The spy sits where the helper lives; if a site still computes its own
    budget, no call is recorded for it.
    """
    monkeypatch.delenv("ROUTER_PROXY_HOP_TIMEOUT_S", raising=False)
    monkeypatch.delenv("ROUTER_PROXY_STREAM_HOPS", raising=False)
    real = getattr(rsrv, "_hop_budget_s", None)
    assert callable(real), (
        "TR-241: _hop_budget_s(want_stream) must exist and own the budget"
    )
    seen = []

    def spy(want_stream, _real=real):
        seen.append(want_stream)
        return _real(want_stream)

    monkeypatch.setattr(rsrv, "_hop_budget_s", spy)
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _json_resp({"ok": True})
    )

    # Site A: the /v1/responses hermes caller, both shapes.
    rsrv._hermes_responses_call(
        "/v1/responses",
        {"input": "x", "stream": True},
        {},
        _opener=lambda req, timeout=None: _sse_resp("response.created"),
    )
    rsrv._hermes_responses_call(
        "/v1/responses",
        {"input": "x"},
        {},
        _opener=lambda req, timeout=None: _json_resp({}),
    )
    # Site B: the default gateway caller, both shapes.
    rsrv._proxy_upstream_default(
        "/v1/chat/completions", {"model": "m", "stream": True}, {}
    )
    rsrv._proxy_upstream_default("/v1/embeddings", {"model": "m"}, {})

    assert True in seen, "the streamed branch must budget via _hop_budget_s(True)"
    assert False in seen, "the buffered branch must budget via _hop_budget_s(False)"


def test_both_upstream_callers_agree_on_the_buffered_budget(monkeypatch):
    """The same hop shape must cost the same at both sites.

    Defaults before TR-241: site A handed the socket max(180, 300)=300s while
    site B handed it 180s. Whatever the number is, the two sites must match.
    """
    for name in (
        "ROUTER_PROXY_HOP_TIMEOUT_S",
        "ROUTER_HERMES_IDLE_TIMEOUT_S",
        "ROUTER_PROXY_IDLE_TIMEOUT_S",
        "ROUTER_PROXY_STREAM_HOPS",
    ):
        monkeypatch.delenv(name, raising=False)
    seen_a, seen_b = {}, {}

    rsrv._hermes_responses_call(
        "/v1/responses",
        {"input": "x"},
        {},
        _opener=lambda req, timeout=None: (
            seen_a.update(timeout=timeout),
            _json_resp({}),
        )[1],
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: (
            seen_b.update(timeout=timeout),
            _json_resp({"ok": True}),
        )[1],
    )
    rsrv._proxy_upstream_default("/v1/embeddings", {"model": "m"}, {})

    assert seen_a["timeout"] == seen_b["timeout"], (
        "TR-241: one hop shape, one budget — the sites disagree"
    )


# ---------- the ladder invariant ----------


def test_the_buffered_budget_is_strictly_below_the_caller_patience(monkeypatch):
    """A wall-clock bounded hop must ALWAYS die before the client does.

    The scheduler waits 1800s; any buffered budget at or above that means the
    router kills work the caller is still patiently waiting for. The clamp
    must hold for the pathological default AND for an operator who configured
    a hop timeout above the ceiling.
    """
    monkeypatch.delenv("ROUTER_PROXY_STREAM_HOPS", raising=False)
    caller_patience = 1800.0

    monkeypatch.setenv("ROUTER_PROXY_HOP_TIMEOUT_S", "5000")
    assert rsrv._hop_budget_s(False) < caller_patience, (
        "a configured hop timeout above the caller patience must be clamped"
    )

    monkeypatch.setenv("ROUTER_PROXY_HOP_TIMEOUT_S", "1800")
    assert rsrv._hop_budget_s(False) < caller_patience, (
        "exactly-at-the-ceiling is not strictly below it"
    )

    monkeypatch.setenv("ROUTER_HERMES_IDLE_TIMEOUT_S", "7200")
    assert rsrv._hop_budget_s(False) < caller_patience, (
        "a huge hermes idle knob must not push the buffered budget past the caller"
    )


def test_a_sane_large_hop_config_is_honored_up_to_the_margin(monkeypatch):
    """The clamp fixes pathological values, not sane ones: a hop timeout that
    already sits strictly below the caller patience is used as configured."""
    monkeypatch.setenv("ROUTER_PROXY_HOP_TIMEOUT_S", "1200")
    assert rsrv._hop_budget_s(False) == 1200.0
    monkeypatch.setenv("ROUTER_PROXY_HOP_TIMEOUT_S", "1740")
    assert rsrv._hop_budget_s(False) == 1740.0, (
        "up to the ladder margin the operator value is kept verbatim"
    )


def test_the_default_buffered_budget_is_unchanged(monkeypatch):
    """The default hop budget stays 180s — only the pathological ceiling is new."""
    monkeypatch.delenv("ROUTER_PROXY_HOP_TIMEOUT_S", raising=False)
    assert rsrv._hop_budget_s(False) == 180.0


def test_streamed_hops_keep_the_tr_138_budget_semantics(monkeypatch):
    """The streamed branch is idle-shaped and keeps its wall backstop: the
    idle budget may run as long as events keep arriving; the wall bounds a
    pathological stream that never stops talking."""
    monkeypatch.delenv("ROUTER_PROXY_STREAM_HOPS", raising=False)
    monkeypatch.setenv("ROUTER_PROXY_IDLE_TIMEOUT_S", "900")
    monkeypatch.setenv("ROUTER_PROXY_HOP_WALL_S", "3600")
    assert rsrv._hop_budget_s(True) == 3600.0

    monkeypatch.setenv("ROUTER_PROXY_HOP_WALL_S", "120")
    assert rsrv._hop_budget_s(True) == 900.0, (
        "the wall is a backstop, the idle budget is the primary"
    )

    monkeypatch.delenv("ROUTER_PROXY_IDLE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("ROUTER_PROXY_HOP_WALL_S", raising=False)
    assert rsrv._hop_budget_s(True) == max(
        rsrv._proxy_idle_budget_s(), rsrv._proxy_wall_ceiling_s()
    )


# ---------- end to end through the real sites ----------


def test_the_socket_sees_the_ladder_budget_at_both_sites(monkeypatch):
    """Whatever the helper says is what reaches urlopen — both callers included."""
    monkeypatch.delenv("ROUTER_PROXY_STREAM_HOPS", raising=False)
    monkeypatch.setenv("ROUTER_PROXY_HOP_TIMEOUT_S", "5000")
    budget = rsrv._hop_budget_s(False)
    assert budget < 1800.0

    seen_a, seen_b = {}, {}
    rsrv._hermes_responses_call(
        "/v1/responses",
        {"input": "x"},
        {},
        _opener=lambda req, timeout=None: (
            seen_a.update(timeout=timeout),
            _json_resp({}),
        )[1],
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: (
            seen_b.update(timeout=timeout),
            _json_resp({"ok": True}),
        )[1],
    )
    rsrv._proxy_upstream_default("/v1/embeddings", {"model": "m"}, {})

    assert seen_a["timeout"] == budget, (
        "site A must hand the ladder budget to the socket"
    )
    assert seen_b["timeout"] == budget, (
        "site B must hand the ladder budget to the socket"
    )

    # And a streamed wish at site A still rides the idle/wall budget.
    monkeypatch.setenv("ROUTER_PROXY_IDLE_TIMEOUT_S", "30")
    monkeypatch.setenv("ROUTER_PROXY_HOP_WALL_S", "3600")
    seen = {}
    rsrv._hermes_responses_call(
        "/v1/responses",
        {"input": "x", "stream": True},
        {},
        _opener=lambda req, timeout=None: (
            seen.update(timeout=timeout),
            _sse_resp("response.created"),
        )[1],
    )
    assert seen["timeout"] == 3600.0, (
        "the streamed branch keeps the TR-138 idle+wall budget"
    )
