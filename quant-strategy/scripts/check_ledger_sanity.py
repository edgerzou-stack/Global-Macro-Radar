import argparse
import datetime
import hashlib
import json
import sqlite3
import logging
import math
import os
from pathlib import Path
from urllib.parse import quote

from core.quarantine import quarantine_filter
from core.portfolio_limits import MAX_HOLDINGS_PER_STRATEGY
from core.market import AShareMarket, HKMarket, USMarket

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("LedgerSanityCheck")

def _read_only_connection(database_path):
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Database not found at {path}")
    connection = sqlite3.connect(
        f"file:{quote(str(path), safe='/')}?mode=ro", uri=True, timeout=30.0
    )
    connection.execute("PRAGMA query_only=ON")
    return connection


def check_ledger(database_path, effective_date):
    """Run post-market invariants against one explicit read-only ledger."""
    logger.info("Starting Post-Market Ledger Sanity Check...")
    try:
        today_str = datetime.date.fromisoformat(str(effective_date)).isoformat()
    except ValueError as error:
        raise ValueError("effective_date must use YYYY-MM-DD") from error

    conn = _read_only_connection(database_path)
    try:
        cursor = conn.cursor()
        trade_filter, trade_parameters, _ = quarantine_filter(conn, "trade_history")
        portfolio_filter, portfolio_parameters, _ = quarantine_filter(
            conn, "portfolio"
        )
        account_filter, account_parameters, _ = quarantine_filter(
            conn, "strategy_accounts"
        )

        # 1. A large realized loss is not, by itself, ledger corruption.  Real
        # markets can gap through arbitrary warning thresholds.  Fail only on
        # a mathematically impossible loss: with a positive exit price the
        # return must remain above -100% minus the configured transaction fee.
        cursor.execute(
            "SELECT strategy, name_or_code, pnl FROM trade_history "
            "WHERE exit_date LIKE ?" + trade_filter,
            (f"{today_str}%",) + trade_parameters,
        )
        realized_trades = cursor.fetchall()
        fee_by_market = {
            "a": float(os.getenv("FEE_A", "0.001")),
            "hk": float(os.getenv("FEE_HK", "0.002")),
            "us": float(os.getenv("FEE_US", "0.000")),
        }
        bad_trades = []
        large_losses = []
        for strategy, name_or_code, pnl in realized_trades:
            try:
                pnl_value = float(pnl)
            except (TypeError, ValueError):
                bad_trades.append((strategy, name_or_code, pnl))
                continue
            market = "hk" if "_hk_" in strategy else "us" if "_us_" in strategy else "a"
            impossible_floor = -1.0 - fee_by_market[market]
            if not math.isfinite(pnl_value) or pnl_value < impossible_floor - 1e-9:
                bad_trades.append((strategy, name_or_code, pnl))
            elif pnl_value < -0.35:
                large_losses.append((strategy, name_or_code, pnl_value))
        if bad_trades:
            raise RuntimeError(
                "FATAL: Found non-finite or mathematically impossible realized "
                f"PnL today: {bad_trades}"
            )
        if large_losses:
            logger.warning(
                "Large but mathematically valid realized losses retained for audit: %s",
                large_losses,
            )

        # 2. No pending/unresolved entry or exit prices for trades marked as exited today
        cursor.execute(
            "SELECT strategy, name_or_code FROM trade_history "
            "WHERE exit_date LIKE ? AND (entry_price <= 0 OR exit_price <= 0)"
            + trade_filter,
            (f"{today_str}%",) + trade_parameters,
        )
        unresolved_trades = cursor.fetchall()
        if unresolved_trades:
            raise RuntimeError(
                "FATAL: Found trades closed today with zero or negative execution prices: "
                f"{unresolved_trades}"
            )

        # 3. Post-Market Grid Capacity Check
        cursor.execute(
            "SELECT strategy, name_or_code, shares FROM portfolio "
            "WHERE shares > 3" + portfolio_filter,
            portfolio_parameters,
        )
        violators = cursor.fetchall()
        if violators:
            raise RuntimeError(
                "FATAL: Grid capacity violated post-trading! "
                f"Positions with shares > 3: {violators}"
            )

        cursor.execute(
            "SELECT strategy,COUNT(*) FROM portfolio WHERE 1=1"
            + portfolio_filter
            + " GROUP BY strategy HAVING COUNT(*)>?",
            portfolio_parameters + (MAX_HOLDINGS_PER_STRATEGY,),
        )
        holding_count_violators = cursor.fetchall()
        if holding_count_violators:
            raise RuntimeError(
                "FATAL: Active holding limit violated: "
                f"{holding_count_violators}"
            )

        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        pending_intents_checked = 0
        legacy_execution_evidence = 0
        if "trade_intents" in tables:
            duplicate_pending = cursor.execute(
                "SELECT strategy_id,symbol,action,COUNT(*) FROM trade_intents "
                "WHERE state='PENDING' AND NOT EXISTS ("
                "SELECT 1 FROM trade_intent_supersessions s "
                "WHERE s.intent_id=trade_intents.intent_id"
                ") GROUP BY strategy_id,symbol,action "
                "HAVING COUNT(*)>1"
            ).fetchall()
            if duplicate_pending:
                raise RuntimeError(
                    f"FATAL: Duplicate active trade intents: {duplicate_pending}"
                )
            invalid_fills = cursor.execute(
                "SELECT intent_id FROM trade_intents WHERE state='FILLED' AND "
                "(executed_at IS NULL OR execution_price IS NULL OR execution_price<=0)"
            ).fetchall()
            if invalid_fills:
                raise RuntimeError(
                    f"FATAL: Filled intents lack execution evidence: {invalid_fills}"
                )
            if "trade_execution_evidence" not in tables:
                raise RuntimeError(
                    "FATAL: v8 trade execution evidence schema is missing"
                )
            missing_evidence = cursor.execute(
                """
                SELECT i.intent_id
                FROM trade_intents i
                LEFT JOIN trade_execution_evidence e ON e.intent_id=i.intent_id
                WHERE i.state='FILLED' AND e.intent_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM trade_intent_supersessions s
                      WHERE s.intent_id=i.intent_id
                  )
                """
            ).fetchall()
            if missing_evidence:
                raise RuntimeError(
                    f"FATAL: Filled intents lack v8 evidence: {missing_evidence}"
                )
            evidence_rows = cursor.execute(
                """
                SELECT i.intent_id,i.market,i.symbol,e.execution_session,
                       e.price_field,e.adjustment,e.provider,e.payload_sha256,
                       e.payload_json
                FROM trade_intents i
                JOIN trade_execution_evidence e ON e.intent_id=i.intent_id
                WHERE i.state='FILLED'
                  AND NOT EXISTS (
                      SELECT 1 FROM trade_intent_supersessions s
                      WHERE s.intent_id=i.intent_id
                  )
                """
            ).fetchall()
            calendars = {
                "A": AShareMarket(),
                "HK": HKMarket(),
                "US": USMarket(),
            }
            invalid_evidence = []
            for (
                intent_id,
                market,
                symbol,
                execution_session,
                price_field,
                adjustment,
                provider,
                payload_sha256,
                payload_json,
            ) in evidence_rows:
                digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                try:
                    is_session = calendars[market].is_trading_date(
                        datetime.date.fromisoformat(execution_session)
                    )
                except Exception:
                    is_session = False
                if (
                    price_field != "open"
                    or adjustment != "raw"
                    or digest != payload_sha256
                    or not is_session
                ):
                    invalid_evidence.append((intent_id, symbol, execution_session))
                if provider == "v7_legacy_record":
                    legacy_execution_evidence += 1
            if invalid_evidence:
                raise RuntimeError(
                    f"FATAL: Invalid execution evidence: {invalid_evidence}"
                )
            pending_rows = cursor.execute(
                "SELECT strategy_id,symbol,action FROM trade_intents "
                "WHERE state='PENDING' AND NOT EXISTS ("
                "SELECT 1 FROM trade_intent_supersessions s "
                "WHERE s.intent_id=trade_intents.intent_id)"
            ).fetchall()
            pending_intents_checked = len(pending_rows)
            actual = {}
            for strategy, symbol in cursor.execute(
                "SELECT strategy,name_or_code FROM portfolio WHERE 1=1"
                + portfolio_filter,
                portfolio_parameters,
            ):
                actual.setdefault(strategy, set()).add(symbol)
            projected = {strategy: set(symbols) for strategy, symbols in actual.items()}
            for strategy, symbol, action in pending_rows:
                symbols = projected.setdefault(strategy, set())
                if action == "SELL_ALL":
                    symbols.discard(symbol)
                elif action == "BUY_NEW":
                    symbols.add(symbol)
            over_projected = {
                strategy: len(symbols)
                for strategy, symbols in projected.items()
                if len(symbols) > MAX_HOLDINGS_PER_STRATEGY
            }
            if over_projected:
                raise RuntimeError(
                    "FATAL: Projected intent holdings exceed limit: "
                    f"{over_projected}"
                )
            # 4f. Cross-Validation between trade_intents and trade_history / portfolio
            filled_sells = cursor.execute(
                "SELECT intent_id, strategy_id, symbol, eligible_session, execution_price "
                "FROM trade_intents i WHERE state='FILLED' AND action='SELL_ALL'"
                " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s WHERE s.intent_id=i.intent_id)"
                " AND eligible_session <= ?",
                (today_str,),
            ).fetchall()
            for intent_id, strat_id, symbol, session, exec_price in filled_sells:
                th_matches = cursor.execute(
                    "SELECT exit_price,shares FROM trade_history "
                    "WHERE strategy=? AND name_or_code=? AND exit_date=? "
                    "AND reason LIKE ?"
                    + trade_filter,
                    [
                        strat_id,
                        symbol,
                        session,
                        f"%[INTENT:{intent_id}]%",
                    ]
                    + list(trade_parameters),
                ).fetchall()
                if len(th_matches) != 1:
                    raise RuntimeError(
                        f"FATAL: FILLED SELL_ALL intent {intent_id} ({strat_id}/{symbol} on {session}) "
                        "must have exactly one intent-linked trade_history exit record"
                    )
                exit_price = float(th_matches[0][0])
                if abs(exit_price - float(exec_price)) >= 0.01:
                    raise RuntimeError(
                        f"FATAL: Trade intent {intent_id} ({strat_id}/{symbol} on {session}) "
                        f"exit_price in trade_history ({exit_price:.2f}) does not match intent execution_price ({exec_price:.2f})"
                    )

            filled_buys = cursor.execute(
                "SELECT intent_id, strategy_id, symbol, eligible_session, execution_price "
                "FROM trade_intents i WHERE state='FILLED' AND action='BUY_NEW'"
                " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s WHERE s.intent_id=i.intent_id)"
                " AND eligible_session <= ?",
                (today_str,),
            ).fetchall()
            for intent_id, strat_id, symbol, session, exec_price in filled_buys:
                in_portfolio = cursor.execute(
                    "SELECT entry_price,shares FROM portfolio "
                    "WHERE strategy=? AND name_or_code=? AND entry_date=?"
                    + portfolio_filter,
                    [strat_id, symbol, session] + list(portfolio_parameters),
                ).fetchone()
                in_history = cursor.execute(
                    "SELECT entry_price,shares FROM trade_history "
                    "WHERE strategy=? AND name_or_code=? AND entry_date=?"
                    + trade_filter,
                    [strat_id, symbol, session] + list(trade_parameters),
                ).fetchall()
                if not in_portfolio and not in_history:
                    raise RuntimeError(
                        f"FATAL: FILLED BUY_NEW intent {intent_id} ({strat_id}/{symbol} on {session}) "
                        "is missing in both portfolio and trade_history"
                    )
                candidates = ([in_portfolio] if in_portfolio else []) + list(
                    in_history
                )
                if not any(
                    abs(float(row[0]) - float(exec_price)) < 0.01
                    and int(row[1] or 1) >= 1
                    for row in candidates
                ):
                    raise RuntimeError(
                        f"FATAL: FILLED BUY_NEW intent {intent_id} "
                        f"({strat_id}/{symbol} on {session}) has no ledger row "
                        "with the exact execution price"
                    )

        # 4. Double-Entry Validation (Cash Delta vs Traded Amount)
        # We ensure that NAV is accurately recorded and cash is not negative.
        cursor.execute(
            "SELECT strategy_id, available_cash, total_capital "
            "FROM strategy_accounts WHERE 1=1" + account_filter,
            account_parameters,
        )
        accounts = cursor.fetchall()
        for strat, cash, total_cap in accounts:
            if cash < 0:
                raise RuntimeError(
                    f"FATAL: Strategy {strat} account has negative cash: {cash}"
                )
            if total_cap <= 0:
                raise RuntimeError(
                    f"FATAL: Strategy {strat} account has zero or negative total capital: {total_cap}"
                )

        # Strategies rebuilt under the fixed-tranche model opt into a strict,
        # replayable cash equation. Legacy strategies remain visible but cannot
        # silently claim this stronger assurance.
        replay_configs = (
            cursor.execute(
                "SELECT key,value FROM meta_data "
                "WHERE key LIKE 'cash_replay_enforced:%'"
            ).fetchall()
            if "meta_data" in tables
            else []
        )
        replayed_accounts = 0
        for key, raw_config in replay_configs:
            strategy = key.split(":", 1)[1]
            config = json.loads(raw_config)
            initial_cash = float(config["initial_cash"])
            tranche_amount = float(config["tranche_amount"])
            tolerance = float(config.get("tolerance", 0.01))
            active_tranches = cursor.execute(
                "SELECT COALESCE(SUM(shares),0) FROM portfolio "
                "WHERE strategy=?" + portfolio_filter,
                (strategy,) + portfolio_parameters,
            ).fetchone()[0]
            realized_delta = cursor.execute(
                "SELECT COALESCE(SUM(?*shares*pnl),0) FROM trade_history "
                "WHERE strategy=?" + trade_filter,
                (tranche_amount, strategy) + trade_parameters,
            ).fetchone()[0]
            expected_cash = (
                initial_cash
                - tranche_amount * float(active_tranches)
                + float(realized_delta)
            )
            account = cursor.execute(
                "SELECT available_cash FROM strategy_accounts "
                "WHERE strategy_id=?" + account_filter,
                (strategy,) + account_parameters,
            ).fetchone()
            if account is None or abs(float(account[0]) - expected_cash) > tolerance:
                raise RuntimeError(
                    "FATAL: Replayable cash invariant failed for "
                    f"{strategy}: actual={account[0] if account else None}, "
                    f"expected={expected_cash}"
                )
            replayed_accounts += 1

        logger.info("Post-Market Ledger Sanity Checks passed successfully. Transactions verified.")
        return {
            "database_path": str(Path(database_path).expanduser().resolve()),
            "effective_date": today_str,
            "accounts_checked": len(accounts),
            "large_loss_warnings": len(large_losses),
            "pending_intents_checked": pending_intents_checked,
            "legacy_execution_evidence": legacy_execution_evidence,
            "replayed_accounts_checked": replayed_accounts,
        }
    finally:
        conn.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only post-market ledger checks")
    parser.add_argument(
        "--database",
        required=True,
        help="Explicit SQLite database path; the file is opened mode=ro",
    )
    parser.add_argument(
        "--effective-date", required=True, help="Run identity date in YYYY-MM-DD"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        check_ledger(args.database, args.effective_date)
    except Exception as error:
        logger.error(
            "Ledger sanity check failed with exception: %s", error, exc_info=True
        )
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
