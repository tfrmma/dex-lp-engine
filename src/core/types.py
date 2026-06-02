"""
Core types. Don't add more dataclasses here without a good reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import TypeAlias

Wei: TypeAlias = int
BPS: TypeAlias = int  # basis points, always


class PoolProtocol(Enum):
    UNISWAP_V3 = "univ3"
    UNISWAP_V4 = "univ4"
    AERODROME = "aero"


class HedgeInstrument(Enum):
    PERP = auto()
    OPTIONS = auto()
    SPOT = auto()


class RebalanceReason(Enum):
    OUT_OF_RANGE = auto()
    IL_THRESHOLD = auto()
    FEE_DRAG = auto()
    MANUAL = auto()


@dataclass(frozen=True, slots=True)
class PoolKey:
    address: str
    token0: str
    token1: str
    fee_bps: BPS
    protocol: PoolProtocol

    def __str__(self) -> str:
        return f"{self.token0[:6]}/{self.token1[:6]}@{self.fee_bps}bps"


@dataclass(slots=True)
class TickRange:
    lower: int
    upper: int

    @property
    def width(self) -> int:
        return self.upper - self.lower

    def contains(self, tick: int) -> bool:
        return self.lower <= tick <= self.upper

    def __repr__(self) -> str:
        return f"TickRange({self.lower}, {self.upper})"


@dataclass(slots=True)
class PositionState:
    pool: PoolKey
    tick_range: TickRange
    liquidity: int
    token0_amount: Decimal
    token1_amount: Decimal
    fees_earned_token0: Decimal
    fees_earned_token1: Decimal
    entry_sqrt_price: int
    entry_price_usd: Decimal
    block_entered: int
    token_id: int | None = None  # NFT id, None if not minted yet

    @property
    def in_range(self) -> bool:
        return self.tick_range.lower < self.current_tick < self.tick_range.upper

    # set externally after each price update — bit ugly but avoids circular deps
    current_tick: int = field(default=0, compare=False)


@dataclass(slots=True)
class MarketSnapshot:
    pool: PoolKey
    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee_growth_global0: int
    fee_growth_global1: int
    block: int
    timestamp: int

    @property
    def price(self) -> Decimal:
        # P = (sqrtP / 2^96)^2, adjusted for token decimals elsewhere
        ratio = Decimal(self.sqrt_price_x96) / Decimal(2**96)
        return ratio * ratio


@dataclass(slots=True)
class HedgeState:
    instrument: HedgeInstrument
    notional_usd: Decimal
    delta: Decimal           # current hedge delta
    target_delta: Decimal    # where we want to be
    entry_price: Decimal
    pnl_usd: Decimal = Decimal(0)
    funding_accrued: Decimal = Decimal(0)


@dataclass
class EngineConfig:
    # position sizing
    max_position_usd: Decimal = Decimal("100_000")
    min_position_usd: Decimal = Decimal("1_000")

    # IL hedging
    il_hedge_threshold_bps: BPS = 50        # start hedging at 0.5% IL
    max_hedge_ratio: Decimal = Decimal("0.9")
    hedge_instrument: HedgeInstrument = HedgeInstrument.PERP

    # range management
    default_range_width_ticks: int = 4000   # ~±20% on most pools
    rebalance_buffer_ticks: int = 200       # don't rebalance right at edge
    fee_drag_threshold_bps: BPS = 15        # gas-adjusted

    # execution
    slippage_tolerance_bps: BPS = 30
    gas_price_gwei_cap: int = 80
    max_retries: int = 3

    # risk
    max_il_pct: Decimal = Decimal("0.05")   # hard stop at 5% IL
    min_fee_apr: Decimal = Decimal("0.20")  # 20% APR minimum to be in range
