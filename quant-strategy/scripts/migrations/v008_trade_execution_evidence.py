"""Additive v8 schema for immutable paper-trade execution evidence."""

import hashlib
import json
import sqlite3


SCHEMA_VERSION = 8


def apply_v008(conn: sqlite3.Connection) -> None:
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
            CREATE TABLE IF NOT EXISTS trade_execution_evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL CHECK(market IN ('A', 'HK', 'US')),
                execution_session TEXT NOT NULL,
                price_field TEXT NOT NULL CHECK(price_field='open'),
                adjustment TEXT NOT NULL CHECK(adjustment='raw'),
                provider TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
                payload_json TEXT NOT NULL,
                FOREIGN KEY(intent_id) REFERENCES trade_intents(intent_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_execution_evidence_session
            ON trade_execution_evidence(market, execution_session, symbol)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_intent_supersessions (
                intent_id TEXT PRIMARY KEY,
                rebuild_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                superseded_at TEXT NOT NULL,
                FOREIGN KEY(intent_id) REFERENCES trade_intents(intent_id)
            )
            """
        )
        # Retain pre-v8 fills as explicitly low-assurance audit records. They
        # are not represented as source-verified OHLC evidence, and rebuild
        # tooling may exclude their accounting effects.
        rows = conn.execute(
            """
            SELECT intent_id,symbol,market,eligible_session,execution_price
            FROM trade_intents
            WHERE state='FILLED' AND execution_price>0
              AND NOT EXISTS (
                  SELECT 1 FROM trade_execution_evidence e
                  WHERE e.intent_id=trade_intents.intent_id
              )
            """
        ).fetchall()
        for intent_id, symbol, market, session, price in rows:
            payload = {
                "schema_version": 1,
                "symbol": symbol,
                "market": market,
                "session": session,
                "price_field": "open",
                "adjustment": "raw",
                "provider": "v7_legacy_record",
                "open": float(price),
                "assurance": "record_only_not_source_verified",
            }
            payload_json = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            conn.execute(
                """
                INSERT INTO trade_execution_evidence (
                    intent_id,symbol,market,execution_session,price_field,
                    adjustment,provider,observed_at,payload_sha256,payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    intent_id,
                    symbol,
                    market,
                    session,
                    "open",
                    "raw",
                    "v7_legacy_record",
                    f"{session}T09:30:00",
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    payload_json,
                ),
            )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise
