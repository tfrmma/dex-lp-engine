"""
Pool data fetcher. Wraps multicall so we can get everything in one round-trip.
Single-call path kept for fallback/debug — don't use it in the hot loop.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from functools import lru_cache

from eth_abi import decode
from web3 import AsyncWeb3
from web3.types import BlockIdentifier

from src.core.types import MarketSnapshot, PoolKey, PositionState, TickRange

log = logging.getLogger(__name__)

# keccak signatures — precomputed, obviously
_SLOT0_SIG       = "0x3850c7bd"
_LIQUIDITY_SIG   = "0x1a686502"
_POSITION_SIG    = "0x514ea4bf"  # positions(uint256 tokenId)
_TICK_SIG        = "0xf30dba93"  # ticks(int24)

# Multicall3
_MC3_ADDR = "0xcA11bde05977b3631167028862bE2a173976CA11"
_MC3_ABI_AGGREGATE3 = {
    "inputs": [{"components": [
        {"name": "target", "type": "address"},
        {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"},
    ], "name": "calls", "type": "tuple[]"}],
    "name": "aggregate3",
    "outputs": [{"components": [
        {"name": "success", "type": "bool"},
        {"name": "returnData", "type": "bytes"},
    ], "name": "returnData", "type": "tuple[]"}],
    "type": "function",
}


class PoolDataClient:
    def __init__(self, w3: AsyncWeb3) -> None:
        self._w3 = w3
        self._mc3 = w3.eth.contract(address=_MC3_ADDR, abi=[_MC3_ABI_AGGREGATE3])

    async def fetch_snapshot(
        self, pool: PoolKey, block: BlockIdentifier = "latest"
    ) -> MarketSnapshot:
        calls = [
            _make_call(pool.address, _SLOT0_SIG),
            _make_call(pool.address, _LIQUIDITY_SIG),
            _make_call(pool.address, "0xddca3f43"),  # feeGrowthGlobal0X128
            _make_call(pool.address, "0xa4e007a6"),  # feeGrowthGlobal1X128
        ]
        results = await self._mc3.functions.aggregate3(calls).call(block_identifier=block)

        slot0    = _decode_slot0(results[0][1])
        liq      = decode(["uint128"], results[1][1])[0]
        fg0      = decode(["uint256"], results[2][1])[0]
        fg1      = decode(["uint256"], results[3][1])[0]
        blk_data = await self._w3.eth.get_block(block)

        return MarketSnapshot(
            pool=pool,
            sqrt_price_x96=slot0["sqrtPriceX96"],
            tick=slot0["tick"],
            liquidity=liq,
            fee_growth_global0=fg0,
            fee_growth_global1=fg1,
            block=blk_data["number"],
            timestamp=blk_data["timestamp"],
        )

    async def fetch_position(self, pool: PoolKey, token_id: int) -> dict:
        """Raw position data from NonfungiblePositionManager."""
        call_data = _POSITION_SIG + decode(["uint256"], token_id.to_bytes(32)).hex()
        # TODO: cache this per block — we're calling it too often right now
        result = await self._w3.eth.call(
            {"to": pool.address, "data": call_data}
        )
        return _decode_position(result)

    async def fetch_tick_data(self, pool_addr: str, tick: int) -> dict:
        tick_bytes = tick.to_bytes(3, signed=True, byteorder="big").rjust(32, b"\x00")
        call_data = _TICK_SIG + tick_bytes.hex()
        result = await self._w3.eth.call({"to": pool_addr, "data": call_data})
        return _decode_tick(result)

    async def fetch_tick_pair(self, pool_addr: str, tick_range: TickRange) -> tuple[dict, dict]:
        """Fetch both ticks of a range in one multicall. Use this, not fetch_tick_data twice."""
        calls = [
            _make_call(pool_addr, _build_tick_calldata(tick_range.lower)),
            _make_call(pool_addr, _build_tick_calldata(tick_range.upper)),
        ]
        results = await self._mc3.functions.aggregate3(calls).call()
        return _decode_tick(results[0][1]), _decode_tick(results[1][1])


# ---- decode helpers --------------------------------------------------

def _decode_slot0(data: bytes) -> dict:
    types = ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]
    vals  = decode(types, data)
    return {
        "sqrtPriceX96": vals[0],
        "tick":         vals[1],
        "observationIndex": vals[2],
    }


def _decode_position(data: bytes) -> dict:
    types = ["uint96", "address", "address", "address", "uint24", "int24", "int24",
             "uint128", "uint256", "uint256", "uint128", "uint128"]
    vals = decode(types, data)
    return {
        "nonce": vals[0], "operator": vals[1],
        "token0": vals[2], "token1": vals[3],
        "fee": vals[4], "tickLower": vals[5], "tickUpper": vals[6],
        "liquidity": vals[7],
        "feeGrowthInside0LastX128": vals[8],
        "feeGrowthInside1LastX128": vals[9],
        "tokensOwed0": vals[10], "tokensOwed1": vals[11],
    }


def _decode_tick(data: bytes) -> dict:
    types = ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"]
    vals = decode(types, data)
    return {
        "liquidityGross": vals[0], "liquidityNet": vals[1],
        "feeGrowthOutside0X128": vals[2], "feeGrowthOutside1X128": vals[3],
        "initialized": vals[7],
    }


def _build_tick_calldata(tick: int) -> str:
    tick_bytes = tick.to_bytes(3, signed=True, byteorder="big").rjust(32, b"\x00")
    return _TICK_SIG + tick_bytes.hex()


def _make_call(target: str, call_data: str) -> tuple:
    return (target, True, bytes.fromhex(call_data.removeprefix("0x")))
