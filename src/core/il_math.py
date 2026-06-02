"""
IL math. Standard CFMM stuff — don't reinvent it.

Reference: https://lambert-guillaume.medium.com/on-calculating-impermanent-loss-in-uniswap-v3-positions
"""
from __future__ import annotations

import math
from decimal import Decimal


def price_from_tick(tick: int, token0_decimals: int = 18, token1_decimals: int = 18) -> Decimal:
    raw = Decimal(str(1.0001**tick))
    return raw * Decimal(10 ** (token0_decimals - token1_decimals))


def tick_from_price(price: Decimal, token0_decimals: int = 18, token1_decimals: int = 18) -> int:
    adjusted = float(price) * 10 ** (token1_decimals - token0_decimals)
    return math.floor(math.log(adjusted) / math.log(1.0001))


def sqrt_price_from_tick(tick: int) -> int:
    """Q64.96 sqrt price. Off-chain approximation — good enough for range math."""
    sqrt_ratio = Decimal(str(1.0001 ** (tick / 2)))
    return int(sqrt_ratio * Decimal(2**96))


def liquidity_for_amounts(
    sqrt_price: int,
    sqrt_lower: int,
    sqrt_upper: int,
    amount0: Decimal,
    amount1: Decimal,
) -> int:
    """
    Compute L for a given [lower, upper] range and token amounts.
    Mirrors UniV3 LiquidityAmounts.sol — if you change this, run tests first.
    """
    sp = Decimal(sqrt_price)
    sl = Decimal(sqrt_lower)
    su = Decimal(sqrt_upper)

    if sp <= sl:
        liq = _l_for_amount0(sl, su, amount0)
    elif sp < su:
        l0 = _l_for_amount0(sp, su, amount0)
        l1 = _l_for_amount1(sl, sp, amount1)
        liq = min(l0, l1)
    else:
        liq = _l_for_amount1(sl, su, amount1)

    return int(liq)


def amounts_for_liquidity(
    sqrt_price: int,
    sqrt_lower: int,
    sqrt_upper: int,
    liquidity: int,
) -> tuple[Decimal, Decimal]:
    sp = Decimal(sqrt_price)
    sl = Decimal(sqrt_lower)
    su = Decimal(sqrt_upper)
    L = Decimal(liquidity)

    sp_clamped = max(sl, min(su, sp))

    amount0 = L * (su - sp_clamped) / (sp_clamped * su) * Decimal(2**96)
    amount1 = L * (sp_clamped - sl) / Decimal(2**96)

    return amount0, amount1


def compute_il(
    price_ratio: Decimal,   # current_price / entry_price
    sqrt_lower: int,
    sqrt_upper: int,
    sqrt_entry: int,
) -> Decimal:
    """
    IL as fraction of entry value (negative = loss).

    IL(r) = 2*sqrt(r)/(1+r) - 1 for a full-range position.
    For concentrated ranges it's worse at the edges — this accounts for that.
    """
    if price_ratio <= Decimal(0):
        return Decimal("-1")  # you got rekt

    r = price_ratio
    k = Decimal(sqrt_entry) / Decimal(sqrt_lower)
    ku = Decimal(sqrt_upper) / Decimal(sqrt_entry)

    # hodl value (normalized)
    hodl = (r + 1) / 2

    # LP value — needs to handle out-of-range cases
    sqrt_r = r.sqrt()
    if sqrt_r <= Decimal(1) / k:
        # below lower tick, all in token0
        lp_val = r * k / Decimal(sqrt_upper) * Decimal(sqrt_entry)
    elif sqrt_r >= ku:
        # above upper tick, all in token1
        lp_val = Decimal(sqrt_upper) / Decimal(sqrt_entry) * k
    else:
        lp_val = sqrt_r  # simplified mid-range

    return (lp_val - hodl) / hodl


def il_breakeven_fees(il: Decimal, time_in_range: Decimal) -> Decimal:
    """Minimum fee APR to cover IL. Rough but directionally right."""
    if time_in_range <= 0 or il >= 0:
        return Decimal(0)
    return abs(il) / time_in_range


def delta_from_il(
    price_ratio: Decimal,
    liquidity_usd: Decimal,
    entry_price: Decimal,
) -> Decimal:
    """
    Approximate hedge delta from LP IL exposure.
    For a v3 concentrated position this is path-dependent — good enough for sizing perp hedges.
    TODO: proper gamma/delta decomposition for options hedging
    """
    sqrt_r = price_ratio.sqrt()
    # d(LP_value)/d(price) approx — treating as sqrt AMM locally
    raw_delta = liquidity_usd / (2 * entry_price * sqrt_r)
    # neutral delta of a 50/50 hodl
    hodl_delta = liquidity_usd / (2 * entry_price)
    return raw_delta - hodl_delta


# ---- internal helpers ------------------------------------------------

def _l_for_amount0(sqrt_a: Decimal, sqrt_b: Decimal, amount0: Decimal) -> Decimal:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return amount0 * sqrt_a * sqrt_b / (Decimal(2**96) * (sqrt_b - sqrt_a))


def _l_for_amount1(sqrt_a: Decimal, sqrt_b: Decimal, amount1: Decimal) -> Decimal:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return amount1 * Decimal(2**96) / (sqrt_b - sqrt_a)
