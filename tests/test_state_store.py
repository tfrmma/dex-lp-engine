"""State store tests."""
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.types import (
    HedgeInstrument, HedgeState, PoolKey, PoolProtocol,
    PositionState, TickRange,
)
from src.utils.state_store import StateStore


def make_pool() -> PoolKey:
    return PoolKey("0xPOOL123", "0xTOKEN0", "0xTOKEN1", 30, PoolProtocol.UNISWAP_V3)


def make_position(pool: PoolKey) -> PositionState:
    return PositionState(
        pool=pool,
        tick_range=TickRange(-1000, 1000),
        liquidity=5_000_000,
        token0_amount=Decimal("5000"),
        token1_amount=Decimal("5000"),
        fees_earned_token0=Decimal("10"),
        fees_earned_token1=Decimal("8"),
        entry_sqrt_price=2**96,
        entry_price_usd=Decimal("2000"),
        block_entered=19_000_000,
        token_id=42,
        current_tick=0,
    )


def test_save_and_restore_position():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = StateStore(f.name)
        pool  = make_pool()
        pos   = make_position(pool)

        store.save_position(pos)
        restored = store.restore_position(pool.address)

        assert restored is not None
        assert restored.token_id == 42
        assert restored.liquidity == 5_000_000
        assert restored.token0_amount == Decimal("5000")
        assert restored.fees_earned_token0 == Decimal("10")
        assert restored.tick_range.lower == -1000
        store.close()


def test_restore_returns_none_for_missing():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = StateStore(f.name)
        result = store.restore_position("0xNOTHERE")
        assert result is None
        store.close()


def test_save_and_restore_hedge():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = StateStore(f.name)
        pool  = make_pool()
        hedge = HedgeState(
            instrument=HedgeInstrument.PERP,
            notional_usd=Decimal("5000"),
            delta=Decimal("-0.25"),
            target_delta=Decimal("-0.25"),
            entry_price=Decimal("2200"),
            pnl_usd=Decimal("50"),
            funding_accrued=Decimal("-5"),
        )
        store.save_hedge(pool.address, hedge)
        restored = store.restore_hedge(pool.address)

        assert restored is not None
        assert restored.delta == Decimal("-0.25")
        assert restored.pnl_usd == Decimal("50")
        assert restored.instrument == HedgeInstrument.PERP
        store.close()


def test_delete_position():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = StateStore(f.name)
        pool  = make_pool()
        pos   = make_position(pool)

        store.save_position(pos)
        store.delete_position(pool.address)

        assert store.restore_position(pool.address) is None
        store.close()


def test_overwrite_on_save():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store = StateStore(f.name)
        pool  = make_pool()
        pos   = make_position(pool)

        store.save_position(pos)
        pos.token_id = 99
        pos.liquidity = 9_999_999
        store.save_position(pos)

        restored = store.restore_position(pool.address)
        assert restored.token_id == 99
        assert restored.liquidity == 9_999_999
        store.close()
