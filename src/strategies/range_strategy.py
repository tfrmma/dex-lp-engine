"""
Range selection strategies.

Philosophy: wider = less rebalancing, lower fees per $ capital.
Narrower = more fee capture when in range, more gas, more IL risk at boundaries.
There's no free lunch; pick your poison.
"""
from __future__ import annotations

import math
from decimal import Decimal

import numpy as np

from src.core.il_math import price_from_tick, tick_from_price
from src.core.types import EngineConfig, MarketSnapshot, TickRange


def volatility_optimal_range(
    current_tick: int,
    vol_1d: Decimal,          # realized 1-day vol, annualized
    target_time_in_range: Decimal = Decimal("0.85"),
    tick_spacing: int = 60,
) -> TickRange:
    """
    Size the range so that price stays inside ~85% of the time under a log-normal assumption.
    Works reasonably well for majors; don't trust it for low-cap tokens.
    """
    # z-score for target in-range probability (two-tailed)
    z = float(_normal_quantile((1 + float(target_time_in_range)) / 2))
    daily_vol = float(vol_1d) / math.sqrt(365)
    log_half_width = z * daily_vol

    lower_price_ratio = math.exp(-log_half_width)
    upper_price_ratio = math.exp(log_half_width)

    current_price = price_from_tick(current_tick)
    lower_tick = tick_from_price(current_price * Decimal(str(lower_price_ratio)))
    upper_tick = tick_from_price(current_price * Decimal(str(upper_price_ratio)))

    return _snap_to_spacing(lower_tick, upper_tick, tick_spacing)


def fee_optimized_range(
    snapshot: MarketSnapshot,
    config: EngineConfig,
    vol_1d: Decimal,
    tick_spacing: int = 60,
) -> TickRange:
    """
    Narrower range → more fee density → higher fee APR per $ of capital.
    But narrower also means more rebalancing cost. This tries to find the knee.

    Rough heuristic: target a range width such that fee capture > gas + IL drag.
    Not academically rigorous but works in production.
    """
    base_range = volatility_optimal_range(
        snapshot.tick, vol_1d, tick_spacing=tick_spacing
    )

    # squeeze the range to improve fee concentration — by up to 40%
    width = base_range.width
    squeeze_factor = _compute_squeeze(vol_1d, config.fee_drag_threshold_bps)
    new_half = int(width * squeeze_factor / 2)

    lower = snapshot.tick - new_half
    upper = snapshot.tick + new_half

    return _snap_to_spacing(lower, upper, tick_spacing)


def rebalance_required(
    position_tick_range: TickRange,
    current_tick: int,
    config: EngineConfig,
) -> bool:
    """
    Returns True if we should rebalance.
    Buffer avoids flapping near the edges — learned this the hard way.
    """
    buffered_lower = position_tick_range.lower + config.rebalance_buffer_ticks
    buffered_upper = position_tick_range.upper - config.rebalance_buffer_ticks

    if buffered_lower >= buffered_upper:
        # range is too narrow for a buffer — just check raw bounds
        return not position_tick_range.contains(current_tick)

    return not (buffered_lower <= current_tick <= buffered_upper)


def compute_new_range_on_rebalance(
    current_tick: int,
    vol_1d: Decimal,
    config: EngineConfig,
    tick_spacing: int = 60,
) -> TickRange:
    half = config.default_range_width_ticks // 2
    raw_lower = current_tick - half
    raw_upper = current_tick + half
    return _snap_to_spacing(raw_lower, raw_upper, tick_spacing)


def time_in_range_estimate(
    tick_range: TickRange,
    current_tick: int,
    vol_1d: Decimal,
    horizon_days: int = 7,
) -> Decimal:
    """
    Simulate probability that price stays in range over horizon.
    Monte Carlo — crude but fast enough for sizing decisions.
    """
    n_paths = 5_000
    daily_vol = float(vol_1d) / math.sqrt(365)
    dt = 1.0 / 24  # hourly steps

    steps = horizon_days * 24
    shocks = np.random.normal(0, daily_vol * math.sqrt(dt), (n_paths, steps))
    log_moves = np.cumsum(shocks, axis=1)

    tick_moves = (log_moves / math.log(1.0001)).astype(int)
    final_ticks = current_tick + tick_moves

    in_range = (
        (final_ticks >= tick_range.lower) & (final_ticks <= tick_range.upper)
    ).all(axis=1)

    return Decimal(str(in_range.mean()))


# ---- internal --------------------------------------------------------

def _snap_to_spacing(lower: int, upper: int, spacing: int) -> TickRange:
    snapped_lower = (lower // spacing) * spacing
    snapped_upper = math.ceil(upper / spacing) * spacing
    return TickRange(snapped_lower, snapped_upper)


def _compute_squeeze(vol_1d: Decimal, fee_drag_bps: int) -> Decimal:
    """Higher vol → less squeeze (need more buffer). Clamp to [0.6, 1.0]."""
    raw = Decimal("1.0") - vol_1d * Decimal(str(fee_drag_bps / 100))
    return max(Decimal("0.6"), min(Decimal("1.0"), raw))


def _normal_quantile(p: float) -> float:
    """Abramowitz & Stegun approximation. Good enough."""
    from scipy.stats import norm  # lazy import — scipy is heavy
    return norm.ppf(p)
