import os
import sqlite3
import pandas as pd
import yfinance as yf
import db_utils
from core.cash_manager import CashManager
from core.clock import clock
from core.data_gateway import DataGateway
import datetime

data_gateway = DataGateway()

def calc_nav():
    old_portfolio, _ = db_utils.load_portfolio_and_trades()
    
    conn = db_utils.get_connection()
    
    today = clock.today()
    
    try:
        c = conn.cursor()
        c.execute("SELECT strategy_id, available_cash FROM strategy_accounts")
        accounts = c.fetchall()
        
        for strat, cash in accounts:
            holdings_value = 0.0
            positions = old_portfolio.get(strat, {})
            
            for key, pos in positions.items():
                ep = pos.get("entry_price", 0)
                shares = pos.get("shares", 1)
                
                # Fetch latest price
                cp = 0.0
                key_fetch = f"{key}.HK" if '_hk_' in strat and not key.upper().endswith('.HK') else key
                try:
                    cp = data_gateway.get_current_price(key_fetch)
                except Exception as e:
                    print(f"Failed to fetch current price for {key} in {strat} during NAV calculation: {e}")
                
                if cp <= 0:
                    cp = ep # Fallback to cost basis if price unavailable
                    
                # Calculate value
                # Note: 'shares' is number of tranches. We use (ep * tranche_size * shares) to find invested amount?
                # Actually, virtual position size is CashManager.TRANCHE_RATIO * INITIAL_CAPITAL.
                # Total invested capital = Tranche Size * shares
                cash_mgr_inst = CashManager()
                invested_capital = cash_mgr_inst.INITIAL_CAPITAL * cash_mgr_inst.TRANCHE_RATIO * shares
                current_value = (cp / ep) * invested_capital if ep > 0 else invested_capital
                holdings_value += current_value
                
            total_nav = cash + holdings_value
            print(f"[NAV Tracker] {strat} - NAV: {total_nav:,.2f} | Cash: {cash:,.2f} | Holdings: {holdings_value:,.2f}")
            
            # Save to db
            c.execute(
                "INSERT OR REPLACE INTO strategy_nav_history (date, strategy_id, nav, cash, holdings_value) VALUES (?, ?, ?, ?, ?)",
                (today, strat, total_nav, cash, holdings_value)
            )
            # Update total_capital in accounts table to sync
            c.execute(
                "UPDATE strategy_accounts SET total_capital = ? WHERE strategy_id = ?",
                (total_nav, strat)
            )
            
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    calc_nav()
