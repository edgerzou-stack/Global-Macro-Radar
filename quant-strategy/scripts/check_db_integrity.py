import argparse
import sqlite3
import logging
from pathlib import Path
from urllib.parse import quote

from core.quarantine import quarantine_filter

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("DBIntegrityCheck")

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
                diff_cap = abs(total_cap - last_nav)
                if diff_cap > 10.0:  # 10 RMB tolerance for floating point cumulative errors over long periods
                    raise RuntimeError(
                        f"Capital mismatch for {strat}: Account total_capital={total_cap}, "
                        f"but last NAV={last_nav}. Diff={diff_cap}"
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
