"""
Pool analytics: 24h volume, TVL, fee APR.
Pulls from Uniswap v3 subgraph on The Graph. Falls back to DeFiLlama if the
subgraph is being its usual unreliable self.

Subgraph can lag by several minutes during high load — don't use these numbers
for anything latency-sensitive. Fine for rebalance decisions at 12s intervals.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

import aiohttp

log = logging.getLogger(__name__)

# Uniswap v3 subgraph — mainnet. Change for Arbitrum/Base/etc.
_UNISWAP_SUBGRAPH = (
    "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3"
)
_DEFILLAMA_POOLS  = "https://yields.llama.fi/pools"

_CACHE_TTL_S = 300  # 5 min is fine; fee APR doesn't move that fast


class PoolAnalytics:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[Decimal, float]] = {}
        self._session: aiohttp.ClientSession | None = None

    async def get_fee_apr(self, pool_address: str) -> Decimal:
        cached = self._cache.get(pool_address)
        if cached and (time.time() - cached[1]) < _CACHE_TTL_S:
            return cached[0]

        try:
            apr = await self._fee_apr_from_subgraph(pool_address)
        except Exception as e:
            log.warning("subgraph failed for %s: %s — trying llama", pool_address[:10], e)
            try:
                apr = await self._fee_apr_from_llama(pool_address)
            except Exception as e2:
                log.error("llama also failed: %s — using fallback 0.3", e2)
                apr = Decimal("0.3")  # conservative fallback; better than 0.5 hardcoded

        self._cache[pool_address] = (apr, time.time())
        return apr

    async def get_tvl_and_volume(self, pool_address: str) -> tuple[Decimal, Decimal]:
        """Returns (tvl_usd, volume_24h_usd)."""
        query = _pool_query(pool_address.lower())
        try:
            data = await self._gql(query)
            pool = data["data"]["pool"]
            tvl    = Decimal(pool["totalValueLockedUSD"])
            vol24h = Decimal(pool["poolDayData"][0]["volumeUSD"]) if pool["poolDayData"] else Decimal(0)
            return tvl, vol24h
        except Exception as e:
            log.warning("failed to fetch TVL/volume for %s: %s", pool_address[:10], e)
            return Decimal(0), Decimal(0)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---- internal ----------------------------------------------------

    async def _fee_apr_from_subgraph(self, pool_address: str) -> Decimal:
        tvl, vol24h = await self.get_tvl_and_volume(pool_address)
        if tvl <= 0:
            raise ValueError("zero TVL from subgraph")

        # need the fee tier to compute APR from volume
        query = _pool_fee_query(pool_address.lower())
        data  = await self._gql(query)
        fee_tier = int(data["data"]["pool"]["feeTier"])  # e.g. 3000 = 0.3%

        daily_fees = vol24h * Decimal(fee_tier) / Decimal(1_000_000)
        return daily_fees / tvl * 365

    async def _fee_apr_from_llama(self, pool_address: str) -> Decimal:
        session = self._get_session()
        async with session.get(_DEFILLAMA_POOLS, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            data = await resp.json()

        addr_lower = pool_address.lower()
        for pool in data.get("data", []):
            if pool.get("pool", "").lower() == addr_lower:
                apy = pool.get("apyBase") or pool.get("apy") or 0
                return Decimal(str(apy)) / 100

        raise ValueError(f"pool {pool_address[:10]} not found in llama")

    async def _gql(self, query: str) -> dict:
        session = self._get_session()
        async with session.post(
            _UNISWAP_SUBGRAPH,
            json={"query": query},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if "errors" in data:
                raise ValueError(f"subgraph error: {data['errors']}")
            return data

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session


# ---- GQL queries -----------------------------------------------------

def _pool_query(address: str) -> str:
    return f"""
    {{
      pool(id: "{address}") {{
        totalValueLockedUSD
        feeTier
        poolDayData(first: 1, orderBy: date, orderDirection: desc) {{
          volumeUSD
        }}
      }}
    }}
    """

def _pool_fee_query(address: str) -> str:
    return f"""
    {{
      pool(id: "{address}") {{
        feeTier
      }}
    }}
    """
