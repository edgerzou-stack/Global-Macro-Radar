import os
import sys
import sqlite3
import datetime

# Setup path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "scripts"))
from core.data_gateway import DataGateway

db_path = os.path.join(ROOT, "quant_system.db")

def repair():
    gateway = DataGateway()
    
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        c = conn.cursor()
        c.execute("SELECT id, strategy, name_or_code, entry_date, exit_date FROM trade_history")
        trades = c.fetchall()
    conn.close()
    
    for row in trades:
        tid, strat, code, entry_date, exit_date = row
        
        # Format code for yfinance if hk/us
        yf_code = code
        if '_hk_' in strat and not code.upper().endswith('.HK'):
            yf_code = f"{code}.HK"
            
        print(f"Fetching adjusted prices for {strat} - {yf_code} from {entry_date} to {exit_date}")
        
        entry_date_str = entry_date[:10].replace('-', '')
        exit_date_str = exit_date[:10].replace('-', '')
        
        entry_dt = datetime.datetime.strptime(entry_date_str, '%Y%m%d')
        exit_dt = datetime.datetime.strptime(exit_date_str, '%Y%m%d')
        
        entry_start = (entry_dt - datetime.timedelta(days=7)).strftime('%Y%m%d')
        exit_start = (exit_dt - datetime.timedelta(days=7)).strftime('%Y%m%d')
        
        # Re-fetch entry
        df_entry = gateway.get_historical_prices(yf_code, start_date=entry_start, end_date=entry_date_str, adjust="hfq")
        # Re-fetch exit
        df_exit = gateway.get_historical_prices(yf_code, start_date=exit_start, end_date=exit_date_str, adjust="hfq")
        
        if not df_entry.empty and not df_exit.empty:
            true_entry = float(df_entry.iloc[-1]['收盘'])
            true_exit = float(df_exit.iloc[-1]['收盘'])
            fee = 0.001 if '_a_' in strat else (0.002 if '_hk_' in strat else 0.000)
            true_pnl = (true_exit / true_entry - 1) - fee
            
            with sqlite3.connect(db_path, timeout=30.0) as conn_upd:
                conn_upd.execute('PRAGMA journal_mode=WAL;')
                c_upd = conn_upd.cursor()
                c_upd.execute("UPDATE trade_history SET entry_price = ?, exit_price = ?, pnl = ? WHERE id = ?",
                          (true_entry, true_exit, true_pnl, tid))
                conn_upd.commit()
            conn_upd.close()
            print(f"  Fixed trade {tid}: entry={true_entry:.2f}, exit={true_exit:.2f}, pnl={true_pnl:.2%}")
        else:
            print(f"  WARNING: Could not fetch valid adjusted data for {yf_code}. Skipping price fix.")
            
    print("\n--- Repairing portfolio ---")
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        c = conn.cursor()
        c.execute("SELECT id, strategy, name_or_code, entry_date, shares FROM portfolio")
        port_rows = c.fetchall()
    conn.close()
    
    for row in port_rows:
        pid, strat, code, entry_date, shares = row
        
        yf_code = code
        if '_hk_' in strat and not code.upper().endswith('.HK'):
            yf_code = f"{code}.HK"
            
        entry_date_str = entry_date[:10].replace('-', '')
        entry_dt = datetime.datetime.strptime(entry_date_str, '%Y%m%d')
        entry_start = (entry_dt - datetime.timedelta(days=7)).strftime('%Y%m%d')
        
        df_entry = gateway.get_historical_prices(yf_code, start_date=entry_start, end_date=entry_date_str, adjust="hfq")
        
        if not df_entry.empty:
            true_entry = float(df_entry.iloc[-1]['收盘'])
            with sqlite3.connect(db_path, timeout=30.0) as conn_upd:
                conn_upd.execute('PRAGMA journal_mode=WAL;')
                c_upd = conn_upd.cursor()
                c_upd.execute("UPDATE portfolio SET entry_price = ? WHERE id = ?", (true_entry, pid))
                conn_upd.commit()
            conn_upd.close()
            print(f"  Fixed portfolio {pid} ({yf_code}): entry={true_entry:.2f}")
            
    # 3. Recalculate Cash Balances
    print("\n--- Recalculating Cash Balances ---")
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        c = conn.cursor()
        c.execute("SELECT strategy_id FROM strategy_accounts")
        strats = [r[0] for r in c.fetchall()]
    conn.close()
    
    INITIAL_CAP = 1000000.0
    TRANCHE_SIZE = INITIAL_CAP * 0.033
    
    for strat in strats:
        cash = INITIAL_CAP
        
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            conn.execute('PRAGMA journal_mode=WAL;')
            c = conn.cursor()
            
            # Replay realized PnL
            c.execute("SELECT pnl, shares FROM trade_history WHERE strategy = ?", (strat,))
            hist_trades = c.fetchall()
            for pnl, shares in hist_trades:
                shares = max(1, shares)  # default is 0 in old db, treat as 1
                invested = TRANCHE_SIZE * shares
                cash += invested * pnl  # pnl is percentage, cash delta is invested * pnl
                
            # Subtract currently invested capital
            c.execute("SELECT shares FROM portfolio WHERE strategy = ?", (strat,))
            open_pos = c.fetchall()
            for row in open_pos:
                shares = max(1, row[0])
                invested = TRANCHE_SIZE * shares
                cash -= invested
        conn.close()
            
        with sqlite3.connect(db_path, timeout=30.0) as conn_upd:
            conn_upd.execute('PRAGMA journal_mode=WAL;')
            c_upd = conn_upd.cursor()
            c_upd.execute("UPDATE strategy_accounts SET available_cash = ? WHERE strategy_id = ?", (cash, strat))
            conn_upd.commit()
        conn_upd.close()
        print(f"  Reset {strat} available_cash to {cash:,.2f}")
        
    print("Database repair complete.")

if __name__ == "__main__":
    repair()
