"""TR-355: a DEGRADED fallback hop must not ride the full hop wall.

Measured 2026-10-29: with every eligible hop gated, the exhausted-chain fallback
served a lane whose ladder row carried `requirements_unmet` and let it run the
full 600s hop wall (wall_time_s 600.38) before answering 502 — 600 seconds of a
30-minute tick spent on a lane that could never satisfy the task.

The clamp only ever SHORTENS: it is a min() against what the ladder already
allowed, keyed on the hop's own requirements_unmet, so healthy hops are
untouched and a misconfigured degraded wall cannot extend a hop.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


@pytest.fixture()
def rsrv(monkeypatch):
    monkeypatch.setenv("ROUTER_PROXY_HOP_TIMEOUT_S", "600")
    monkeypatch.setenv("ROUTER_PROXY_IDLE_TIMEOUT_S", "1800")
    monkeypatch.setenv("ROUTER_PROXY_HOP_WALL_S", "3600")
    monkeypatch.delenv("ROUTER_DEGRADED_WALL_S", raising=False)
    import router_server  # noqa: F401  (path is seeded by tests/conftest.py)

    return importlib.reload(router_server)


def test_healthy_buffered_hop_keeps_the_full_wall(rsrv):
    """The leash must not shorten a hop that meets the requirements."""
    assert rsrv._hop_budget_s(False) == 600.0


def test_degraded_buffered_hop_is_leashed(rsrv):
    assert rsrv._hop_budget_s(False, degraded=True) == 120.0


def test_degraded_streamed_hop_is_leashed_too(rsrv):
    """A streamed hop's budget is max(idle, wall) — 3600s — so it needs the leash."""
    assert rsrv._hop_budget_s(True) == 3600.0
    assert rsrv._hop_budget_s(True, degraded=True) == 120.0


def test_degraded_wall_never_extends_a_hop(rsrv, monkeypatch):
    """A degraded wall ABOVE the ladder cannot make a hop longer."""
    monkeypatch.setenv("ROUTER_DEGRADED_WALL_S", "9999")
    assert rsrv._hop_budget_s(False, degraded=True) == 600.0


def test_degraded_wall_is_configurable(rsrv, monkeypatch):
    monkeypatch.setenv("ROUTER_DEGRADED_WALL_S", "45")
    assert rsrv._hop_budget_s(False, degraded=True) == 45.0


def test_failure_envelope_names_degraded_wall_policy(rsrv):
    envelope = rsrv._failure_envelope(
        {"ladder": [{"provider": "deepseek-foreman", "model": "deepseek-flash",
                     "requirements_unmet": ["tool_use"], "fallback": True,
                     "reason": "hop-wall-timeout", "timeout_kind": "hop-wall-timeout"}]},
        "s", "router-proxy", None, 0.0, "hop-wall-timeout",
        [{"provider": "deepseek-foreman", "model": "deepseek-flash",
          "requirements_unmet": ["tool_use"], "fallback": True,
          "reason": "hop-wall-timeout", "timeout_kind": "hop-wall-timeout"}],
        "hop timed out")
    assert envelope["terminal_reason"] == "hop-wall-timeout"
    assert envelope["fallback_policy"] == "degraded-wall-cap"
    assert envelope["degraded_wall_s"] == 120.0


def test_unparseable_degraded_wall_falls_back_to_default(rsrv, monkeypatch):
    monkeypatch.setenv("ROUTER_DEGRADED_WALL_S", "not-a-number")
    assert rsrv._proxy_degraded_wall_s() == 120.0


def test_provider_last_mile_hop_honours_the_degraded_leash(rsrv, monkeypatch):
    """TR-355 AC2, second half: the provider last-mile hop must leash too.

    Measured live 2026-11-05 00:24:07 (scheduler.log.1:309463): a DEGRADED
    last-mile hop deepseek-foreman/deepseek-flash ran latency_s=600.017 /
    wall_time_s=611.306 while its own failure envelope advertised
    degraded_wall_s=120.0 — _provider_upstream_factory never read the
    X-Router-Hop-Degraded-Fallback stamp the hop loop set on it.
    """
    recorded = {}

    class _FakeResp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        recorded['timeout'] = timeout
        return _FakeResp()

    monkeypatch.setattr(rsrv.urllib.request, 'urlopen', _fake_urlopen)
    call = rsrv._provider_upstream_factory('deepseek-foreman', {
        'deepseek-foreman': {
            'api_base_url': 'https://example.invalid/v1',
            'api_key_env': 'X',
        }})

    # Degraded stamp present -> the 120s cap, not the 600s ladder hop wall.
    status, payload = call('/v1/chat/completions', {'model': 'deepseek-flash'},
                           {'X-Router-Hop-Degraded-Fallback': '1'})
    assert status == 200 and payload == {'ok': True}
    assert recorded['timeout'] == 120.0, (
        'degraded last-mile hop rode the full hop wall '
        f'({recorded["timeout"]}s) instead of the 120s cap')

    # The cap is configurable and only ever shortens.
    monkeypatch.setenv("ROUTER_DEGRADED_WALL_S", "45")
    call('/v1/chat/completions', {'model': 'deepseek-flash'},
         {'X-Router-Hop-Degraded-Fallback': '1'})
    assert recorded['timeout'] == 45.0
