import sqlite3
import os
from db_utils import get_db_path

def reset_db():
    db_path = get_db_path()
    
    print(f"Connecting to {db_path} to reset state...")
    
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    tables_to_clear = [
        "portfolio",
        "trade_history",
        "strategy_nav_history",
        "portfolio_snapshots",
        "strategy_accounts"
    ]
    
    for table in tables_to_clear:
        try:
            c.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError as e:
            print(f"Table {table} might not exist: {e}")
            
    conn.commit()
    conn.close()
    print("Database reset complete. All historical data, trades, and cash balances have been wiped.")
    print("The CashManager will automatically re-initialize strategy accounts to 1,000,000 upon next run.")

if __name__ == "__main__":
    reset_db()
