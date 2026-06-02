"""
Main engine. One instance per pool.
Don't share state between instances.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

import structlog

from src.core.pool_data import PoolDataClient
from src.core.types import EngineConfig, PoolKey, PositionState
from src.execution.lp_executor import LPExecutor
from src.hedging.il_hedge import ILHedgeManager
from src.hedging.perp_executor import PerpHedgeExecutor
from src.rebalancing.rebalance_engine import RebalanceEngine
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.metrics import EngineMetrics
from src.utils.pool_analytics import PoolAnalytics
from src.utils.price_oracle import PriceOracle
from src.utils.state_store import StateStore
from src.utils.vol_estimator import VolEstimator

log = structlog.get_logger(__name__)


class LPEngine:
    def __init__(
        self,
        pool: PoolKey,
        config: EngineConfig,
        pool_client: PoolDataClient,
        lp_executor: LPExecutor,
        perp_executor: PerpHedgeExecutor,
        price_oracle: PriceOracle,
        pool_analytics: PoolAnalytics,
        state_store: StateStore,
        circuit_breaker: CircuitBreaker,
        metrics: EngineMetrics,
    ) -> None:
        self.pool   = pool
        self.config = config

        self._pool_client    = pool_client
        self._lp_executor    = lp_executor
        self._perp_executor  = perp_executor
        self._oracle         = price_oracle
        self._analytics      = pool_analytics
        self._store          = state_store
        self._breaker        = circuit_breaker
        self._metrics        = metrics

        self._hedge_manager    = ILHedgeManager(config)
        self._rebalance_engine = RebalanceEngine(config)
        self._vol_estimator    = VolEstimator()

        self._position: PositionState | None = None
        self._running   = False
        self._tir_ticks = 0   # blocks in range / total blocks

    # ---- lifecycle ---------------------------------------------------

    async def start(self, amount0_usd: Decimal, amount1_usd: Decimal) -> None:
        # try to restore from DB before opening a new position
        existing = self._store.restore_position(self.pool.address)
        if existing is not None:
            log.info("restored existing position", token_id=existing.token_id)
            self._position = existing
            hedge = self._store.restore_hedge(self.pool.address)
            if hedge:
                self._hedge_manager.on_hedge_opened(hedge)
            self._running = True
            return

        log.info("engine starting fresh", pool=str(self.pool))
        snapshot = await self._pool_client.fetch_snapshot(self.pool)
        self._vol_estimator.update(snapshot.price)
        vol = self._vol_estimator.realized_vol()

        from src.strategies.range_strategy import fee_optimized_range
        tick_range = fee_optimized_range(snapshot, self.config, vol)

        token_id, liquidity = await self._lp_executor.open_position(
            token0=self.pool.token0,
            token1=self.pool.token1,
            fee=self.pool.fee_bps * 100,
            tick_range=tick_range,
            amount0_desired=amount0_usd,
            amount1_desired=amount1_usd,
            current_sqrt_price=snapshot.sqrt_price_x96,
        )

        self._position = PositionState(
            pool=self.pool,
            tick_range=tick_range,
            liquidity=liquidity,
            token0_amount=amount0_usd,
            token1_amount=amount1_usd,
            fees_earned_token0=Decimal(0),
            fees_earned_token1=Decimal(0),
            entry_sqrt_price=snapshot.sqrt_price_x96,
            entry_price_usd=snapshot.price,
            block_entered=snapshot.block,
            token_id=token_id,
            current_tick=snapshot.tick,
        )
        self._store.save_position(self._position)
        self._running = True
        log.info("position opened", token_id=token_id, range=str(tick_range))

    async def stop(self) -> None:
        self._running = False
        if self._position and self._position.token_id:
            await self._lp_executor.close_position(
                self._position.token_id, self._position.liquidity
            )
        if self._hedge_manager.has_hedge:
            price_usd = await self._oracle.get_price_usd(self.pool.token0)
            pnl = await self._perp_executor.close_hedge(
                self._hedge_manager.current_hedge, price_usd
            )
            self._hedge_manager.on_hedge_closed(pnl)
        if self._position:
            self._store.delete_position(self.pool.address)
        log.info("engine stopped", pool=str(self.pool))

    async def run_loop(self, poll_interval_s: float = 12.0) -> None:
        """12s ≈ 1 block mainnet. Tune per chain."""
        while self._running:
            if self._breaker.tripped:
                log.error(
                    "circuit breaker tripped, halting",
                    reason=self._breaker.trip_reason,
                )
                break
            if self._breaker.stop_requested:
                log.info("graceful stop requested")
                await self.stop()
                break
            try:
                await self._tick()
            except Exception:
                log.exception("tick error, continuing")
            await asyncio.sleep(poll_interval_s)

    # ---- single tick -------------------------------------------------

    async def _tick(self) -> None:
        if self._position is None:
            return

        snapshot  = await self._pool_client.fetch_snapshot(self.pool)
        price_usd = await self._oracle.get_price_usd(self.pool.token0)
        eth_price = await self._oracle.get_price_usd("ETH")

        self._position.current_tick = snapshot.tick
        self._vol_estimator.update(snapshot.price)

        # circuit breaker check before doing anything
        pos_usd = self._position_value_usd(price_usd)
        oracle_age = self._oracle.last_update_time(self.pool.token0)
        await self._breaker.check(pos_usd, oracle_age)
        if self._breaker.tripped:
            return

        vol_1d    = self._vol_estimator.realized_vol()
        fee_apr   = await self._analytics.get_fee_apr(self.pool.address)

        await self._process_hedge(price_usd)
        await self._process_rebalance(snapshot, vol_1d, price_usd, eth_price, fee_apr)
        await self._maybe_collect_fees(snapshot)
        self._update_metrics(price_usd, fee_apr)

    async def _process_hedge(self, price_usd: Decimal) -> None:
        action = self._hedge_manager.evaluate(self._position, price_usd)
        if action is None:
            return

        pos_usd = self._position_value_usd(price_usd)

        if action.action_type == "open":
            state = await self._perp_executor.open_hedge(action.delta, price_usd, pos_usd)
            self._hedge_manager.on_hedge_opened(state)
            self._store.save_hedge(self.pool.address, state)

        elif action.action_type == "adjust" and self._hedge_manager.has_hedge:
            pnl = await self._perp_executor.adjust_hedge(
                self._hedge_manager.current_hedge, action.delta, price_usd
            )
            self._hedge_manager.on_hedge_adjusted(action.delta, pnl)
            self._store.save_hedge(self.pool.address, self._hedge_manager.current_hedge)

        if self._hedge_manager.should_remove_hedge(self._position, price_usd):
            pnl = await self._perp_executor.close_hedge(
                self._hedge_manager.current_hedge, price_usd
            )
            self._hedge_manager.on_hedge_closed(pnl)
            self._store.delete_position(self.pool.address)  # hedge gone, keep position

    async def _process_rebalance(
        self,
        snapshot,
        vol_1d: Decimal,
        price_usd: Decimal,
        eth_price: Decimal,
        fee_apr: Decimal,
    ) -> None:
        if not self._breaker.allow_rebalance():
            return

        gas_gwei = await self._get_gas_price_gwei()
        pos_usd  = self._position_value_usd(price_usd)

        proposal = self._rebalance_engine.evaluate(
            position=self._position,
            snapshot=snapshot,
            vol_1d=vol_1d,
            pool_fee_apr=fee_apr,
            gas_price_gwei=gas_gwei,
            position_usd=pos_usd,
            eth_price_usd=eth_price,
        )

        if not (proposal and proposal.approved):
            return

        log.info("rebalancing", proposal=str(proposal))
        self._breaker.record_rebalance()

        token_id, liquidity = await self._lp_executor.rebalance(
            self._position, proposal.proposed_range, snapshot.sqrt_price_x96
        )
        self._position.token_id         = token_id
        self._position.liquidity        = liquidity
        self._position.tick_range       = proposal.proposed_range
        self._position.entry_sqrt_price = snapshot.sqrt_price_x96
        self._store.save_position(self._position)

        self._metrics.record_rebalance(proposal.reason.name, proposal.estimated_gas_usd)

    async def _maybe_collect_fees(self, snapshot) -> None:
        if self._position.token_id is None:
            return
        blocks_since = snapshot.block - self._position.block_entered
        if blocks_since % 7_200 < 10:
            t0, t1 = await self._lp_executor.collect_fees(self._position.token_id)
            self._position.fees_earned_token0 += t0
            self._position.fees_earned_token1 += t1
            self._store.save_position(self._position)
            log.info("fees collected t0=%.6f t1=%.6f", t0, t1)

    def _update_metrics(self, price_usd: Decimal, fee_apr: Decimal) -> None:
        from src.core.il_math import compute_il
        pos_usd = self._position_value_usd(price_usd)

        price_ratio = price_usd / self._position.entry_price_usd
        il = compute_il(
            price_ratio,
            sqrt_lower=_sqrt_from_tick(self._position.tick_range.lower),
            sqrt_upper=_sqrt_from_tick(self._position.tick_range.upper),
            sqrt_entry=self._position.entry_sqrt_price,
        )
        self._metrics.update_position(pos_usd, il)

        if self._hedge_manager.has_hedge:
            h = self._hedge_manager.current_hedge
            self._metrics.update_hedge(h.delta, h.pnl_usd, h.funding_accrued)

        mid_tick = (self._position.tick_range.lower + self._position.tick_range.upper) // 2
        tick_dist = abs(self._position.current_tick - mid_tick)
        self._metrics.update_range_stats(Decimal("1"), tick_dist)

        oracle_age = time.time() - self._oracle.last_update_time(self.pool.token0)
        self._metrics.update_oracle_staleness(oracle_age)

    # ---- helpers -----------------------------------------------------

    def _position_value_usd(self, price_usd: Decimal) -> Decimal:
        if self._position is None:
            return Decimal(0)
        return (self._position.token0_amount + self._position.token1_amount) * price_usd

    async def _get_gas_price_gwei(self) -> int:
        try:
            gas_price = await self._lp_executor._w3.eth.gas_price
            return min(gas_price // 10**9, self.config.gas_price_gwei_cap)
        except Exception:
            return 30


def _sqrt_from_tick(tick: int) -> int:
    from src.core.il_math import sqrt_price_from_tick
    return sqrt_price_from_tick(tick)
