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


def test_unparseable_degraded_wall_falls_back_to_default(rsrv, monkeypatch):
    monkeypatch.setenv("ROUTER_DEGRADED_WALL_S", "not-a-number")
    assert rsrv._proxy_degraded_wall_s() == 120.0
