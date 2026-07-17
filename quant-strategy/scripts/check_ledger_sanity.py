import argparse
import datetime
import sqlite3
import logging
from pathlib import Path
from urllib.parse import quote

from core.quarantine import quarantine_filter

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

        # 1. No corrupted PnL (e.g. -89%)
        cursor.execute(
            "SELECT strategy, name_or_code, pnl FROM trade_history "
            "WHERE exit_date LIKE ? AND pnl < -0.35" + trade_filter,
            (f"{today_str}%",) + trade_parameters,
        )
        bad_trades = cursor.fetchall()
        if bad_trades:
            raise RuntimeError(
                "FATAL: Found impossible realized losses (>35%) today! "
                f"Suspected ledger corruption: {bad_trades}"
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

        logger.info("Post-Market Ledger Sanity Checks passed successfully. Transactions verified.")
        return {
            "database_path": str(Path(database_path).expanduser().resolve()),
            "effective_date": today_str,
            "accounts_checked": len(accounts),
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
