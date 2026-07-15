"""Additive v6 schema for an idempotent execution journal.

This migration deliberately does not read, rewrite, or infer values from the
legacy portfolio and trade-history tables. Production migration/cutover is a
separate, explicitly approved operation.
"""

import sqlite3


SCHEMA_VERSION = 6


def apply_v006(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version > SCHEMA_VERSION:
        return

    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                strategy_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                currency TEXT NOT NULL CHECK(length(currency) = 3),
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                state TEXT NOT NULL CHECK(state IN (
                    'PENDING', 'OPEN', 'PARTIALLY_FILLED', 'FILLED',
                    'CANCELLED', 'REJECTED', 'EXPIRED'
                )),
                requested_quantity INTEGER NOT NULL CHECK(requested_quantity > 0),
                filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK(
                    filled_quantity >= 0 AND filled_quantity <= requested_quantity
                ),
                limit_price_minor INTEGER CHECK(limit_price_minor IS NULL OR limit_price_minor > 0),
                initial_reserved_cash_minor INTEGER NOT NULL DEFAULT 0 CHECK(initial_reserved_cash_minor >= 0),
                reserved_cash_minor INTEGER NOT NULL DEFAULT 0 CHECK(reserved_cash_minor >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS journal_transactions (
                transaction_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                reference_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS journal_entries (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT NOT NULL REFERENCES journal_transactions(transaction_id),
                line_no INTEGER NOT NULL CHECK(line_no > 0),
                strategy_id TEXT NOT NULL,
                account TEXT NOT NULL,
                currency TEXT NOT NULL CHECK(length(currency) = 3),
                debit_minor INTEGER NOT NULL DEFAULT 0 CHECK(debit_minor >= 0),
                credit_minor INTEGER NOT NULL DEFAULT 0 CHECK(credit_minor >= 0),
                CHECK(
                    (debit_minor > 0 AND credit_minor = 0)
                    OR (credit_minor > 0 AND debit_minor = 0)
                ),
                UNIQUE(transaction_id, line_no)
            );

            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id),
                idempotency_key TEXT NOT NULL UNIQUE,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                price_minor INTEGER NOT NULL CHECK(price_minor > 0),
                fee_minor INTEGER NOT NULL DEFAULT 0 CHECK(fee_minor >= 0),
                gross_minor INTEGER NOT NULL CHECK(gross_minor > 0),
                executed_at TEXT NOT NULL,
                transaction_id TEXT NOT NULL UNIQUE REFERENCES journal_transactions(transaction_id)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_state_updated
                ON orders(state, updated_at);
            CREATE INDEX IF NOT EXISTS idx_orders_strategy_symbol
                ON orders(strategy_id, symbol);
            CREATE INDEX IF NOT EXISTS idx_fills_order_executed
                ON fills(order_id, executed_at);
            CREATE INDEX IF NOT EXISTS idx_journal_entries_transaction_currency
                ON journal_entries(transaction_id, currency);
            """
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
