"""Transaction-consistent, read-only data access for report generation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from core.quarantine import quarantine_filter
from db_utils import get_db_path, normalize_db_path, test_strategy_filter


NAV_RUN_STATUS_PREFIX = "nav_run_status:"
SETTLEMENT_RUN_STATUS_PREFIX = "settlement_run_status:"


class ReportRepositoryError(RuntimeError):
    """Raised when persisted report evidence is incomplete or inconsistent."""


def open_readonly_database(database_path=None):
    path = Path(normalize_db_path(database_path or get_db_path()))
    database_uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(database_uri, uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


class ReportSnapshot(NamedTuple):
    run_id: str | None
    generated_at: str
    snapshot_date: str | None
    daily_payload: object
    portfolio: object
    trade_history: tuple
    accounts: tuple
    nav_status: object
    settlement_status: object
    execution_summary: object
    trade_evidence: object

    def mutable_daily_payload(self):
        return _thaw(self.daily_payload)


class TradeEvidenceSnapshot(NamedTuple):
    available: bool
    pending: object
    filled: object
    legacy: tuple


class ReportRepository:
    """Load the core report snapshot from one SQLite read transaction."""

    def __init__(self, database_path=None):
        self.database_path = Path(
            normalize_db_path(database_path or get_db_path())
        )

    def _connect(self):
        return open_readonly_database(self.database_path)

    def _read(self, loader, *args):
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            result = loader(connection, *args)
            connection.rollback()
            return result
        finally:
            connection.close()

    @staticmethod
    def _table_exists(connection, table):
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None

    @classmethod
    def _load_trade_history(cls, connection):
        if not cls._table_exists(connection, "trade_history"):
            return []
        trade_filter, trade_parameters, _ = quarantine_filter(
            connection,
            "trade_history",
        )
        trade_test_filter, trade_test_parameters = test_strategy_filter("strategy")
        trade_rows = connection.execute(
            "SELECT id, strategy, name_or_code, entry_date, entry_price, "
            "exit_date, exit_price, pnl, reason, shares "
            "FROM trade_history WHERE 1=1"
            + trade_filter
            + trade_test_filter,
            trade_parameters + trade_test_parameters,
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "strategy": row["strategy"],
                "name": row["name_or_code"],
                "entry_date": row["entry_date"],
                "entry_price": row["entry_price"],
                "exit_date": row["exit_date"],
                "exit_price": row["exit_price"],
                "pnl": row["pnl"],
                "reason": row["reason"],
                "shares": max(1, row["shares"] or 0),
            }
            for row in trade_rows
        ]

    @classmethod
    def _load_portfolio_and_trades(cls, connection):
        portfolio_filter, portfolio_parameters, _ = quarantine_filter(
            connection,
            "portfolio",
        )
        portfolio_test_filter, portfolio_test_parameters = test_strategy_filter(
            "strategy"
        )
        rows = connection.execute(
            "SELECT id, strategy, name_or_code, entry_date, entry_price, shares "
            "FROM portfolio WHERE 1=1"
            + portfolio_filter
            + portfolio_test_filter,
            portfolio_parameters + portfolio_test_parameters,
        ).fetchall()
        portfolio = {}
        for (
            _row_id,
            strategy,
            name_or_code,
            entry_date,
            entry_price,
            shares,
        ) in rows:
            portfolio.setdefault(strategy, {})[name_or_code] = {
                "entry_date": entry_date,
                "entry_price": entry_price,
                "shares": max(1, shares or 0),
            }
        return portfolio, cls._load_trade_history(connection)

    @staticmethod
    def _load_latest_daily_results(connection):
        result_filter, result_parameters, _ = quarantine_filter(
            connection,
            "strategy_daily_results",
        )
        row = connection.execute(
            "SELECT MAX(result_date) FROM strategy_daily_results WHERE 1=1"
            + result_filter,
            result_parameters,
        ).fetchone()
        if not row or not row[0]:
            return None
        latest_date = row[0]
        rows = connection.execute(
            "SELECT strategy, result_json FROM strategy_daily_results "
            "WHERE result_date=?"
            + result_filter,
            (latest_date,) + result_parameters,
        ).fetchall()
        if not rows:
            return None

        payload = {
            "mode": "global_12_grid",
            "snapshot_date": latest_date,
            "results": {},
            "diff": {},
            "stage_counts": {},
            "appendix": {},
        }
        for strategy, raw_json in rows:
            try:
                strategy_data = json.loads(raw_json)
            except (TypeError, json.JSONDecodeError) as error:
                print(f"Error parsing json for strategy {strategy}: {error}")
                continue
            payload["results"][strategy] = strategy_data.get("results", [])
            payload["diff"][strategy] = strategy_data.get("diff", {})
            payload["stage_counts"][strategy] = len(
                payload["results"][strategy]
            )
            payload["appendix"][strategy] = strategy_data.get("appendix", [])
        return payload

    @staticmethod
    def _load_active_strategy_accounts(connection):
        account_filter, account_parameters, _ = quarantine_filter(
            connection,
            "strategy_accounts",
        )
        return connection.execute(
            "SELECT strategy_id, total_capital, available_cash "
            "FROM strategy_accounts WHERE 1=1"
            + account_filter
            + " ORDER BY strategy_id",
            account_parameters,
        ).fetchall()

    @staticmethod
    def _load_run_status(connection, prefix, run_id):
        if not run_id:
            return None
        row = connection.execute(
            "SELECT value FROM meta_data WHERE key=?",
            (prefix + run_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            raise ValueError(f"Invalid {prefix} payload for run {run_id}")
        return payload

    @staticmethod
    def _load_execution_ledger_summary(connection):
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"orders", "fills"}.issubset(tables):
            return {"available": False, "orders": {}, "fills": {}, "intents": {}}
        orders = dict(
            connection.execute(
                "SELECT strategy_id, COUNT(*) FROM orders GROUP BY strategy_id"
            )
        )
        fills = dict(
            connection.execute(
                "SELECT o.strategy_id, COUNT(*) FROM fills f "
                "JOIN orders o ON o.order_id=f.order_id GROUP BY o.strategy_id"
            )
        )
        intents = {}
        if "trade_intents" in tables:
            supersession_filter = (
                " WHERE NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
                "WHERE s.intent_id=trade_intents.intent_id)"
                if "trade_intent_supersessions" in tables
                else ""
            )
            intents = {
                strategy: {"pending": pending, "filled": filled}
                for strategy, pending, filled in connection.execute(
                    "SELECT strategy_id,"
                    "SUM(CASE WHEN state='PENDING' THEN 1 ELSE 0 END),"
                    "SUM(CASE WHEN state='FILLED' THEN 1 ELSE 0 END) "
                    "FROM trade_intents"
                    + supersession_filter
                    + " GROUP BY strategy_id"
                )
            }
        return {
            "available": True,
            "v7_available": "trade_intents" in tables,
            "orders": orders,
            "fills": fills,
            "intents": intents,
        }

    @staticmethod
    def _load_trade_evidence(connection, trade_history=None):
        if trade_history is None:
            trade_history = ReportRepository._load_trade_history(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "trade_intents" not in tables:
            return TradeEvidenceSnapshot(
                available="trade_history" in tables,
                pending=None,
                filled=None,
                legacy=tuple(dict(row) for row in trade_history),
            )

        has_supersessions = "trade_intent_supersessions" in tables
        supersession_filter = (
            " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
            "WHERE s.intent_id=i.intent_id)"
            if has_supersessions
            else ""
        )
        intent_test_filter, intent_test_parameters = test_strategy_filter(
            "strategy_id"
        )
        pending_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT i.intent_id,i.source_run_id,i.signal_date,i.strategy_id,"
                "i.symbol,i.market,i.action,i.state,i.eligible_session,i.reason "
                "FROM trade_intents i WHERE i.state='PENDING'"
                + supersession_filter
                + intent_test_filter
                + " ORDER BY i.strategy_id,"
                "CASE i.action WHEN 'SELL_ALL' THEN 0 ELSE 1 END,"
                "COALESCE(i.target_rank,0),i.symbol",
                intent_test_parameters,
            )
        ]

        active_filled = connection.execute(
            "SELECT COUNT(*) FROM trade_intents i WHERE i.state='FILLED'"
            + supersession_filter
            + intent_test_filter,
            intent_test_parameters,
        ).fetchone()[0]
        if active_filled and "trade_execution_evidence" not in tables:
            raise ReportRepositoryError(
                "Active FILLED trade intents exist without the v8 execution "
                "evidence table"
            )
        filled_rows = []
        if active_filled:
            rows = connection.execute(
                "SELECT i.intent_id,i.strategy_id,i.symbol,i.market,i.action,"
                "i.tranche_quantity,i.eligible_session,i.execution_price,"
                "i.executed_at,e.execution_session,e.price_field,e.adjustment,"
                "e.provider,e.observed_at,e.payload_sha256,e.payload_json "
                "FROM trade_intents i "
                "LEFT JOIN trade_execution_evidence e ON e.intent_id=i.intent_id "
                "WHERE i.state='FILLED'"
                + supersession_filter
                + intent_test_filter
                + " ORDER BY i.strategy_id,e.execution_session,i.executed_at,"
                "i.intent_id",
                intent_test_parameters,
            ).fetchall()
            if len(rows) != active_filled:
                raise ReportRepositoryError(
                    "Active FILLED trade-intent count changed while building "
                    "the report"
                )
            filled_rows = [dict(row) for row in rows]

        history_by_intent = {}
        for trade in trade_history:
            match = re.search(r"\[INTENT:([^\]]+)\]", str(trade["reason"] or ""))
            if not match:
                continue
            intent_id = match.group(1)
            if intent_id in history_by_intent:
                raise ReportRepositoryError(
                    f"Multiple trade_history rows claim intent {intent_id}"
                )
            history_by_intent[intent_id] = trade

        consumed_history_ids = set()
        validated_filled = []
        for row in filled_rows:
            symbol = str(row["symbol"])
            strategy = str(row["strategy_id"])
            missing_evidence = any(
                row[field] is None
                for field in (
                    "execution_session",
                    "price_field",
                    "adjustment",
                    "provider",
                    "observed_at",
                    "payload_sha256",
                    "payload_json",
                )
            )
            if missing_evidence:
                raise ReportRepositoryError(
                    f"{strategy}/{symbol} FILLED intent {row['intent_id']} "
                    "is missing execution evidence"
                )
            if row["execution_price"] is None or float(row["execution_price"]) <= 0:
                raise ReportRepositoryError(
                    f"{strategy}/{symbol} FILLED intent has no positive "
                    "execution price"
                )
            if row["execution_session"] != row["eligible_session"]:
                raise ReportRepositoryError(
                    f"{strategy}/{symbol} execution session does not match "
                    "eligible session"
                )
            if row["price_field"] != "open" or row["adjustment"] != "raw":
                raise ReportRepositoryError(
                    f"{strategy}/{symbol} execution evidence is not an "
                    "unadjusted open"
                )
            payload_sha256 = str(row["payload_sha256"]).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
                raise ReportRepositoryError(
                    f"{strategy}/{symbol} execution evidence has an invalid "
                    "SHA-256"
                )
            actual_sha256 = hashlib.sha256(
                str(row["payload_json"]).encode("utf-8")
            ).hexdigest()
            if actual_sha256 != payload_sha256:
                raise ReportRepositoryError(
                    f"{strategy}/{symbol} execution evidence SHA-256 mismatch"
                )

            linked_pnl = None
            if row["action"] == "SELL_ALL":
                linked = history_by_intent.get(str(row["intent_id"]))
                if linked is None:
                    raise ReportRepositoryError(
                        f"FILLED SELL_ALL intent {row['intent_id']} has no exact "
                        "trade_history link"
                    )
                if (
                    str(linked["strategy"]) != strategy
                    or str(linked["name"]) != symbol
                    or str(linked["exit_date"]) != str(row["execution_session"])
                    or abs(
                        float(linked["exit_price"])
                        - float(row["execution_price"])
                    )
                    >= 0.01
                ):
                    raise ReportRepositoryError(
                        f"FILLED SELL_ALL intent {row['intent_id']} does not "
                        "match its linked trade_history row"
                    )
                consumed_history_ids.add(int(linked["id"]))
                linked_pnl = linked["pnl"]

            row["payload_sha256"] = payload_sha256
            row["linked_pnl"] = linked_pnl
            validated_filled.append(row)

        legacy = tuple(
            dict(trade)
            for trade in trade_history
            if int(trade["id"]) not in consumed_history_ids
        )
        return TradeEvidenceSnapshot(
            available=True,
            pending=tuple(pending_rows),
            filled=tuple(validated_filled),
            legacy=legacy,
        )

    def load_snapshot(self, *, run_id, generated_at):
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            portfolio, trade_history = self._load_portfolio_and_trades(connection)
            daily_payload = self._load_latest_daily_results(connection)
            accounts = self._load_active_strategy_accounts(connection)
            nav_status = self._load_run_status(
                connection,
                NAV_RUN_STATUS_PREFIX,
                run_id,
            )
            settlement_status = self._load_run_status(
                connection,
                SETTLEMENT_RUN_STATUS_PREFIX,
                run_id,
            )
            execution_summary = self._load_execution_ledger_summary(connection)
            trade_evidence = self._load_trade_evidence(
                connection,
                trade_history,
            )
            connection.rollback()
        finally:
            connection.close()

        snapshot_date = (
            str(daily_payload.get("snapshot_date", ""))[:10]
            if daily_payload
            else None
        )
        return ReportSnapshot(
            run_id=run_id,
            generated_at=str(generated_at),
            snapshot_date=snapshot_date,
            daily_payload=_freeze(daily_payload),
            portfolio=_freeze(portfolio),
            trade_history=tuple(_freeze(item) for item in trade_history),
            accounts=tuple(tuple(row) for row in accounts),
            nav_status=_freeze(nav_status),
            settlement_status=_freeze(settlement_status),
            execution_summary=_freeze(execution_summary),
            trade_evidence=TradeEvidenceSnapshot(
                available=trade_evidence.available,
                pending=(
                    None
                    if trade_evidence.pending is None
                    else tuple(_freeze(row) for row in trade_evidence.pending)
                ),
                filled=(
                    None
                    if trade_evidence.filled is None
                    else tuple(_freeze(row) for row in trade_evidence.filled)
                ),
                legacy=tuple(_freeze(row) for row in trade_evidence.legacy),
            ),
        )

    def load_active_strategy_accounts(self):
        rows = self._read(self._load_active_strategy_accounts)
        return [tuple(row) for row in rows]

    def load_run_status(self, prefix, run_id):
        return self._read(self._load_run_status, prefix, run_id)

    def load_execution_ledger_summary(self):
        return self._read(self._load_execution_ledger_summary)

    def load_trade_evidence(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            evidence = self._load_trade_evidence(connection)
            connection.rollback()
            return TradeEvidenceSnapshot(
                available=evidence.available,
                pending=(
                    None
                    if evidence.pending is None
                    else tuple(_freeze(row) for row in evidence.pending)
                ),
                filled=(
                    None
                    if evidence.filled is None
                    else tuple(_freeze(row) for row in evidence.filled)
                ),
                legacy=tuple(_freeze(row) for row in evidence.legacy),
            )
        finally:
            connection.close()
