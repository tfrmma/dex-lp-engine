"""
Realized vol estimator. Yang-Zhang handles gap + open-to-close variance,
which matters for 24/7 crypto markets where gaps happen constantly.

For now: simple EWMA on log returns. Good enough.
TODO: implement proper Yang-Zhang or Parkinson — matters for options hedging sizing
"""
from __future__ import annotations

import math
from collections import deque
from decimal import Decimal


class VolEstimator:
    def __init__(self, window: int = 288, ewma_alpha: float = 0.06) -> None:
        # 288 samples = 24h at 5-min intervals
        self._window = window
        self._alpha  = ewma_alpha
        self._prices: deque[Decimal] = deque(maxlen=window)
        self._ewma_var: float = 0.0

    def update(self, price: Decimal) -> None:
        if self._prices:
            log_ret = float(math.log(float(price) / float(self._prices[-1])))
            self._ewma_var = (
                self._alpha * log_ret**2 + (1 - self._alpha) * self._ewma_var
            )
        self._prices.append(price)

    def realized_vol(self, annualize: bool = True) -> Decimal:
        """Annualized vol from EWMA. Returns 0 if insufficient data."""
        if len(self._prices) < 10:
            return Decimal("0.5")  # assume 50% vol if we have no data

        daily_vol = math.sqrt(self._ewma_var * 288)  # scale to daily (288 5-min intervals)
        annualized = daily_vol * math.sqrt(365)
        return Decimal(str(annualized))

    def simple_realized_vol(self) -> Decimal:
        """Non-EWMA version. Slower to react but less noise."""
        if len(self._prices) < 2:
            return Decimal("0.5")

        prices = list(self._prices)
        log_returns = [
            math.log(float(prices[i]) / float(prices[i - 1]))
            for i in range(1, len(prices))
        ]
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        return Decimal(str(math.sqrt(variance * 288 * 365)))

    @property
    def sample_count(self) -> int:
        return len(self._prices)
