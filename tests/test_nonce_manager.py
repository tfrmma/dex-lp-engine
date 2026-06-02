"""Nonce manager tests — mock the w3 call."""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.nonce_manager import NonceManager


def make_w3(starting_nonce: int = 5):
    w3 = MagicMock()
    w3.eth.get_transaction_count = AsyncMock(return_value=starting_nonce)
    return w3


@pytest.mark.asyncio
async def test_sequential_nonces():
    w3 = make_w3(10)
    nm = NonceManager(w3, "0xACCOUNT")
    assert await nm.next() == 10
    assert await nm.next() == 11
    assert await nm.next() == 12
    # should only have called get_transaction_count once
    w3.eth.get_transaction_count.assert_called_once()


@pytest.mark.asyncio
async def test_reset_resyncs_from_chain():
    w3 = make_w3(5)
    nm = NonceManager(w3, "0xACCOUNT")
    await nm.next()  # initializes to 5, returns 5
    await nm.reset()
    # after reset next call should re-fetch
    w3.eth.get_transaction_count.return_value = 7
    assert await nm.next() == 7
    assert w3.eth.get_transaction_count.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_nonces_no_collision():
    w3 = make_w3(0)
    nm = NonceManager(w3, "0xACCOUNT")
    nonces = await asyncio.gather(*[nm.next() for _ in range(10)])
    assert sorted(nonces) == list(range(10))
    assert len(set(nonces)) == 10  # all unique
