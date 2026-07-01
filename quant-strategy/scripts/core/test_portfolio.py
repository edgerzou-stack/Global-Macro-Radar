import os
import sys

# Add parent dir to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import db_utils
from core.portfolio import PortfolioManager

def test_portfolio_manager():
    print("Testing PortfolioManager and Transaction logic...")
    pm = PortfolioManager(db_utils)
    
    # 1. Backup current db state
    old_port, old_trades = db_utils.load_portfolio_and_trades()
    print(f"Current portfolio strategies count: {len(old_port)}")
    print(f"Current trades count: {len(old_trades)}")
    
    # 2. Mock some target strategy output
    strategy_targets = {
        "dividend_a_stock": ["600519", "000001"], # Suppose we hold these two
        "growth_us_stock": ["AAPL", "MSFT"]
    }
    
    # 3. Mock prices
    current_prices = {
        "600519": {"最新价": 1700.0},
        "000001": {"最新价": 11.5},
        "AAPL": {"最新价": 190.0},
        "MSFT": {"最新价": 420.0}
    }
    
    # 4. Test diff and update
    snapshot_date = "2026-07-01 00:00:00"
    try:
        new_portfolio, new_trades, diff = pm.diff_and_update(strategy_targets, current_prices, snapshot_date)
        print("\n--- Diff Result ---")
        for s in diff:
            if diff[s]['added'] or diff[s]['removed']:
                print(f"Strategy {s}: Added {len(diff[s]['added'])}, Removed {len(diff[s]['removed'])}")
        
        # Verify it wrote to DB
        check_port, check_trades = db_utils.load_portfolio_and_trades()
        print(f"\nNew DB portfolio strategies count: {len(check_port)}")
        print(f"New DB trades count: {len(check_trades)}")
        
    finally:
        # Restore old state to keep DB clean
        print("\nRestoring original DB state...")
        db_utils.update_portfolio_and_trades(old_port, old_trades)
        final_port, final_trades = db_utils.load_portfolio_and_trades()
        print(f"Restored portfolio count: {len(final_port)}")
        assert len(final_port) == len(old_port), "Restore failed!"

if __name__ == "__main__":
    test_portfolio_manager()
