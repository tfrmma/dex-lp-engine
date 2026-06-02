"""Circuit breaker tests."""
import asyncio
import time
from decimal import Decimal

import pytest

from src.utils.circuit_breaker import CircuitBreaker


def make_breaker(**kwargs) -> CircuitBreaker:
    return CircuitBreaker(pool_address="0xPOOL", **kwargs)


def test_not_tripped_initially():
    cb = make_breaker()
    assert not cb.tripped
    assert not cb.stop_requested


def test_drawdown_trips_breaker():
    cb = make_breaker(max_pnl_drawdown_pct=Decimal("0.10"))
    cb.update_value(Decimal("10000"))   # set peak
    asyncio.get_event_loop().run_until_complete(
        cb.check(Decimal("8900"), time.time())  # 11% drawdown
    )
    assert cb.tripped
    assert "drawdown" in cb.trip_reason


def test_no_trip_below_drawdown_threshold():
    cb = make_breaker(max_pnl_drawdown_pct=Decimal("0.10"))
    cb.update_value(Decimal("10000"))
    asyncio.get_event_loop().run_until_complete(
        cb.check(Decimal("9500"), time.time())  # 5% drawdown — fine
    )
    assert not cb.tripped


def test_stale_oracle_trips_breaker():
    cb = make_breaker()
    stale_time = time.time() - 120   # 2 min ago
    asyncio.get_event_loop().run_until_complete(
        cb.check(Decimal("10000"), stale_time)
    )
    assert cb.tripped
    assert "stale" in cb.trip_reason


def test_rebalance_rate_hard_limit():
    cb = make_breaker(max_rebalances_per_hour=6)
    for _ in range(13):  # 2x limit
        cb.record_rebalance()
    asyncio.get_event_loop().run_until_complete(
        cb.check(Decimal("10000"), time.time())
    )
    assert cb.tripped


def test_allow_rebalance_soft_limit():
    cb = make_breaker(max_rebalances_per_hour=6)
    for _ in range(6):
        cb.record_rebalance()
    assert not cb.allow_rebalance()


def test_manual_stop_request():
    cb = make_breaker()
    cb.request_stop("test")
    assert cb.stop_requested
    assert not cb.tripped  # stop != trip
