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
                cash_mgr_inst = CashManager()
                invested_capital = cash_mgr_inst.INITIAL_CAPITAL * cash_mgr_inst.TRANCHE_RATIO * shares
                
                true_multiplier = 1.0
                if ep > 0:
                    try:
                        from core.market import AShareMarket, HKMarket, USMarket
                        if "_us_" in strat:
                            market = USMarket()
                        elif "_hk_" in strat:
                            market = HKMarket()
                        else:
                            market = AShareMarket()

                        entry_date = pos.get("entry_date", str(today))[:10].replace('-', '')
                        end_date_str = market.get_effective_trading_date().replace('-', '')
                        
                        # Fetch ONLY the precise dates we need instead of massive ranges
                        df_adj_entry = data_gateway.get_historical_prices(key_fetch, entry_date, entry_date, adjust="hfq")
                        df_unadj_entry = data_gateway.get_historical_prices(key_fetch, entry_date, entry_date, adjust="")
                        
                        df_adj_exit = data_gateway.get_historical_prices(key_fetch, end_date_str, end_date_str, adjust="hfq")
                        df_unadj_exit = data_gateway.get_historical_prices(key_fetch, end_date_str, end_date_str, adjust="")
                        
                        if not df_adj_entry.empty and not df_unadj_entry.empty and not df_adj_exit.empty and not df_unadj_exit.empty:
                            first_adj = float(df_adj_entry.iloc[0]['收盘'])
                            first_unadj = float(df_unadj_entry.iloc[0]['收盘'])
                            last_adj = float(df_adj_exit.iloc[-1]['收盘'])
                            last_unadj = float(df_unadj_exit.iloc[-1]['收盘'])
                            
                            factor_entry = first_adj / first_unadj if first_unadj > 0 else 1.0
                            factor_exit = last_adj / last_unadj if last_unadj > 0 else 1.0
                            
                            true_adj_ep = ep * factor_entry
                            true_adj_cp = cp * factor_exit
                            
                            if true_adj_ep > 0:
                                true_multiplier = true_adj_cp / true_adj_ep
                        else:
                            true_multiplier = cp / ep
                    except Exception as e:
                        print(f"Failed to calculate true adjusted multiplier for {key}: {e}")
                        true_multiplier = cp / ep
                
                current_value = invested_capital * true_multiplier
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
