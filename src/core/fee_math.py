"""
Fee capture math. Mostly growth accumulators off UniV3 slot0 / position data.
"""
from __future__ import annotations

from decimal import Decimal

from src.core.types import MarketSnapshot, PositionState

_Q128 = 2**128


def fees_owed(
    position: PositionState,
    snapshot: MarketSnapshot,
    fee_growth_inside0_last: int,
    fee_growth_inside1_last: int,
    fee_growth_inside0_now: int,
    fee_growth_inside1_now: int,
) -> tuple[Decimal, Decimal]:
    """
    Uncollected fees. Mirrors the on-chain accumulator math.
    feeGrowthInside is in Q128 per unit of liquidity.
    """
    L = position.liquidity
    delta0 = (fee_growth_inside0_now - fee_growth_inside0_last) % _Q128
    delta1 = (fee_growth_inside1_now - fee_growth_inside1_last) % _Q128

    token0_fees = Decimal(L * delta0) / Decimal(_Q128)
    token1_fees = Decimal(L * delta1) / Decimal(_Q128)

    return token0_fees, token1_fees


def fee_growth_inside(
    tick_lower: int,
    tick_upper: int,
    current_tick: int,
    fee_growth_global: int,
    fee_growth_below_lower: int,
    fee_growth_above_upper: int,
) -> int:
    """
    Reconstruct feeGrowthInsideX128 from pool ticks.
    Pain in the ass but you have to do it if you're polling off-chain.
    """
    if current_tick >= tick_lower:
        fg_below = fee_growth_below_lower
    else:
        fg_below = (fee_growth_global - fee_growth_below_lower) % _Q128

    if current_tick < tick_upper:
        fg_above = fee_growth_above_upper
    else:
        fg_above = (fee_growth_global - fee_growth_above_upper) % _Q128

    return (fee_growth_global - fg_below - fg_above) % _Q128


def estimate_fee_apr(
    fees_24h_usd: Decimal,
    tvl_usd: Decimal,
) -> Decimal:
    if tvl_usd <= 0:
        return Decimal(0)
    return fees_24h_usd / tvl_usd * 365


def fee_tier_to_bps(fee_tier: int) -> int:
    """UniV3 fee tiers: 100, 500, 3000, 10000 → bps."""
    return fee_tier // 100


def expected_fees_for_range(
    pool_tvl_usd: Decimal,
    pool_fee_apr: Decimal,
    position_liquidity_fraction: Decimal,  # our L / total L
    time_in_range_fraction: Decimal,        # expected time in range [0,1]
) -> Decimal:
    """
    Expected fee income. Optimistic — assumes steady volume.
    In practice volume clusters when price is moving, which is also when you're near edges.
    Life's unfair like that.
    """
    annual_fees = pool_tvl_usd * pool_fee_apr * position_liquidity_fraction
    return annual_fees * time_in_range_fraction


def gas_adjusted_fee_apr(
    fee_apr: Decimal,
    gas_cost_usd: Decimal,
    position_usd: Decimal,
    rebalance_frequency_per_year: Decimal,
) -> Decimal:
    # TODO: model gas costs dynamically from the gas oracle
    annual_gas = gas_cost_usd * rebalance_frequency_per_year
    net_fees = fee_apr * position_usd - annual_gas
    return net_fees / position_usd if position_usd > 0 else Decimal(0)
