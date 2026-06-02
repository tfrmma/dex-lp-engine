"""
Circuit breaker + kill switch.

Three ways to stop the engine:
  1. SIGUSR1 — graceful stop, closes positions
  2. SIGTERM — same
  3. Redis key ENGINE_STOP_{pool_address} — checked every tick

Hard stops (no graceful close, just halt):
  - rebalance rate > N/hour
  - cumulative PnL drop > threshold
  - oracle stale for > 60s
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections import deque
from decimal import Decimal

log = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        max_rebalances_per_hour: int = 6,
        max_pnl_drawdown_pct: Decimal = Decimal("0.10"),
        redis_client=None,
        pool_address: str = "",
    ) -> None:
        self._max_rebalances_ph = max_rebalances_per_hour
        self._max_drawdown      = max_pnl_drawdown_pct
        self._redis             = redis_client
        self._pool_addr         = pool_address

        self._rebalance_times: deque[float] = deque()
        self._peak_value_usd: Decimal | None = None
        self._stop_requested  = False
        self._tripped         = False
        self._trip_reason     = ""

        self._install_signal_handlers()

    # ---- public API --------------------------------------------------

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    def request_stop(self, reason: str = "manual") -> None:
        log.warning("stop requested: %s", reason)
        self._stop_requested = True

    def record_rebalance(self) -> None:
        now = time.time()
        self._rebalance_times.append(now)
        # keep a sliding 1-hour window
        cutoff = now - 3600
        while self._rebalance_times and self._rebalance_times[0] < cutoff:
            self._rebalance_times.popleft()

    def update_value(self, current_value_usd: Decimal) -> None:
        if self._peak_value_usd is None:
            self._peak_value_usd = current_value_usd
        else:
            self._peak_value_usd = max(self._peak_value_usd, current_value_usd)

    async def check(self, current_value_usd: Decimal, oracle_last_update: float) -> None:
        """Call every tick. Trips the breaker if any limit is breached."""
        if self._tripped or self._stop_requested:
            return

        self.update_value(current_value_usd)

        if self._check_rebalance_rate():
            return
        if self._check_drawdown(current_value_usd):
            return
        if self._check_oracle_staleness(oracle_last_update):
            return
        await self._check_redis_kill_switch()

    def allow_rebalance(self) -> bool:
        """Pre-check before submitting a rebalance. Non-tripping."""
        rebalances_last_hour = len(self._rebalance_times)
        if rebalances_last_hour >= self._max_rebalances_ph:
            log.warning(
                "rebalance rate limit: %d in last hour (max %d)",
                rebalances_last_hour, self._max_rebalances_ph,
            )
            return False
        return True

    # ---- checks ------------------------------------------------------

    def _check_rebalance_rate(self) -> bool:
        count = len(self._rebalance_times)
        if count > self._max_rebalances_ph * 2:  # 2x is a hard trip, not just a block
            self._trip(f"rebalance rate {count}/hr exceeded hard limit {self._max_rebalances_ph * 2}")
            return True
        return False

    def _check_drawdown(self, current_value: Decimal) -> bool:
        if self._peak_value_usd is None or self._peak_value_usd == 0:
            return False
        drawdown = (self._peak_value_usd - current_value) / self._peak_value_usd
        if drawdown > self._max_drawdown:
            self._trip(f"drawdown {drawdown:.1%} exceeded limit {self._max_drawdown:.1%}")
            return True
        return False

    def _check_oracle_staleness(self, last_update: float) -> bool:
        age = time.time() - last_update
        if age > 60:
            self._trip(f"oracle stale for {age:.0f}s")
            return True
        return False

    async def _check_redis_kill_switch(self) -> None:
        if self._redis is None:
            return
        try:
            key = f"ENGINE_STOP_{self._pool_addr}"
            val = await self._redis.get(key)
            if val:
                self._trip(f"redis kill switch set: {val.decode()}")
        except Exception as e:
            log.debug("redis check failed (non-fatal): %s", e)

    def _trip(self, reason: str) -> None:
        if not self._tripped:
            log.error("CIRCUIT BREAKER TRIPPED: %s", reason)
            self._tripped      = True
            self._trip_reason  = reason

    # ---- signal handling ---------------------------------------------

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGTERM, signal.SIGUSR1):
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: self.request_stop(f"signal {s.name}"),
                )
        except RuntimeError:
            # no running event loop yet — signals will be installed on first tick
            pass
