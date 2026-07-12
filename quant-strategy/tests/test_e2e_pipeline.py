import os
import sqlite3
import pytest
import tempfile
import shutil
from unittest.mock import patch
from core.portfolio import PortfolioManager
import json

@pytest.fixture
def temp_db():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "quant_system.db")
    yield db_path
    shutil.rmtree(temp_dir)

def test_e2e_pipeline(temp_db, offline_data_gateway):
    """
    Minimal end-to-end integration test: screen -> portfolio -> calc_nav
    against the frozen DB and a temporary quant_system.db.
    """
    
    with sqlite3.connect(temp_db) as conn:
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
        
        c.execute("INSERT INTO strategy_accounts (strategy_id, total_capital, available_cash) VALUES ('test_strategy', 100000.0, 100000.0)")
        conn.commit()
    
    screen_results = {
        "test_strategy": ["600519.SS", "000001.SZ"]
    }
    
    class DummyDBUtils:
        def __init__(self, db_path):
            self.db_path = db_path
            
        def get_connection(self):
            return sqlite3.connect(self.db_path)
            
        def load_portfolio_and_trades(self):
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT strategy, name_or_code FROM portfolio")
                portfolio = {}
                for strat, code in c.fetchall():
                    if strat not in portfolio:
                        portfolio[strat] = []
                    portfolio[strat].append(code)
                return portfolio, []
                
        def update_portfolio_and_trades(self, new_portfolio, new_trades, snapshot_date, cursor):
            pass

    db_utils = DummyDBUtils(temp_db)
    pm = PortfolioManager(db_utils_module=db_utils)
    
    current_prices = {
        "600519.SS": {"最新价": 1500.0, "昨收": 1490.0, "涨跌幅": 0.67},
        "000001.SZ": {"最新价": 10.0, "昨收": 9.5, "涨跌幅": 5.26}
    }
    
    from core.cash_manager import CashManager
    with patch.object(CashManager, 'allocate', return_value=True):
        new_port, new_trades, diff = pm.diff_and_update(screen_results, current_prices, "2026-07-12")
        
    assert "test_strategy" in diff
    assert len(diff["test_strategy"]["added"]) > 0
