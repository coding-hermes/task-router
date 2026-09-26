"""TR-070/the pricing law: a driver's $0 is not a price for a PLAN lane.

Measured before this: 95 of 110 `hermes` cost buckets in the rolling averages read exactly 0.0
(custom 131k samples, ollama-cloud 81k, opencode-go 11k, synthetic 7k) while the registry prices
those very lanes - so the cost-per-task the chain sort feeds on was zero across the fleet. The
driver reports what its own accounting knows (0.0 by config on a subscription), and pricing a lane
is the ROUTER's job.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import router_outcomes as ro  # noqa: E402


def test_a_priced_plan_lane_gets_a_plan_effective_cost_not_a_zero():
    cost, basis = ro.plan_effective_cost('ollama-cloud', 'glm-5.3-flash', 1_000_000, 0)
    assert cost is not None and cost > 0, 'a priced lane must never come back as a free zero'
    assert 'public split' in basis and 'driver reported $0' in basis


def test_the_plan_offset_comes_from_the_lanes_own_ratio():
    """The offset is baked into normalized_price, so the lane's ratio scales the token split."""
    pin, pout, norm, pub = ro._registry_prices()[('xkiro', 'openai/gpt-6-luna')]
    assert norm < pub, 'fixture assumption: xkiro is a plan carrier (normalized below list)'
    cost, basis = ro.plan_effective_cost('xkiro', 'openai/gpt-6-luna', 1_000_000, 0)
    assert cost == pytest.approx(round(pin * (norm / pub), 8), rel=1e-6)
    assert 'plan ratio' in basis
    # and the offset is real: a plan lane is far below its list sticker
    assert cost < pin * 0.5


def test_a_lane_with_no_declared_price_is_null_with_a_reason_never_a_zero():
    cost, basis = ro.plan_effective_cost('custom', 'kimi-k3-fast', 1000, 10)
    assert cost is None
    assert 'no declared price' in basis


def test_a_row_with_no_usage_gets_no_invented_cost():
    cost, basis = ro.plan_effective_cost('ollama-cloud', 'glm-5.3-flash', 0, 0)
    assert cost is None
    assert 'no usage' in basis
