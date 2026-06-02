"""
Nonce manager. Atomic counter per account so concurrent txs don't stomp each other.
One instance per (w3, account) pair — share it across executors if you're running
multiple pools from the same wallet.
"""
from __future__ import annotations

import asyncio
import logging
from web3 import AsyncWeb3

log = logging.getLogger(__name__)


class NonceManager:
    def __init__(self, w3: AsyncWeb3, account: str) -> None:
        self._w3 = w3
        self._account = account
        self._nonce: int | None = None
        self._lock = asyncio.Lock()

    async def next(self) -> int:
        async with self._lock:
            if self._nonce is None:
                self._nonce = await self._w3.eth.get_transaction_count(
                    self._account, "pending"
                )
            nonce = self._nonce
            self._nonce += 1
            return nonce

    async def reset(self) -> None:
        """Call after a revert or RPC error to resync from chain."""
        async with self._lock:
            self._nonce = None
            log.warning("nonce reset for %s", self._account[:10])

    async def peek(self) -> int:
        """Current value without incrementing. For debugging."""
        async with self._lock:
            if self._nonce is None:
                self._nonce = await self._w3.eth.get_transaction_count(
                    self._account, "pending"
                )
            return self._nonce
