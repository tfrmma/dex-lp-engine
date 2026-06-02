"""
Position state persistence. SQLite because it's zero-ops and more than fast enough
for the write frequency here (~1 write per rebalance).

On restart, the engine calls restore() to reconstruct PositionState from the DB
rather than starting blind. If the DB is missing we fall back to on-chain scan.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path

from src.core.types import (
    EngineConfig, HedgeInstrument, HedgeState,
    PoolKey, PoolProtocol, PositionState, TickRange,
)

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    pool_address TEXT PRIMARY KEY,
    token_id     INTEGER,
    tick_lower   INTEGER NOT NULL,
    tick_upper   INTEGER NOT NULL,
    liquidity    TEXT NOT NULL,
    token0_amt   TEXT NOT NULL,
    token1_amt   TEXT NOT NULL,
    fees_t0      TEXT NOT NULL DEFAULT '0',
    fees_t1      TEXT NOT NULL DEFAULT '0',
    entry_sqrt   TEXT NOT NULL,
    entry_price  TEXT NOT NULL,
    block_entered INTEGER NOT NULL,
    pool_json    TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hedge_state (
    pool_address TEXT PRIMARY KEY,
    instrument   TEXT NOT NULL,
    notional_usd TEXT NOT NULL,
    delta        TEXT NOT NULL,
    target_delta TEXT NOT NULL,
    entry_price  TEXT NOT NULL,
    pnl_usd      TEXT NOT NULL DEFAULT '0',
    funding      TEXT NOT NULL DEFAULT '0',
    updated_at   INTEGER NOT NULL
);
"""


class StateStore:
    def __init__(self, db_path: str | Path = "lp_engine.db") -> None:
        self._path = Path(db_path)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        log.info("state store at %s", self._path)

    def save_position(self, position: PositionState) -> None:
        import time
        pool_json = json.dumps({
            "address":  position.pool.address,
            "token0":   position.pool.token0,
            "token1":   position.pool.token1,
            "fee_bps":  position.pool.fee_bps,
            "protocol": position.pool.protocol.value,
        })
        self._conn.execute("""
            INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            position.pool.address,
            position.token_id,
            position.tick_range.lower,
            position.tick_range.upper,
            str(position.liquidity),
            str(position.token0_amount),
            str(position.token1_amount),
            str(position.fees_earned_token0),
            str(position.fees_earned_token1),
            str(position.entry_sqrt_price),
            str(position.entry_price_usd),
            position.block_entered,
            pool_json,
            int(time.time()),
        ))
        self._conn.commit()

    def save_hedge(self, pool_address: str, hedge: HedgeState) -> None:
        import time
        self._conn.execute("""
            INSERT OR REPLACE INTO hedge_state VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            pool_address,
            hedge.instrument.name,
            str(hedge.notional_usd),
            str(hedge.delta),
            str(hedge.target_delta),
            str(hedge.entry_price),
            str(hedge.pnl_usd),
            str(hedge.funding_accrued),
            int(time.time()),
        ))
        self._conn.commit()

    def delete_position(self, pool_address: str) -> None:
        self._conn.execute("DELETE FROM positions WHERE pool_address = ?", (pool_address,))
        self._conn.execute("DELETE FROM hedge_state WHERE pool_address = ?", (pool_address,))
        self._conn.commit()

    def restore_position(self, pool_address: str) -> PositionState | None:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE pool_address = ?", (pool_address,)
        ).fetchone()
        if row is None:
            return None

        (addr, token_id, tick_lower, tick_upper, liquidity,
         t0_amt, t1_amt, fees_t0, fees_t1,
         entry_sqrt, entry_price, block_entered, pool_json, _) = row

        pool_data = json.loads(pool_json)
        pool = PoolKey(
            address=pool_data["address"],
            token0=pool_data["token0"],
            token1=pool_data["token1"],
            fee_bps=pool_data["fee_bps"],
            protocol=PoolProtocol(pool_data["protocol"]),
        )
        log.info("restored position token_id=%s pool=%s", token_id, addr[:10])
        return PositionState(
            pool=pool,
            tick_range=TickRange(tick_lower, tick_upper),
            liquidity=int(liquidity),
            token0_amount=Decimal(t0_amt),
            token1_amount=Decimal(t1_amt),
            fees_earned_token0=Decimal(fees_t0),
            fees_earned_token1=Decimal(fees_t1),
            entry_sqrt_price=int(entry_sqrt),
            entry_price_usd=Decimal(entry_price),
            block_entered=block_entered,
            token_id=token_id,
        )

    def restore_hedge(self, pool_address: str) -> HedgeState | None:
        row = self._conn.execute(
            "SELECT * FROM hedge_state WHERE pool_address = ?", (pool_address,)
        ).fetchone()
        if row is None:
            return None

        (_, instrument, notional, delta, target_delta,
         entry_price, pnl, funding, _) = row

        return HedgeState(
            instrument=HedgeInstrument[instrument],
            notional_usd=Decimal(notional),
            delta=Decimal(delta),
            target_delta=Decimal(target_delta),
            entry_price=Decimal(entry_price),
            pnl_usd=Decimal(pnl),
            funding_accrued=Decimal(funding),
        )

    def close(self) -> None:
        self._conn.close()
