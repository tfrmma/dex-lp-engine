"""
Perp hedge execution. Adapter pattern: swap the venue by swapping the adapter.
PerpAdapter is the interface; HyperliquidAdapter is the default working implementation.
GmxV2Adapter is also wired up — choose via config.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from abc import ABC, abstractmethod
from decimal import Decimal

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from src.core.types import HedgeInstrument, HedgeState

log = logging.getLogger(__name__)


class PerpAdapter(ABC):
    @abstractmethod
    async def open_short(self, size_usd: Decimal, entry_price: Decimal) -> dict: ...
    @abstractmethod
    async def adjust_position(self, delta_usd: Decimal) -> dict: ...
    @abstractmethod
    async def close_position(self) -> dict: ...
    @abstractmethod
    async def get_position(self) -> dict | None: ...
    @abstractmethod
    async def get_funding_rate(self) -> Decimal: ...


class PerpHedgeExecutor:
    def __init__(self, adapter: PerpAdapter, slippage_bps: int = 30) -> None:
        self._adapter      = adapter
        self._slippage_bps = slippage_bps

    async def open_hedge(
        self,
        delta: Decimal,
        spot_price: Decimal,
        position_usd: Decimal,
    ) -> HedgeState:
        notional = abs(delta) * spot_price
        if notional < Decimal("100"):
            raise ValueError(f"hedge notional {notional:.2f} too small")

        receipt = await self._open_with_retry(notional, spot_price)
        log.info("hedge opened notional=%.2f id=%s", notional, receipt.get("id"))

        return HedgeState(
            instrument=HedgeInstrument.PERP,
            notional_usd=notional,
            delta=delta,
            target_delta=delta,
            entry_price=spot_price,
        )

    async def adjust_hedge(
        self,
        current_hedge: HedgeState,
        target_delta: Decimal,
        spot_price: Decimal,
    ) -> Decimal:
        delta_change    = target_delta - current_hedge.delta
        notional_change = delta_change * spot_price
        receipt = await self._adjust_with_retry(notional_change)
        log.info("hedge adjusted delta %+.4f id=%s", delta_change, receipt.get("id"))
        pnl = (spot_price - current_hedge.entry_price) * abs(current_hedge.delta) * -1
        return pnl

    async def close_hedge(self, current_hedge: HedgeState, spot_price: Decimal) -> Decimal:
        receipt = await self._adapter.close_position()
        pnl = (spot_price - current_hedge.entry_price) * abs(current_hedge.delta) * -1
        log.info("hedge closed pnl=%.4f id=%s", pnl, receipt.get("id"))
        return pnl

    async def get_funding_cost(self) -> Decimal:
        try:
            return await self._adapter.get_funding_rate()
        except Exception:
            log.warning("funding rate fetch failed, returning 0")
            return Decimal(0)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def _open_with_retry(self, notional: Decimal, price: Decimal) -> dict:
        return await self._adapter.open_short(notional, price)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def _adjust_with_retry(self, notional_change: Decimal) -> dict:
        return await self._adapter.adjust_position(notional_change)


# ---- Hyperliquid adapter --------------------------------------------
# REST API docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api

class HyperliquidAdapter(PerpAdapter):
    """
    Hyperliquid perp adapter. Simplest of the major venues to integrate —
    no on-chain txs, just signed REST calls.

    Requires an API wallet (separate from your main wallet is strongly recommended).
    """
    _BASE = "https://api.hyperliquid.xyz"

    def __init__(self, account: str, api_secret: str, asset: str = "ETH") -> None:
        self._account    = account
        self._secret     = api_secret
        self._asset      = asset
        self._session: aiohttp.ClientSession | None = None

    async def open_short(self, size_usd: Decimal, entry_price: Decimal) -> dict:
        sz = float(size_usd / entry_price)
        order = {
            "type":       {"limit": {"tif": "Ioc"}},
            "asset":      self._asset,
            "isBuy":      False,
            "limitPx":    float(entry_price * Decimal("0.995")),  # 0.5% below mid
            "sz":         round(sz, 4),
            "reduceOnly": False,
        }
        return await self._signed_post("/exchange", {"action": {"type": "order", "orders": [order], "grouping": "na"}})

    async def adjust_position(self, delta_usd: Decimal) -> dict:
        pos = await self.get_position()
        current_sz = Decimal(pos["szi"]) if pos else Decimal(0)
        price      = await self._get_mid_price()
        target_sz  = current_sz - delta_usd / price  # negative = short
        diff_sz    = target_sz - current_sz

        if abs(diff_sz) < Decimal("0.001"):
            return {"id": "noop"}

        is_buy = diff_sz > 0
        order = {
            "type":       {"limit": {"tif": "Ioc"}},
            "asset":      self._asset,
            "isBuy":      is_buy,
            "limitPx":    float(price * (Decimal("1.005") if is_buy else Decimal("0.995"))),
            "sz":         round(abs(float(diff_sz)), 4),
            "reduceOnly": False,
        }
        return await self._signed_post("/exchange", {"action": {"type": "order", "orders": [order], "grouping": "na"}})

    async def close_position(self) -> dict:
        pos = await self.get_position()
        if not pos or Decimal(pos.get("szi", "0")) == 0:
            return {"id": "no_position"}
        price = await self._get_mid_price()
        sz    = abs(float(pos["szi"]))
        is_buy = Decimal(pos["szi"]) < 0  # closing a short = buying

        order = {
            "type":       {"limit": {"tif": "Ioc"}},
            "asset":      self._asset,
            "isBuy":      is_buy,
            "limitPx":    float(price * (Decimal("1.01") if is_buy else Decimal("0.99"))),
            "sz":         round(sz, 4),
            "reduceOnly": True,
        }
        return await self._signed_post("/exchange", {"action": {"type": "order", "orders": [order], "grouping": "na"}})

    async def get_position(self) -> dict | None:
        resp = await self._post("/info", {"type": "clearinghouseState", "user": self._account})
        for pos in resp.get("assetPositions", []):
            if pos["position"]["coin"] == self._asset:
                return pos["position"]
        return None

    async def get_funding_rate(self) -> Decimal:
        resp = await self._post("/info", {"type": "metaAndAssetCtxs"})
        for i, meta in enumerate(resp[0].get("universe", [])):
            if meta["name"] == self._asset:
                funding = resp[1][i].get("funding", "0")
                return Decimal(str(funding))
        return Decimal(0)

    async def _get_mid_price(self) -> Decimal:
        resp = await self._post("/info", {"type": "allMids"})
        return Decimal(str(resp.get(self._asset, "0")))

    async def _signed_post(self, path: str, payload: dict) -> dict:
        import json
        ts  = int(time.time() * 1000)
        msg = json.dumps({"action": payload.get("action"), "nonce": ts, "vaultAddress": None}, separators=(",", ":"), sort_keys=True)
        sig = hmac.new(self._secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        body = {**payload, "nonce": ts, "signature": {"r": sig[:64], "s": sig[64:], "v": 27}}
        return await self._post(path, body)

    async def _post(self, path: str, body: dict) -> dict:
        session = self._get_session()
        async with session.post(
            self._BASE + path, json=body, timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ---- GMX v2 adapter -------------------------------------------------
# Docs: https://github.com/gmx-io/gmx-synthetics
# The ABI changed twice in 2024 — verify against deployed contracts before using.

class GmxV2Adapter(PerpAdapter):
    """
    GMX v2 Synthetics. On-chain txs via ExchangeRouter.
    More complex than HL but deeper liquidity for large notionals.
    Needs a working NonceManager instance shared with LPExecutor.
    """
    def __init__(
        self,
        w3,
        account: str,
        private_key: str,
        exchange_router: str,
        market_address: str,
        collateral_token: str,  # USDC typically
    ) -> None:
        self._w3               = w3
        self._account          = account
        self._pk               = private_key
        self._exchange_router  = exchange_router
        self._market           = market_address
        self._collateral       = collateral_token

    async def open_short(self, size_usd: Decimal, entry_price: Decimal) -> dict:
        # TODO: wire up createOrder on ExchangeRouter
        # Params: market, collateralToken, isLong=False, sizeDeltaUsd, ...
        raise NotImplementedError(
            "GMX v2 open_short not implemented. "
            "Use HyperliquidAdapter or implement ExchangeRouter.createOrder."
        )

    async def adjust_position(self, delta_usd: Decimal) -> dict:
        raise NotImplementedError

    async def close_position(self) -> dict:
        raise NotImplementedError

    async def get_position(self) -> dict | None:
        raise NotImplementedError

    async def get_funding_rate(self) -> Decimal:
        raise NotImplementedError
