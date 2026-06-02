"""
IL hedge manager. Keeps a running perp (or spot) position to offset LP delta exposure.

Key insight: a v3 LP position has a gamma-like payoff — convex losses as price moves away.
We hedge the linear (delta) component here; gamma is too expensive to hedge continuously
and the fee income is supposed to compensate. If it doesn't, you should widen your range.

TODO: implement options-based hedging for asymmetric IL exposure (token0 vs token1)
"""
from __future__ import annotations

import logging
from decimal import Decimal

from src.core.il_math import compute_il, delta_from_il, price_from_tick
from src.core.types import EngineConfig, HedgeInstrument, HedgeState, PositionState

log = logging.getLogger(__name__)


class ILHedgeManager:
    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self._hedge: HedgeState | None = None

    # ---- public API --------------------------------------------------

    @property
    def has_hedge(self) -> bool:
        return self._hedge is not None

    @property
    def current_hedge(self) -> HedgeState | None:
        return self._hedge

    def evaluate(
        self,
        position: PositionState,
        current_price_usd: Decimal,
    ) -> HedgeAction | None:
        """
        Returns a hedge action if adjustment is needed, None otherwise.
        Called on every price tick — keep it cheap.
        """
        il = self._compute_il(position, current_price_usd)
        il_bps = int(abs(il) * 10_000)

        if il_bps < self.config.il_hedge_threshold_bps:
            return None  # not worth it yet

        target_delta = self._target_hedge_delta(position, current_price_usd, il)

        if self._hedge is None:
            return HedgeAction(
                action_type="open",
                instrument=self.config.hedge_instrument,
                delta=target_delta,
                reason=f"IL={il_bps}bps > threshold={self.config.il_hedge_threshold_bps}bps",
            )

        current_delta = self._hedge.delta
        delta_error = abs(target_delta - current_delta)

        # only rehedge if error is material — every tx costs gas
        if delta_error / max(abs(target_delta), Decimal("0.001")) > Decimal("0.10"):
            return HedgeAction(
                action_type="adjust",
                instrument=self.config.hedge_instrument,
                delta=target_delta,
                reason=f"delta drift {delta_error:.4f}",
            )

        return None

    def should_remove_hedge(self, position: PositionState, current_price_usd: Decimal) -> bool:
        if self._hedge is None:
            return False
        il = self._compute_il(position, current_price_usd)
        # unwind if IL reversed and we're close to neutral
        return il > Decimal("-0.002") and abs(self._hedge.delta) < Decimal("0.05")

    def on_hedge_opened(self, state: HedgeState) -> None:
        self._hedge = state
        log.info("hedge opened delta=%.4f notional=%.0f", state.delta, state.notional_usd)

    def on_hedge_adjusted(self, new_delta: Decimal, pnl_delta: Decimal) -> None:
        if self._hedge is None:
            return
        self._hedge.delta = new_delta
        self._hedge.pnl_usd += pnl_delta

    def on_hedge_closed(self, final_pnl: Decimal) -> None:
        if self._hedge:
            log.info("hedge closed pnl=%.2f", final_pnl)
        self._hedge = None

    def update_funding(self, funding: Decimal) -> None:
        if self._hedge:
            self._hedge.funding_accrued += funding

    # ---- internal ----------------------------------------------------

    def _compute_il(self, position: PositionState, current_price_usd: Decimal) -> Decimal:
        price_ratio = current_price_usd / position.entry_price_usd
        return compute_il(
            price_ratio,
            sqrt_lower=_sqrt_from_tick(position.tick_range.lower),
            sqrt_upper=_sqrt_from_tick(position.tick_range.upper),
            sqrt_entry=position.entry_sqrt_price,
        )

    def _target_hedge_delta(
        self,
        position: PositionState,
        current_price_usd: Decimal,
        il: Decimal,
    ) -> Decimal:
        position_usd = (position.token0_amount + position.token1_amount) * current_price_usd
        raw_delta = delta_from_il(
            price_ratio=current_price_usd / position.entry_price_usd,
            liquidity_usd=position_usd,
            entry_price=position.entry_price_usd,
        )
        # cap hedge ratio — hedging too much just introduces more risk
        max_delta = position_usd * self.config.max_hedge_ratio / current_price_usd
        return max(min(raw_delta, max_delta), -max_delta)


def _sqrt_from_tick(tick: int) -> int:
    from src.core.il_math import sqrt_price_from_tick
    return sqrt_price_from_tick(tick)


class HedgeAction:
    __slots__ = ("action_type", "instrument", "delta", "reason")

    def __init__(
        self,
        action_type: str,
        instrument: HedgeInstrument,
        delta: Decimal,
        reason: str,
    ) -> None:
        self.action_type = action_type
        self.instrument  = instrument
        self.delta       = delta
        self.reason      = reason

    def __repr__(self) -> str:
        return f"HedgeAction({self.action_type} δ={self.delta:.4f} [{self.reason}])"
