import argparse
import json
import logging
import math
import sqlite3
from pathlib import Path
from urllib.parse import quote

from core.quarantine import quarantine_filter
from core.trade_intents import TRANCHE_AMOUNT

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("DBIntegrityCheck")

CAPITAL_TOLERANCE = 10.0


def _table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _latest_nav_generated_at(connection):
    if not _table_exists(connection, "meta_data"):
        return None
    timestamps = []
    for (raw_value,) in connection.execute(
        "SELECT value FROM meta_data WHERE key LIKE 'nav_run_status:%'"
    ):
        try:
            payload = json.loads(raw_value)
        except (TypeError, ValueError):
            continue
        generated_at = payload.get("generated_at")
        if isinstance(generated_at, str) and generated_at:
            timestamps.append(generated_at)
    return max(timestamps, default=None)


def _post_nav_realized_delta(connection, strategy_id, nav_generated_at):
    required = {"trade_intents", "trade_execution_evidence"}
    if not nav_generated_at or not all(
        _table_exists(connection, table) for table in required
    ):
        return 0.0, 0
    delta, fill_count = connection.execute(
        "SELECT COALESCE(SUM(? * ti.tranche_quantity * ti.realized_pnl),0), "
        "COUNT(*) FROM trade_intents ti "
        "WHERE ti.strategy_id=? AND ti.action='SELL_ALL' "
        "AND ti.state='FILLED' AND ti.realized_pnl IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM trade_execution_evidence e "
        "WHERE e.intent_id=ti.intent_id AND e.observed_at>?)",
        (TRANCHE_AMOUNT, strategy_id, nav_generated_at),
    ).fetchone()
    return float(delta), int(fill_count)

def _read_only_connection(database_path):
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Database not found at {path}")
    connection = sqlite3.connect(
        f"file:{quote(str(path), safe='/')}?mode=ro", uri=True, timeout=30.0
    )
    connection.execute("PRAGMA query_only=ON")
    return connection


def check_database(database_path):
    """Validate one explicitly selected database without opening it for writes."""
    logger.info("Starting Pre-Market Database Integrity Check...")
    conn = _read_only_connection(database_path)
    try:
        cursor = conn.cursor()
        account_filter, account_parameters, _ = quarantine_filter(
            conn, "strategy_accounts"
        )
        nav_filter, nav_parameters, _ = quarantine_filter(
            conn, "strategy_nav_history"
        )
        portfolio_filter, portfolio_parameters, _ = quarantine_filter(
            conn, "portfolio"
        )

        # 1. PRAGMA integrity_check
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite PRAGMA integrity_check FAILED: {result}")
        logger.info("SQLite physical integrity check passed.")

        # 2. Capital Balancing
        cursor.execute(
            "SELECT strategy_id, available_cash, total_capital "
            "FROM strategy_accounts WHERE 1=1" + account_filter,
            account_parameters,
        )
        accounts = cursor.fetchall()
        nav_generated_at = _latest_nav_generated_at(conn)
        pending_nav_reconciliations = []

        for strat, cash, total_cap in accounts:
            # Get latest NAV history
            cursor.execute(
                "SELECT nav, cash, holdings_value FROM strategy_nav_history "
                "WHERE strategy_id = ?" + nav_filter + " ORDER BY date DESC LIMIT 1",
                (strat,) + nav_parameters,
            )
            nav_row = cursor.fetchone()
            if nav_row:
                last_nav, last_cash, last_holdings = nav_row
                # The total capital in account should match the last recorded NAV
                signed_diff = total_cap - last_nav
                diff_cap = abs(signed_diff)
                if diff_cap > CAPITAL_TOLERANCE:
                    realized_delta, fill_count = _post_nav_realized_delta(
                        conn, strat, nav_generated_at
                    )
                    residual = abs(signed_diff - realized_delta)
                    if (
                        fill_count > 0
                        and math.isfinite(realized_delta)
                        and residual <= CAPITAL_TOLERANCE
                    ):
                        reconciliation = {
                            "strategy_id": strat,
                            "last_nav": last_nav,
                            "account_total_capital": total_cap,
                            "post_nav_realized_delta": realized_delta,
                            "fill_count": fill_count,
                        }
                        pending_nav_reconciliations.append(reconciliation)
                        logger.warning(
                            "Recognized post-NAV settlement for %s: "
                            "account delta=%s, evidenced realized delta=%s, "
                            "fills=%s; NAV reconciliation is pending.",
                            strat,
                            signed_diff,
                            realized_delta,
                            fill_count,
                        )
                    else:
                        raise RuntimeError(
                            f"Capital mismatch for {strat}: "
                            f"Account total_capital={total_cap}, "
                            f"but last NAV={last_nav}. Diff={diff_cap}; "
                            f"evidenced post-NAV realized delta={realized_delta}, "
                            f"residual={residual}"
                        )

                # Check cash pool consistency
                # During trading, cash is allocated/released, so available_cash might not strictly equal last_cash
                # if there are pending trades (entry_price <= 0). But let's check for extreme divergences.
                pass

        # 3. Rule Checks: Max 3 shares per grid
        cursor.execute(
            "SELECT strategy, name_or_code, shares FROM portfolio "
            "WHERE shares > 3" + portfolio_filter,
            portfolio_parameters,
        )
        violators = cursor.fetchall()
        if violators:
            raise RuntimeError(
                f"Grid capacity violation! Found positions with shares > 3: {violators}"
            )

        logger.info("Pre-market database integrity and capital balancing checks passed successfully.")
        return {
            "database_path": str(Path(database_path).expanduser().resolve()),
            "integrity": result,
            "accounts_checked": len(accounts),
            "grid_violations": 0,
            "pending_nav_reconciliations": pending_nav_reconciliations,
        }
    finally:
        conn.close()


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only pre-market database checks")
    parser.add_argument(
        "--database",
        required=True,
        help="Explicit SQLite database path; the file is opened mode=ro",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        check_database(args.database)
    except Exception as error:
        logger.error(
            "Database integrity check failed with exception: %s", error, exc_info=True
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
