"""
Rebalancing logic. Flow: detect → estimate cost → approve/skip → execute.

Cost model is conservative on purpose — late rebalances beat paying 3x gas
for a micro-adjustment every time.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from src.core.fee_math import gas_adjusted_fee_apr
from src.core.il_math import price_from_tick
from src.core.types import (
    EngineConfig,
    MarketSnapshot,
    PositionState,
    RebalanceReason,
    TickRange,
)
from src.strategies.range_strategy import (
    compute_new_range_on_rebalance,
    rebalance_required,
    time_in_range_estimate,
)

log = logging.getLogger(__name__)

_TIR_CACHE_TTL_S = 60  # Monte Carlo is ~20ms; don't run it every block


@dataclass
class RebalanceProposal:
    reason: RebalanceReason
    current_range: TickRange
    proposed_range: TickRange
    estimated_gas_usd: Decimal
    expected_gain_usd: Decimal
    approved: bool

    @property
    def net_gain(self) -> Decimal:
        return self.expected_gain_usd - self.estimated_gas_usd

    def __repr__(self) -> str:
        verdict = "APPROVE" if self.approved else "SKIP"
        return (
            f"[{verdict}] rebalance {self.reason.name} "
            f"range={self.proposed_range} "
            f"gain={self.expected_gain_usd:.2f} gas={self.estimated_gas_usd:.2f}"
        )


class RebalanceEngine:
    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        # TIR cache: (tick_range, current_tick, vol) → (tir, timestamp)
        self._tir_cache: dict[tuple, tuple[Decimal, float]] = {}

    def evaluate(
        self,
        position: PositionState,
        snapshot: MarketSnapshot,
        vol_1d: Decimal,
        pool_fee_apr: Decimal,
        gas_price_gwei: int,
        position_usd: Decimal,
        eth_price_usd: Decimal,     # from oracle, not hardcoded
    ) -> RebalanceProposal | None:
        reason = self._detect_reason(position, snapshot, pool_fee_apr, position_usd)
        if reason is None:
            return None

        new_range = compute_new_range_on_rebalance(
            snapshot.tick, vol_1d, self.config, _tick_spacing_for_fee(snapshot.pool.fee_bps)
        )
        gas_cost     = _estimate_gas_cost(gas_price_gwei, eth_price_usd)
        expected_gain = self._estimate_gain(
            position, new_range, snapshot, vol_1d, pool_fee_apr, position_usd
        )

        approved = (expected_gain - gas_cost) > Decimal("10")
        proposal = RebalanceProposal(
            reason=reason,
            current_range=position.tick_range,
            proposed_range=new_range,
            estimated_gas_usd=gas_cost,
            expected_gain_usd=expected_gain,
            approved=approved,
        )
        if not approved:
            log.debug("rebalance skipped: %s", proposal)
        return proposal

    # ---- detection ---------------------------------------------------

    def _detect_reason(
        self,
        position: PositionState,
        snapshot: MarketSnapshot,
        pool_fee_apr: Decimal,
        position_usd: Decimal,
    ) -> RebalanceReason | None:
        if rebalance_required(position.tick_range, snapshot.tick, self.config):
            return RebalanceReason.OUT_OF_RANGE
        if self._il_triggered(position, snapshot):
            return RebalanceReason.IL_THRESHOLD
        if self._fee_drag_triggered(position, snapshot, pool_fee_apr, position_usd):
            return RebalanceReason.FEE_DRAG
        return None

    def _il_triggered(self, position: PositionState, snapshot: MarketSnapshot) -> bool:
        price_change = abs(price_from_tick(snapshot.tick) / position.entry_price_usd - 1)
        return price_change > self.config.max_il_pct

    def _fee_drag_triggered(
        self,
        position: PositionState,
        snapshot: MarketSnapshot,
        pool_fee_apr: Decimal,
        position_usd: Decimal,
    ) -> bool:
        tir = _rough_time_in_range(position, snapshot)
        return pool_fee_apr * tir < self.config.min_fee_apr

    # ---- gain estimation with cached TIR ----------------------------

    def _estimate_gain(
        self,
        position: PositionState,
        new_range: TickRange,
        snapshot: MarketSnapshot,
        vol_1d: Decimal,
        pool_fee_apr: Decimal,
        position_usd: Decimal,
        horizon_days: int = 7,
    ) -> Decimal:
        old_tir = self._cached_tir(position.tick_range, snapshot.tick, vol_1d, horizon_days)
        new_tir = self._cached_tir(new_range, snapshot.tick, vol_1d, horizon_days)
        daily_fee = pool_fee_apr * position_usd / Decimal(365)
        return max(Decimal(0), (new_tir - old_tir) * daily_fee * horizon_days)

    def _cached_tir(
        self,
        tick_range: TickRange,
        current_tick: int,
        vol_1d: Decimal,
        horizon_days: int,
    ) -> Decimal:
        key = (tick_range.lower, tick_range.upper, current_tick, str(vol_1d), horizon_days)
        cached = self._tir_cache.get(key)
        if cached and (time.time() - cached[1]) < _TIR_CACHE_TTL_S:
            return cached[0]
        tir = time_in_range_estimate(tick_range, current_tick, vol_1d, horizon_days)
        self._tir_cache[key] = (tir, time.time())
        # don't let this cache grow unbounded
        if len(self._tir_cache) > 256:
            oldest = min(self._tir_cache, key=lambda k: self._tir_cache[k][1])
            del self._tir_cache[oldest]
        return tir


# ---- cost estimation -------------------------------------------------

def _estimate_gas_cost(gas_price_gwei: int, eth_price_usd: Decimal) -> Decimal:
    # decreaseLiquidity + collect + mint ≈ 450k gas
    gas_wei = 450_000 * gas_price_gwei * 10**9
    return Decimal(gas_wei) / Decimal(10**18) * eth_price_usd


def _rough_time_in_range(position: PositionState, snapshot: MarketSnapshot) -> Decimal:
    if position.block_entered >= snapshot.block:
        return Decimal(1)
    return Decimal(1) if position.tick_range.contains(snapshot.tick) else Decimal("0.5")


def _tick_spacing_for_fee(fee_bps: int) -> int:
    return {1: 1, 5: 10, 30: 60, 100: 200}.get(fee_bps, 60)
