# dex-lp-engine

Concentrated liquidity LP strategy with dynamic IL hedging and automatic range rebalancing.

> **Test on testnet before deploying real capital.**
> The author is not responsible for losses.

## What it does

- **Range management** — sizes tick ranges from realized vol (log-normal), rebalances when price drifts out or fee drag exceeds threshold
- **IL hedging** — opens perp shorts via Hyperliquid to offset LP delta exposure when IL exceeds threshold
- **Fee optimization** — concentrates liquidity to maximize fee APR while keeping gas-adjusted return positive
- **Gas-aware rebalancing** — skips rebalances when expected gain < gas cost
- **Circuit breaker** — stops on drawdown breach, rebalance rate limits, stale oracle, or Redis kill switch
- **State persistence** — SQLite; engine recovers open positions after restart without re-querying on-chain

## Project layout

```
src/
  core/           types, IL math, fee math, pool data client (multicall)
  strategies/     range selection (vol-optimal, fee-optimized)
  hedging/        IL hedge manager, Hyperliquid + GMX v2 adapters
  rebalancing/    rebalance engine (detect → cost model → approve/skip)
  execution/      UniV3 NPM calls, nonce manager, tx signing
  utils/          price oracle, vol estimator, pool analytics, metrics,
                  circuit breaker, state store, key provider
```

## Running

```python
from web3 import AsyncWeb3
from src.core.pool_data import PoolDataClient
from src.core.types import EngineConfig, PoolKey, PoolProtocol
from src.engine import LPEngine
from src.execution.lp_executor import LPExecutor
from src.execution.nonce_manager import NonceManager
from src.hedging.perp_executor import HyperliquidAdapter, PerpHedgeExecutor
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.key_provider import key_provider_from_env
from src.utils.metrics import EngineMetrics
from src.utils.pool_analytics import PoolAnalytics
from src.utils.price_oracle import PriceOracle
from src.utils.state_store import StateStore

async def main():
    w3       = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(os.environ["ETH_RPC_URL"]))
    account  = os.environ["ACCOUNT_ADDRESS"]
    pk       = await key_provider_from_env().get_private_key()

    pool = PoolKey(
        address="0x8ad599c3A0ff1De082011EFDDc58f1908eb6e6D8",
        token0="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
        token1="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
        fee_bps=30,
        protocol=PoolProtocol.UNISWAP_V3,
    )
    config  = EngineConfig()
    nonces  = NonceManager(w3, account)

    hl_adapter    = HyperliquidAdapter(account, os.environ["HL_API_SECRET"])
    perp_executor = PerpHedgeExecutor(hl_adapter)

    engine = LPEngine(
        pool=pool,
        config=config,
        pool_client=PoolDataClient(w3),
        lp_executor=LPExecutor(w3, config, account, pk, nonces),
        perp_executor=perp_executor,
        price_oracle=PriceOracle(w3),
        pool_analytics=PoolAnalytics(),
        state_store=StateStore(),
        circuit_breaker=CircuitBreaker(pool_address=pool.address),
        metrics=EngineMetrics(str(pool)),
    )

    await engine.start(amount0_usd=Decimal("5000"), amount1_usd=Decimal("5000"))
    await engine.run_loop()
```

## Tests

```bash
pytest tests/ -v
```

## Key management

See `config/.env.example` for options. The `EnvKeyProvider` (reading `PRIVATE_KEY` from `.env`) is only appropriate for testnet. For mainnet, implement `KMSKeyProvider` or `VaultKeyProvider` in `src/utils/key_provider.py`.

## Known limitations / open TODOs

- GMX v2 adapter is stubbed (`NotImplementedError`). Use Hyperliquid or implement `ExchangeRouter.createOrder`.
- `delta_from_il` uses a simplified LP delta approximation — options hedging would need proper gamma/delta decomp.
- Vol estimator is EWMA on log returns. Yang-Zhang would be more accurate for gap-heavy markets.
- Fee growth inside is a point estimate without tick history — retrospective TIR is approximate.
- KMS and Vault key providers are stubs.

## Metrics

Prometheus metrics exposed on `:8000/metrics` when `METRICS_ENABLED=true`. Grafana dashboard not included — wire up the gauges however you like.

Key metrics: `lp_position_value_usd`, `lp_il_current_pct`, `lp_rebalances_total`, `lp_gas_spent_usd_total`, `lp_hedge_delta`, `lp_time_in_range_ratio`.
