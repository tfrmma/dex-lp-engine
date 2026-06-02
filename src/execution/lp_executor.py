"""
LP execution: mint, burn, collect, rebalance.

All calls go through NonfungiblePositionManager. We don't use the router for
LP ops — it adds a hop and makes debugging harder.

EIP-1559 gas everywhere. Type 0 txs on mainnet are a liability.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from eth_abi import decode as abi_decode
from eth_abi import encode
from tenacity import retry, stop_after_attempt, wait_fixed
from web3 import AsyncWeb3
from web3.types import TxReceipt

from src.core.types import EngineConfig, PositionState, TickRange
from src.execution.nonce_manager import NonceManager

log = logging.getLogger(__name__)

# NonfungiblePositionManager — mainnet + most L2s
NPM_ADDR = "0xC36442b4a4522E871399CD717aBDD847Ab11FE88"

_MINT_SEL         = bytes.fromhex("88316456")
_DECREASE_LIQ_SEL = bytes.fromhex("0c49ccbe")
_COLLECT_SEL      = bytes.fromhex("fc6f7865")
_BURN_SEL         = bytes.fromhex("42966c68")

_UINT128_MAX = 2**128 - 1

# Correct keccak256 event topics (full 32 bytes)
# keccak256("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
_TOPIC_INCREASE_LIQ = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35"
# keccak256("Collect(uint256,address,uint128,uint128)")
_TOPIC_COLLECT      = "0x40d0efd1a53d60ecbf40971b9daf7dc90178c3eddac09f81fd45e6ac75c17640"


class LPExecutor:
    def __init__(
        self,
        w3: AsyncWeb3,
        config: EngineConfig,
        account: str,
        private_key: str,
        nonce_manager: NonceManager,
        min_confirmations: int = 2,
    ) -> None:
        self._w3 = w3
        self.config = config
        self.account = account
        self._pk = private_key
        self._nonces = nonce_manager
        self._min_confirmations = min_confirmations

    async def open_position(
        self,
        token0: str,
        token1: str,
        fee: int,
        tick_range: TickRange,
        amount0_desired: Decimal,
        amount1_desired: Decimal,
        current_sqrt_price: int,
    ) -> tuple[int, int]:
        amount0_min, amount1_min = _apply_slippage(
            amount0_desired, amount1_desired, self.config.slippage_tolerance_bps
        )
        deadline = await self._deadline()
        call_data = _encode_mint(
            token0, token1, fee, tick_range,
            amount0_desired, amount1_desired,
            amount0_min, amount1_min,
            self.account, deadline,
        )
        receipt = await self._send(NPM_ADDR, call_data)
        token_id, liquidity = _parse_mint_receipt(receipt)
        log.info("minted LP token_id=%d liquidity=%d", token_id, liquidity)
        return token_id, liquidity

    async def close_position(self, token_id: int, liquidity: int) -> tuple[Decimal, Decimal]:
        await self._decrease_liquidity(token_id, liquidity)
        t0, t1 = await self._collect_all(token_id)
        await self._burn(token_id)
        return t0, t1

    async def collect_fees(self, token_id: int) -> tuple[Decimal, Decimal]:
        return await self._collect_all(token_id)

    async def rebalance(
        self,
        position: PositionState,
        new_range: TickRange,
        current_sqrt_price: int,
    ) -> tuple[int, int]:
        """
        Close + reopen. Not atomic — we accept the gap risk.
        A custom rebalancer contract would fix this but adds audit surface.
        """
        if position.token_id is None:
            raise ValueError("no token_id on position, can't rebalance")

        await self._decrease_liquidity(position.token_id, position.liquidity)
        t0_collected, t1_collected = await self._collect_all(position.token_id)
        await self._burn(position.token_id)

        log.info(
            "rebalancing: collected t0=%.6f t1=%.6f new_range=%s",
            t0_collected, t1_collected, new_range,
        )

        return await self.open_position(
            token0=position.pool.token0,
            token1=position.pool.token1,
            fee=position.pool.fee_bps * 100,
            tick_range=new_range,
            amount0_desired=t0_collected,
            amount1_desired=t1_collected,
            current_sqrt_price=current_sqrt_price,
        )

    # ---- private tx helpers -----------------------------------------

    async def _decrease_liquidity(self, token_id: int, liquidity: int) -> None:
        deadline = await self._deadline()
        call_data = _encode_decrease_liquidity(token_id, liquidity, 0, 0, deadline)
        await self._send(NPM_ADDR, call_data)

    async def _collect_all(self, token_id: int) -> tuple[Decimal, Decimal]:
        call_data = _encode_collect(token_id, self.account, _UINT128_MAX, _UINT128_MAX)
        receipt = await self._send(NPM_ADDR, call_data)
        return _parse_collect_receipt(receipt)

    async def _burn(self, token_id: int) -> None:
        call_data = _BURN_SEL + encode(["uint256"], [token_id])
        await self._send(NPM_ADDR, call_data)

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
    async def _send(self, to: str, data: bytes) -> TxReceipt:
        gas_params = await _get_gas_params(self._w3, self.config.gas_price_gwei_cap)
        nonce = await self._nonces.next()

        tx = {
            "from":  self.account,
            "to":    to,
            "data":  data,
            "gas":   600_000,
            "nonce": nonce,
            **gas_params,
        }

        try:
            signed   = self._w3.eth.account.sign_transaction(tx, private_key=self._pk)
            tx_hash  = await self._w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt  = await self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        except Exception:
            await self._nonces.reset()
            raise

        if receipt["status"] != 1:
            await self._nonces.reset()
            raise RuntimeError(f"tx {tx_hash.hex()} reverted")

        # wait for N confirmations before trusting the state
        await self._wait_confirmations(receipt["blockNumber"])

        log.debug("tx %s gas_used=%d", tx_hash.hex()[:12], receipt["gasUsed"])
        return receipt

    async def _wait_confirmations(self, tx_block: int) -> None:
        if self._min_confirmations <= 1:
            return
        while True:
            latest = await self._w3.eth.block_number
            if latest - tx_block >= self._min_confirmations:
                return
            import asyncio
            await asyncio.sleep(3)

    async def _deadline(self, minutes: int = 10) -> int:
        block = await self._w3.eth.get_block("latest")
        return block["timestamp"] + minutes * 60


# ---- ABI encoding ----------------------------------------------------

def _encode_mint(
    token0: str, token1: str, fee: int,
    tick_range: TickRange,
    amount0_desired: Decimal, amount1_desired: Decimal,
    amount0_min: Decimal, amount1_min: Decimal,
    recipient: str, deadline: int,
) -> bytes:
    return _MINT_SEL + encode(
        ["address","address","uint24","int24","int24",
         "uint256","uint256","uint256","uint256","address","uint256"],
        [token0, token1, fee,
         tick_range.lower, tick_range.upper,
         int(amount0_desired), int(amount1_desired),
         int(amount0_min), int(amount1_min),
         recipient, deadline],
    )


def _encode_decrease_liquidity(
    token_id: int, liquidity: int,
    amount0_min: int, amount1_min: int, deadline: int,
) -> bytes:
    return _DECREASE_LIQ_SEL + encode(
        ["uint256","uint128","uint256","uint256","uint256"],
        [token_id, liquidity, amount0_min, amount1_min, deadline],
    )


def _encode_collect(token_id: int, recipient: str, max0: int, max1: int) -> bytes:
    return _COLLECT_SEL + encode(
        ["uint256","address","uint128","uint128"],
        [token_id, recipient, max0, max1],
    )


# ---- receipt parsing -------------------------------------------------
# topic[0] is the full keccak256 of the event signature — NOT a function selector.
# The old code matched on 4 bytes of what is actually a 32-byte topic, so it never matched.

def _parse_mint_receipt(receipt: TxReceipt) -> tuple[int, int]:
    for entry in receipt.get("logs", []):
        topics = entry.get("topics", [])
        if not topics:
            continue
        topic0 = topics[0].hex() if isinstance(topics[0], bytes) else topics[0]
        if topic0.lower().startswith(_TOPIC_INCREASE_LIQ.lstrip("0x").lower()[:8]):
            # tokenId is topics[1], liquidity/amounts are in data
            token_id = int(topics[1].hex(), 16) if len(topics) > 1 else 0
            raw = bytes.fromhex(entry["data"].removeprefix("0x"))
            liquidity, amount0, amount1 = abi_decode(["uint128","uint256","uint256"], raw)
            return token_id, liquidity
    raise ValueError("IncreaseLiquidity log not found in mint receipt")


def _parse_collect_receipt(receipt: TxReceipt) -> tuple[Decimal, Decimal]:
    for entry in receipt.get("logs", []):
        topics = entry.get("topics", [])
        if not topics:
            continue
        topic0 = topics[0].hex() if isinstance(topics[0], bytes) else topics[0]
        if topic0.lower().startswith(_TOPIC_COLLECT.lstrip("0x").lower()[:8]):
            raw = bytes.fromhex(entry["data"].removeprefix("0x"))
            # Collect(tokenId, recipient, amount0, amount1) — recipient in topics[2]
            amount0, amount1 = abi_decode(["uint128","uint128"], raw)
            return Decimal(amount0), Decimal(amount1)
    # no collect event = nothing to collect, not an error
    return Decimal(0), Decimal(0)


# ---- gas helpers -----------------------------------------------------

async def _get_gas_params(w3: AsyncWeb3, cap_gwei: int) -> dict:
    latest   = await w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas", 0)
    priority = int(1.5 * 10**9)  # 1.5 gwei tip
    max_fee  = min(base_fee * 2 + priority, cap_gwei * 10**9)
    return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}


def _apply_slippage(
    amount0: Decimal, amount1: Decimal, slippage_bps: int
) -> tuple[Decimal, Decimal]:
    factor = Decimal(1) - Decimal(slippage_bps) / Decimal(10_000)
    return amount0 * factor, amount1 * factor
