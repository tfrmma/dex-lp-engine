"""
Hedge manager tests. Focus on the decision logic, not execution.
"""
from decimal import Decimal

import pytest

from src.core.il_math import sqrt_price_from_tick
from src.core.types import EngineConfig, PoolKey, PoolProtocol, PositionState, TickRange
from src.hedging.il_hedge import ILHedgeManager


def _make_position(entry_price: Decimal, tick_lower: int = -2000, tick_upper: int = 2000) -> PositionState:
    tick_range = TickRange(tick_lower, tick_upper)
    return PositionState(
        pool=PoolKey("0xPOOL", "0xTOKEN0", "0xTOKEN1", 30, PoolProtocol.UNISWAP_V3),
        tick_range=tick_range,
        liquidity=1_000_000,
        token0_amount=Decimal("5"),
        token1_amount=Decimal("10000"),
        fees_earned_token0=Decimal(0),
        fees_earned_token1=Decimal(0),
        entry_sqrt_price=sqrt_price_from_tick(0),
        entry_price_usd=entry_price,
        block_entered=1_000_000,
        current_tick=0,
    )


def test_no_hedge_below_threshold():
    config = EngineConfig(il_hedge_threshold_bps=50)
    mgr = ILHedgeManager(config)
    pos = _make_position(Decimal("2000"))

    # tiny price move — well below threshold
    action = mgr.evaluate(pos, Decimal("2002"))
    assert action is None


def test_hedge_opens_above_threshold():
    config = EngineConfig(il_hedge_threshold_bps=10)  # very low threshold
    mgr = ILHedgeManager(config)
    pos = _make_position(Decimal("2000"))

    # 10% price move
    action = mgr.evaluate(pos, Decimal("2200"))
    assert action is not None
    assert action.action_type == "open"


def test_hedge_direction():
    config = EngineConfig(il_hedge_threshold_bps=10)
    mgr = ILHedgeManager(config)
    pos = _make_position(Decimal("2000"))

    action = mgr.evaluate(pos, Decimal("2500"))
    if action:
        # price went up → LP is short delta → hedge should also be short (negative delta)
        assert action.delta < 0


def test_no_duplicate_open():
    config = EngineConfig(il_hedge_threshold_bps=10)
    mgr = ILHedgeManager(config)
    pos = _make_position(Decimal("2000"))

    action1 = mgr.evaluate(pos, Decimal("2400"))
    assert action1 is not None
    assert action1.action_type == "open"

    from src.core.types import HedgeInstrument, HedgeState
    mgr.on_hedge_opened(HedgeState(
        instrument=HedgeInstrument.PERP,
        notional_usd=Decimal("5000"),
        delta=action1.delta,
        target_delta=action1.delta,
        entry_price=Decimal("2400"),
    ))

    # same price — delta hasn't drifted, should not trigger adjust
    action2 = mgr.evaluate(pos, Decimal("2400"))
    assert action2 is None or action2.action_type != "open"


def test_should_remove_hedge_when_price_returns():
    config = EngineConfig(il_hedge_threshold_bps=10)
    mgr = ILHedgeManager(config)
    pos = _make_position(Decimal("2000"))

    from src.core.types import HedgeInstrument, HedgeState
    mgr.on_hedge_opened(HedgeState(
        instrument=HedgeInstrument.PERP,
        notional_usd=Decimal("1000"),
        delta=Decimal("-0.02"),
        target_delta=Decimal("-0.02"),
        entry_price=Decimal("2200"),
    ))

    # price came back to near entry — IL near zero
    assert mgr.should_remove_hedge(pos, Decimal("2001"))
