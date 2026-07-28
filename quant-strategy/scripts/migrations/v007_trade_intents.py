"""Additive v7 schema for tranche-based, market-aware paper execution intents."""

import sqlite3


SCHEMA_VERSION = 7


def apply_v007(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version > SCHEMA_VERSION:
        return

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_intents (
                intent_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                source_run_id TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL CHECK(market IN ('A', 'HK', 'US')),
                currency TEXT NOT NULL CHECK(length(currency) = 3),
                action TEXT NOT NULL CHECK(action IN (
                    'BUY_NEW', 'SELL_ALL', 'ADD_TRANCHE'
                )),
                state TEXT NOT NULL CHECK(state IN (
                    'PENDING', 'FILLED', 'CANCELLED', 'REJECTED'
                )),
                tranche_quantity INTEGER NOT NULL CHECK(tranche_quantity > 0),
                target_rank INTEGER,
                eligible_session TEXT NOT NULL,
                reserved_cash REAL NOT NULL DEFAULT 0 CHECK(reserved_cash >= 0),
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                executed_at TEXT,
                execution_price REAL CHECK(
                    execution_price IS NULL OR execution_price > 0
                ),
                fee_rate REAL CHECK(fee_rate IS NULL OR fee_rate >= 0),
                realized_pnl REAL,
                UNIQUE(source_run_id, strategy_id, symbol, action)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_intents_market_state_session
            ON trade_intents(market, state, eligible_session)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_intents_strategy_state
            ON trade_intents(strategy_id, state, target_rank)
            """
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise
