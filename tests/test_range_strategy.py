"""
Range strategy + rebalance engine tests.
"""
from decimal import Decimal

import pytest

from src.core.types import EngineConfig, PoolKey, PoolProtocol, TickRange
from src.rebalancing.rebalance_engine import RebalanceEngine
from src.strategies.range_strategy import (
    compute_new_range_on_rebalance,
    rebalance_required,
    time_in_range_estimate,
    volatility_optimal_range,
)


def test_optimal_range_centered_on_current_tick():
    tick_range = volatility_optimal_range(
        current_tick=100000, vol_1d=Decimal("0.5"), tick_spacing=60
    )
    mid = (tick_range.lower + tick_range.upper) // 2
    assert abs(mid - 100000) <= 120, "range should be roughly centered"


def test_optimal_range_snapped_to_spacing():
    for spacing in [1, 10, 60, 200]:
        r = volatility_optimal_range(0, Decimal("0.6"), tick_spacing=spacing)
        assert r.lower % spacing == 0, f"lower not snapped for spacing={spacing}"
        assert r.upper % spacing == 0, f"upper not snapped for spacing={spacing}"


def test_optimal_range_wider_at_higher_vol():
    r_low  = volatility_optimal_range(0, Decimal("0.3"), tick_spacing=60)
    r_high = volatility_optimal_range(0, Decimal("1.5"), tick_spacing=60)
    assert r_high.width > r_low.width, "higher vol should produce wider range"


def test_rebalance_required_when_out_of_range():
    config = EngineConfig(rebalance_buffer_ticks=200)
    tick_range = TickRange(1000, 2000)

    assert rebalance_required(tick_range, 800, config)   # below
    assert rebalance_required(tick_range, 2200, config)  # above
    assert not rebalance_required(tick_range, 1500, config)  # in range


def test_rebalance_required_buffer():
    config = EngineConfig(rebalance_buffer_ticks=200)
    tick_range = TickRange(1000, 2000)

    # within buffer zone — should still trigger
    assert rebalance_required(tick_range, 1050, config)
    assert rebalance_required(tick_range, 1950, config)

    # comfortably inside
    assert not rebalance_required(tick_range, 1500, config)


def test_time_in_range_estimate_in_range():
    tick_range = TickRange(-10000, 10000)
    tir = time_in_range_estimate(tick_range, 0, Decimal("0.5"), horizon_days=7)
    assert Decimal("0.3") < tir <= Decimal("1.0"), f"unexpected TIR: {tir}"


def test_time_in_range_wide_range_higher():
    narrow = TickRange(-500, 500)
    wide   = TickRange(-5000, 5000)
    vol    = Decimal("0.8")

    tir_narrow = time_in_range_estimate(narrow, 0, vol)
    tir_wide   = time_in_range_estimate(wide, 0, vol)

    assert tir_wide > tir_narrow, "wider range should have higher TIR"


def test_compute_new_range_centered():
    config = EngineConfig(default_range_width_ticks=4000)
    r = compute_new_range_on_rebalance(50000, Decimal("0.5"), config, tick_spacing=60)
    mid = (r.lower + r.upper) // 2
    assert abs(mid - 50000) <= 120
