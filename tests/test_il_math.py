"""
IL math tests. Basic sanity checks — not exhaustive.
"""
import math
from decimal import Decimal

import pytest

from src.core.il_math import (
    compute_il,
    delta_from_il,
    liquidity_for_amounts,
    price_from_tick,
    sqrt_price_from_tick,
    tick_from_price,
)


def test_price_tick_roundtrip():
    for tick in [-100000, -50000, 0, 10000, 50000, 100000]:
        price = price_from_tick(tick)
        recovered = tick_from_price(price)
        # tick math is floored — allow ±1
        assert abs(recovered - tick) <= 1, f"roundtrip failed at tick={tick}"


def test_il_zero_at_entry():
    sqrt_entry = sqrt_price_from_tick(0)
    sqrt_lower = sqrt_price_from_tick(-1000)
    sqrt_upper = sqrt_price_from_tick(1000)
    il = compute_il(Decimal("1.0"), sqrt_lower, sqrt_upper, sqrt_entry)
    assert abs(il) < Decimal("0.001"), f"IL at entry should be ~0, got {il}"


def test_il_increases_with_price_move():
    sqrt_entry = sqrt_price_from_tick(0)
    sqrt_lower = sqrt_price_from_tick(-2000)
    sqrt_upper = sqrt_price_from_tick(2000)

    il_small = compute_il(Decimal("1.10"), sqrt_lower, sqrt_upper, sqrt_entry)
    il_large = compute_il(Decimal("1.50"), sqrt_lower, sqrt_upper, sqrt_entry)

    assert il_large < il_small, "larger price move should mean more IL"


def test_il_symmetric_approximately():
    """IL should be roughly symmetric for up/down moves of same magnitude."""
    sqrt_entry = sqrt_price_from_tick(0)
    sqrt_lower = sqrt_price_from_tick(-3000)
    sqrt_upper = sqrt_price_from_tick(3000)

    il_up   = compute_il(Decimal("1.20"), sqrt_lower, sqrt_upper, sqrt_entry)
    il_down = compute_il(Decimal("1") / Decimal("1.20"), sqrt_lower, sqrt_upper, sqrt_entry)

    # not perfectly symmetric due to log-linear ticks, but should be close
    assert abs(abs(il_up) - abs(il_down)) < Decimal("0.02")


def test_delta_sign():
    """Hedge delta should be negative (short) when price goes up from entry."""
    delta = delta_from_il(
        price_ratio=Decimal("1.2"),
        liquidity_usd=Decimal("10000"),
        entry_price=Decimal("2000"),
    )
    assert delta < 0, f"expected negative delta (hedge short), got {delta}"


def test_liquidity_for_amounts_in_range():
    entry_tick = 0
    lower_tick = -1000
    upper_tick  = 1000

    sqrt_p = sqrt_price_from_tick(entry_tick)
    sqrt_l = sqrt_price_from_tick(lower_tick)
    sqrt_u = sqrt_price_from_tick(upper_tick)

    liq = liquidity_for_amounts(sqrt_p, sqrt_l, sqrt_u, Decimal("1e18"), Decimal("2000e18"))
    assert liq > 0, "should have positive liquidity"
