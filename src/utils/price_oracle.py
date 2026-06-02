"""
Price oracle. Pyth primary, Chainlink fallback.
If both are down you have bigger problems.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

import aiohttp

log = logging.getLogger(__name__)

_CL_ETH_USD  = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"
_PYTH_API    = "https://hermes.pyth.network/v2/updates/price/latest"
_STALE_AFTER = 60

_PYTH_IDS: dict[str, str] = {
    "ETH":  "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "WBTC": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "USDC": "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
}


class PriceOracle:
    def __init__(self, w3=None) -> None:
        self._w3 = w3
        self._cache: dict[str, tuple[Decimal, float]] = {}
        self._session: aiohttp.ClientSession | None = None

    async def get_price_usd(self, symbol: str) -> Decimal:
        cached = self._cache.get(symbol)
        if cached and (time.time() - cached[1]) < 10:
            return cached[0]

        try:
            price = await self._fetch_pyth(symbol)
        except Exception as e:
            log.warning("pyth failed for %s: %s — trying chainlink", symbol, e)
            price = await self._fetch_chainlink(symbol)

        self._cache[symbol] = (price, time.time())
        return price

    def last_update_time(self, symbol: str) -> float:
        cached = self._cache.get(symbol)
        return cached[1] if cached else 0.0

    def is_stale(self, symbol: str) -> bool:
        return (time.time() - self.last_update_time(symbol)) > _STALE_AFTER

    async def _fetch_pyth(self, symbol: str) -> Decimal:
        price_id = _PYTH_IDS.get(symbol)
        if not price_id:
            raise ValueError(f"no pyth id for {symbol}")

        session = self._get_session()
        async with session.get(
            _PYTH_API,
            params={"ids[]": price_id},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        item = data["parsed"][0]["price"]
        return Decimal(item["price"]) * Decimal(10) ** Decimal(item["expo"])

    async def _fetch_chainlink(self, symbol: str) -> Decimal:
        if self._w3 is None:
            raise RuntimeError("w3 not configured, chainlink unavailable")
        from eth_abi import decode as abi_decode
        result = await self._w3.eth.call(
            {"to": _CL_ETH_USD, "data": bytes.fromhex("feaf968c")}
        )
        _, answer, _, _, _ = abi_decode(
            ["uint80","int256","uint256","uint256","uint80"], result
        )
        return Decimal(answer) / Decimal(10**8)

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
