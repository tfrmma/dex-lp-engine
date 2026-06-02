"""
Prometheus metrics. Optional — if you don't want the dependency, set
METRICS_ENABLED=false and everything silently no-ops.

Exposes on :8000/metrics by default. Wire up to Grafana however you like.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal

log = logging.getLogger(__name__)

_enabled = os.getenv("METRICS_ENABLED", "true").lower() == "true"


def _noop(*a, **kw):
    pass


try:
    if _enabled:
        from prometheus_client import Counter, Gauge, Histogram, start_http_server
    else:
        raise ImportError("disabled")
except ImportError:
    # no prometheus_client installed or disabled — stub everything out
    class _Stub:
        def labels(self, **kw): return self
        inc = _noop; set = _noop; observe = _noop
    Counter = Histogram = Gauge = lambda *a, **kw: _Stub()
    def start_http_server(*a, **kw): pass
    _enabled = False


# ---- metric definitions ---------------------------------------------

position_value_usd = Gauge(
    "lp_position_value_usd", "Current position value in USD", ["pool"]
)
fees_earned_usd = Counter(
    "lp_fees_earned_usd_total", "Cumulative fees collected in USD", ["pool"]
)
il_current_pct = Gauge(
    "lp_il_current_pct", "Current IL as percentage of entry value", ["pool"]
)
hedge_delta = Gauge(
    "lp_hedge_delta", "Current hedge delta", ["pool"]
)
hedge_pnl_usd = Gauge(
    "lp_hedge_pnl_usd", "Hedge PnL in USD", ["pool"]
)
funding_accrued_usd = Gauge(
    "lp_funding_accrued_usd", "Accumulated funding cost in USD", ["pool"]
)
rebalance_count = Counter(
    "lp_rebalances_total", "Number of rebalances executed", ["pool", "reason"]
)
gas_spent_usd = Counter(
    "lp_gas_spent_usd_total", "Cumulative gas cost in USD", ["pool"]
)
time_in_range_ratio = Gauge(
    "lp_time_in_range_ratio", "Fraction of time position has been in range", ["pool"]
)
tick_distance_from_center = Gauge(
    "lp_tick_distance_from_center", "How far current tick is from range midpoint", ["pool"]
)
oracle_staleness_s = Gauge(
    "lp_oracle_staleness_seconds", "Seconds since last oracle update", ["pool"]
)


class EngineMetrics:
    """Thin wrapper so the engine doesn't import prometheus directly."""

    def __init__(self, pool_label: str, port: int = 8000) -> None:
        self._pool = pool_label
        if _enabled:
            try:
                start_http_server(port)
                log.info("metrics server on :%d", port)
            except OSError:
                log.warning("metrics port %d already in use, skipping", port)

    def update_position(self, value_usd: Decimal, il_pct: Decimal) -> None:
        position_value_usd.labels(pool=self._pool).set(float(value_usd))
        il_current_pct.labels(pool=self._pool).set(float(il_pct * 100))

    def record_fees(self, fees_usd: Decimal) -> None:
        fees_earned_usd.labels(pool=self._pool).inc(float(fees_usd))

    def update_hedge(self, delta: Decimal, pnl: Decimal, funding: Decimal) -> None:
        hedge_delta.labels(pool=self._pool).set(float(delta))
        hedge_pnl_usd.labels(pool=self._pool).set(float(pnl))
        funding_accrued_usd.labels(pool=self._pool).set(float(funding))

    def record_rebalance(self, reason: str, gas_usd: Decimal) -> None:
        rebalance_count.labels(pool=self._pool, reason=reason).inc()
        gas_spent_usd.labels(pool=self._pool).inc(float(gas_usd))

    def update_range_stats(self, tir: Decimal, tick_dist: int) -> None:
        time_in_range_ratio.labels(pool=self._pool).set(float(tir))
        tick_distance_from_center.labels(pool=self._pool).set(tick_dist)

    def update_oracle_staleness(self, age_s: float) -> None:
        oracle_staleness_s.labels(pool=self._pool).set(age_s)
