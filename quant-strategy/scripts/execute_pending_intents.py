#!/usr/bin/env python3
"""Settle due paper-trade intents at verified exact-session raw opens."""

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

from core.data_gateway import DataGateway
from core.clock import clock
from core.market import AShareMarket, HKMarket, USMarket
from core.trade_intents import TradeIntentLedger


MARKETS = {"A": AShareMarket, "HK": HKMarket, "US": USMarket}
SETTLEMENT_RUN_STATUS_PREFIX = "settlement_run_status:"


def _database_environment(connection):
    row = connection.execute(
        "SELECT value FROM meta_data WHERE key='database_environment'"
    ).fetchone()
    return row[0] if row else None


def _run_id(session):
    return (
        os.environ.get("PIPELINE_RUN_ID")
        or os.environ.get("RUN_ID")
        or f"manual-{session}"
    )


def _pending_intent_count(connection, market):
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "trade_intents" not in tables:
        return 0
    supersession_filter = (
        " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
        "WHERE s.intent_id=trade_intents.intent_id)"
        if "trade_intent_supersessions" in tables
        else ""
    )
    return connection.execute(
        "SELECT COUNT(*) FROM trade_intents "
        "WHERE market=? AND state='PENDING'" + supersession_filter,
        (market,),
    ).fetchone()[0]


def _not_yet_due_intent_count(connection, market, cutoff_date):
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "trade_intents" not in tables:
        return 0
    supersession_filter = (
        " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
        "WHERE s.intent_id=trade_intents.intent_id)"
        if "trade_intent_supersessions" in tables
        else ""
    )
    return connection.execute(
        "SELECT COUNT(*) FROM trade_intents "
        "WHERE market=? AND state='PENDING' AND eligible_session>?"
        + supersession_filter,
        (market, cutoff_date),
    ).fetchone()[0]


def _active_due_intents(connection, market, cutoff_date):
    return {
        row["intent_id"]: dict(row)
        for row in connection.execute(
            "SELECT intent_id,strategy_id,symbol,action,eligible_session "
            "FROM trade_intents WHERE market=? AND state='PENDING' "
            "AND eligible_session<=? AND NOT EXISTS ("
            "SELECT 1 FROM trade_intent_supersessions s "
            "WHERE s.intent_id=trade_intents.intent_id)",
            (market, cutoff_date),
        )
    }


def _strategy_breakdown(connection, market, cutoff_date, before_due, market_result):
    after = {}
    if before_due:
        placeholders = ",".join("?" for _ in before_due)
        after = {
            row["intent_id"]: dict(row)
            for row in connection.execute(
                "SELECT intent_id,state,eligible_session,execution_price "
                f"FROM trade_intents WHERE intent_id IN ({placeholders})",
                tuple(before_due),
            )
        }

    deferred_ids = {
        str(item.get("intent_id"))
        for item in market_result.get("deferred", [])
        if item.get("intent_id")
    }
    filled = []
    blocked = []
    rescheduled = []
    cancelled = []
    for intent_id, original in before_due.items():
        current = after.get(intent_id, {})
        state = current.get("state")
        detail = {
            **original,
            "execution_price": current.get("execution_price"),
        }
        if state == "FILLED":
            filled.append(detail)
        elif state == "CANCELLED":
            cancelled.append(detail)
        elif state == "PENDING" and intent_id in deferred_ids:
            continue
        elif (
            state == "PENDING"
            and str(current.get("eligible_session") or "")
            > str(original["eligible_session"])
        ):
            detail["rescheduled_session"] = current["eligible_session"]
            rescheduled.append(detail)
        elif state == "PENDING":
            blocked.append(detail)

    strategies = {}

    def bucket(strategy_id):
        return strategies.setdefault(
            str(strategy_id),
            {
                "filled_by_action": {},
                "pending_by_action": {},
                "blocked": 0,
                "deferred": 0,
                "rescheduled": 0,
                "cancelled": 0,
            },
        )

    for detail in filled:
        target = bucket(detail["strategy_id"])["filled_by_action"]
        action = str(detail["action"])
        target[action] = int(target.get(action, 0)) + 1
    for detail in blocked:
        bucket(detail["strategy_id"])["blocked"] += 1
    for detail in rescheduled:
        bucket(detail["strategy_id"])["rescheduled"] += 1
    for detail in cancelled:
        bucket(detail["strategy_id"])["cancelled"] += 1
    for detail in market_result.get("deferred", []):
        bucket(detail["strategy_id"])["deferred"] += 1

    for strategy_id, action, count in connection.execute(
        "SELECT strategy_id,action,COUNT(*) FROM trade_intents "
        "WHERE market=? AND state='PENDING' AND NOT EXISTS ("
        "SELECT 1 FROM trade_intent_supersessions s "
        "WHERE s.intent_id=trade_intents.intent_id) "
        "GROUP BY strategy_id,action",
        (market,),
    ):
        bucket(strategy_id)["pending_by_action"][str(action)] = int(count)

    if len(filled) != int(market_result.get("filled", 0)):
        raise RuntimeError(
            f"{market} settlement filled-detail count does not match aggregate"
        )
    if len(blocked) != int(market_result.get("blocked", 0)):
        raise RuntimeError(
            f"{market} settlement blocked-detail count does not match aggregate"
        )
    return {
        "strategies": strategies,
        "events": {
            "filled": filled,
            "blocked": blocked,
            "deferred": list(market_result.get("deferred", [])),
            "rescheduled": rescheduled,
            "cancelled": cancelled,
        },
    }


def _persist_run_status(connection, result):
    run_id = result["run_id"]
    connection.execute(
        "REPLACE INTO meta_data (key,value) VALUES (?,?)",
        (
            SETTLEMENT_RUN_STATUS_PREFIX + run_id,
            json.dumps(result, ensure_ascii=False, sort_keys=True),
        ),
    )
    connection.commit()


def execute(
    database,
    session_date,
    markets=None,
    gateway=None,
    *,
    allow_production=False,
):
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    session = dt.date.fromisoformat(str(session_date)).isoformat()
    selected = list(markets or MARKETS)
    unknown = sorted(set(selected) - set(MARKETS))
    if unknown:
        raise ValueError(f"Unsupported markets: {unknown}")

    connection = sqlite3.connect(path, timeout=30.0)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        environment = _database_environment(connection)
        if environment == "production":
            from db_utils import get_production_db_path

            if not allow_production:
                raise RuntimeError(
                    "Production intent settlement requires the explicit "
                    "--allow-production acknowledgement"
                )
            if path != Path(get_production_db_path()).resolve():
                raise RuntimeError(
                    "Production intent settlement requires the canonical database"
                )
        elif environment not in {"test", "backtest"}:
            raise RuntimeError(
                f"Unsupported or unlabelled database environment: {environment!r}"
            )
        ledger = TradeIntentLedger(connection)
        data = gateway or DataGateway()
        connection.row_factory = sqlite3.Row

        def load_price(symbol, _market, eligible_session):
            try:
                quote_loader = getattr(data, "get_exact_open_quote", None)
                if quote_loader is not None:
                    return quote_loader(symbol, eligible_session)
                return data.get_exact_open_price(symbol, eligible_session)
            except Exception as error:
                print(
                    f"OPEN_DEFERRED {symbol}/{eligible_session}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return None

        results = {}
        observed_at = clock.now(dt.timezone.utc)
        for market in selected:
            calendar = MARKETS[market]()
            execution_cutoff = session
            try:
                cutoff_is_trading_session = bool(calendar.is_trading_date(session))
                cutoff_calendar_status = (
                    "trading_session"
                    if cutoff_is_trading_session
                    else "closed_session"
                )
                cutoff_calendar_reason = None
                market_local_date = observed_at.astimezone(
                    calendar.tz
                ).date()
                if dt.date.fromisoformat(session) >= market_local_date:
                    effective_session = calendar.get_effective_trading_date()
                    if effective_session < execution_cutoff:
                        execution_cutoff = effective_session
                        cutoff_calendar_status = (
                            "trading_session_pre_open"
                            if cutoff_is_trading_session
                            else "closed_session_waiting"
                        )
            except Exception as error:
                # The report date is only a settlement cutoff. A failure to
                # classify that date must not suppress historical intents whose
                # immutable eligible sessions can still be proven by the quote
                # gateway.
                cutoff_calendar_status = "calendar_unavailable"
                cutoff_calendar_reason = str(error)
            before_due = _active_due_intents(
                connection,
                market,
                execution_cutoff,
            )
            market_result = ledger.execute_market_session(
                market=market,
                session_date=execution_cutoff,
                price_loader=load_price,
            )
            strategy_breakdown = _strategy_breakdown(
                connection,
                market,
                execution_cutoff,
                before_due,
                market_result,
            )
            total_pending = _pending_intent_count(connection, market)
            not_yet_due = _not_yet_due_intent_count(
                connection,
                market,
                execution_cutoff,
            )
            if market_result["deferred"]:
                status = "degraded_pending_prices"
            elif market_result["blocked"]:
                status = "processed_with_blocked_intents"
            elif total_pending and total_pending == not_yet_due:
                status = "waiting_for_eligible_session"
            else:
                status = "processed"
            results[market] = {
                "status": status,
                **market_result,
                **strategy_breakdown,
                "pending": total_pending,
                "not_yet_due": not_yet_due,
                "cutoff_calendar_status": cutoff_calendar_status,
                "execution_cutoff": execution_cutoff,
            }
            if cutoff_calendar_reason is not None:
                results[market]["cutoff_calendar_reason"] = (
                    cutoff_calendar_reason
                )
        result = {
            "database": str(path),
            "environment": environment,
            "run_id": _run_id(session),
            "session_date": session,
            "generated_at": observed_at.isoformat(),
            "markets": results,
        }
        _persist_run_status(connection, result)
        return result
    finally:
        connection.close()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Settle due trade intents on an isolated ledger copy"
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--session-date", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--market",
        action="append",
        choices=sorted(MARKETS),
        help="Repeat to select markets; defaults to A, HK and US",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help=(
            "Allow the canonical production database. The unified runner only "
            "sets this after its separate production-write acknowledgement."
        ),
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    result = execute(
        args.database,
        args.session_date,
        markets=args.market,
        allow_production=args.allow_production,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
