import pytest
import sqlite3
import os
import tempfile
import shutil
from unittest.mock import MagicMock, patch

from scripts.core.portfolio import PortfolioManager
from scripts.core.cash_manager import CashManager

class DummyDBUtils:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()
        
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS portfolio (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT,
                    name_or_code TEXT,
                    entry_date TEXT,
                    entry_price REAL,
                    shares INTEGER DEFAULT 1
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT,
                    name_or_code TEXT,
                    entry_date TEXT,
                    entry_price REAL,
                    exit_date TEXT,
                    exit_price REAL,
                    pnl REAL,
                    reason TEXT,
                    shares INTEGER DEFAULT 1
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS cash_reserves (
                    strategy TEXT PRIMARY KEY,
                    reserved_cash REAL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS strategy_accounts (
                    strategy_id TEXT PRIMARY KEY,
                    total_capital REAL,
                    available_cash REAL,
                    locked_cash REAL,
                    last_updated TEXT
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT,
                    symbol TEXT,
                    shares INTEGER,
                    avg_cost REAL,
                    current_price REAL,
                    market_value REAL,
                    unrealized_pnl REAL
                )
            """)
            conn.commit()
            
    def get_connection(self):
        return sqlite3.connect(self.db_path)
        
    def load_portfolio_and_trades(self):
        return {}, []

    def update_portfolio_and_trades(self, new_portfolio, new_trades, snapshot_date, cursor):
        pass


def test_limit_down_rejection():
    """
    Simulate a scenario where a stock opens at a limit-down price (open=low=close).
    Verify that the portfolio engine correctly rejects the transaction or penalizes it with max slippage.
    """
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_quant_system.db")
    
    try:
        db_utils = DummyDBUtils(db_path)
        pm = PortfolioManager(db_utils_module=db_utils)
        
        # We need a limit-down scenario for A-shares
        strategy = "test_a_shares"
        symbol = "000001.SZ"
        
        strategy_targets = {
            strategy: [symbol]
        }
        
        # Limit down data: open = low = close = latest
        current_prices = {
            symbol: {
                "最新价": 9.0,
                "开盘": 9.0,
                "最低": 9.0,
                "最高": 9.0,
                "昨收": 10.0,
                "涨跌幅": -10.0
            }
        }
        
        snapshot_date = "2026-07-01 10:00:00"
        
        # Patch the cash manager so we have money
        with patch.object(CashManager, 'allocate', return_value=True):
            with patch('scripts.core.portfolio.clock.now') as mock_now:
                # Mock time to be during market hours (e.g., 10:30 AM Beijing time)
                import datetime
                import pytz
                bjt = pytz.timezone("Asia/Shanghai")
                mock_now.return_value = bjt.localize(datetime.datetime(2026, 7, 1, 10, 30, 0)).astimezone(pytz.utc)
                
                new_portfolio, new_trades, diff = pm.diff_and_update(strategy_targets, current_prices, snapshot_date)
                
                # The transaction should be rejected because of limit-down
                # So diff['test_a_shares']['added'] should be empty, or have a specific reason
                # Or it should be heavily penalized
                
                added = diff[strategy]["added"]
                
                # If it's rejected, added should be empty.
                # If it's penalized with max slippage, entry_price should be heavily penalized
                if len(added) > 0:
                    entry = added[0]
                    # Check if it executed at slippage price instead of prior day's close or limit-down
                    assert entry["entry_price"] == 0.0 or entry["entry_price"] > 9.0, "Should be rejected (0.0) or penalized with max slippage, not executed at limit-down!"
                else:
                    assert len(added) == 0, "Should reject the transaction at limit down."
                        
    finally:
        shutil.rmtree(temp_dir)
