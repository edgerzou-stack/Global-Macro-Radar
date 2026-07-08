import sqlite3
import json
import os

def get_db_path():
    # Use path relative to __file__ to always point to quant-strategy/quant_system.db
    default_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "quant_system.db")
    return os.environ.get("SQLITE_DB_PATH", default_db)

def init_db():
    conn = sqlite3.connect(get_db_path(), timeout=30.0)
    c = conn.cursor()
    c.execute('PRAGMA journal_mode=WAL;')
    c.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            name_or_code TEXT NOT NULL,
            entry_date TEXT,
            entry_price REAL,
            weight REAL DEFAULT 0.0,
            shares INTEGER DEFAULT 0,
            UNIQUE(strategy, name_or_code)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            name_or_code TEXT NOT NULL,
            entry_date TEXT,
            entry_price REAL,
            exit_date TEXT,
            exit_price REAL,
            pnl REAL,
            reason TEXT,
            weight REAL DEFAULT 0.0,
            shares INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS meta_data (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    # P2.17 & P3.24: 新增 portfolio_snapshots 和 daily_results 拆分表
    c.execute('''
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            strategy TEXT NOT NULL,
            name_or_code TEXT NOT NULL,
            weight REAL DEFAULT 0.0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS strategy_daily_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_date TEXT NOT NULL,
            strategy TEXT NOT NULL,
            result_json TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS strategy_accounts (
            strategy_id TEXT PRIMARY KEY,
            total_capital REAL,
            available_cash REAL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS strategy_nav_history (
            date TEXT,
            strategy_id TEXT,
            nav REAL,
            cash REAL,
            holdings_value REAL,
            PRIMARY KEY(date, strategy_id)
        )
    ''')
    # 尝试更新旧表结构，添加缺少的列 (sqlite 允许 ADD COLUMN)
    try:
        c.execute("ALTER TABLE portfolio ADD COLUMN weight REAL DEFAULT 0.0")
        c.execute("ALTER TABLE portfolio ADD COLUMN shares INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass # Column exists
    try:
        c.execute("ALTER TABLE trade_history ADD COLUMN weight REAL DEFAULT 0.0")
        c.execute("ALTER TABLE trade_history ADD COLUMN shares INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass # Column exists
    conn.commit()
    return conn

def get_connection():
    db_path = get_db_path()
    if not os.path.exists(db_path):
        init_db()
    return sqlite3.connect(db_path, timeout=30.0)

def load_portfolio_and_trades():
    conn = get_connection()
    c = conn.cursor()
    
    c.execute("SELECT strategy, name_or_code, entry_date, entry_price, shares FROM portfolio")
    portfolio = {}
    for row in c.fetchall():
        strategy, name_or_code, entry_date, entry_price, shares = row
        if strategy not in portfolio:
            portfolio[strategy] = {}
        # shares == 0 default in db implies 1 tranche originally
        portfolio[strategy][name_or_code] = {"entry_date": entry_date, "entry_price": entry_price, "shares": max(1, shares)}
        
    c.execute("SELECT strategy, name_or_code, entry_date, entry_price, exit_date, exit_price, pnl, reason FROM trade_history")
    trade_history = []
    for row in c.fetchall():
        strategy, name_or_code, entry_date, entry_price, exit_date, exit_price, pnl, reason = row
        trade_history.append({
            "strategy": strategy, "name": name_or_code, "entry_date": entry_date, 
            "entry_price": entry_price, "exit_date": exit_date, "exit_price": exit_price, "pnl": pnl,
            "reason": reason
        })
        
    conn.close()
    return portfolio, trade_history

def update_portfolio(portfolio_dict):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM portfolio")
    for strat, holdings in portfolio_dict.items():
        for key, info in holdings.items():
            c.execute("INSERT INTO portfolio (strategy, name_or_code, entry_date, entry_price) VALUES (?, ?, ?, ?)",
                      (strat, key, info.get("entry_date"), info.get("entry_price", 0)))
    conn.commit()
    conn.close()

def append_trades(trades_list):
    if not trades_list:
        return
    conn = get_connection()
    c = conn.cursor()
    for t in trades_list:
        # Dedup guard: skip if an identical trade already exists
        c.execute("""SELECT COUNT(*) FROM trade_history 
                     WHERE strategy=? AND name_or_code=? AND entry_date=? AND exit_date=? 
                     AND entry_price=? AND exit_price=?""",
                  (t.get("strategy"), t.get("name"), t.get("entry_date"), t.get("exit_date"), 
                   t.get("entry_price", 0), t.get("exit_price", 0)))
        if c.fetchone()[0] > 0:
            continue
        c.execute("INSERT INTO trade_history (strategy, name_or_code, entry_date, entry_price, exit_date, exit_price, pnl, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (t.get("strategy"), t.get("name"), t.get("entry_date"), t.get("entry_price", 0), t.get("exit_date"), t.get("exit_price", 0), t.get("pnl", 0), t.get("reason", "")))
    conn.commit()
    conn.close()

def update_portfolio_and_trades(portfolio_dict, trades_list, snapshot_date=None):
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN TRANSACTION")
        
        c.execute("DELETE FROM portfolio")
        for strat, holdings in portfolio_dict.items():
            for key, info in holdings.items():
                # P2.18: Save weights and shares
                weight = info.get("weight", 0.0)
                shares = info.get("shares", 0)
                c.execute("INSERT INTO portfolio (strategy, name_or_code, entry_date, entry_price, weight, shares) VALUES (?, ?, ?, ?, ?, ?)",
                          (strat, key, info.get("entry_date"), info.get("entry_price", 0), weight, shares))
                
                # P2.17: Insert portfolio snapshot if date provided
                if snapshot_date:
                    c.execute("INSERT INTO portfolio_snapshots (snapshot_date, strategy, name_or_code, weight) VALUES (?, ?, ?, ?)",
                              (snapshot_date, strat, key, weight))
                          
        if trades_list:
            for t in trades_list:
                weight = t.get("weight", 0.0)
                shares = t.get("shares", 0)
                # Dedup guard: skip if an identical trade already exists
                c.execute("""SELECT COUNT(*) FROM trade_history 
                             WHERE strategy=? AND name_or_code=? AND entry_date=? AND exit_date=? 
                             AND entry_price=? AND exit_price=?""",
                          (t.get("strategy"), t.get("name"), t.get("entry_date"), t.get("exit_date"), 
                           t.get("entry_price", 0), t.get("exit_price", 0)))
                if c.fetchone()[0] > 0:
                    continue
                c.execute("INSERT INTO trade_history (strategy, name_or_code, entry_date, entry_price, exit_date, exit_price, pnl, reason, weight, shares) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (t.get("strategy"), t.get("name"), t.get("entry_date"), t.get("entry_price", 0), t.get("exit_date"), t.get("exit_price", 0), t.get("pnl", 0), t.get("reason", ""), weight, shares))
                          
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Transaction failed, rolling back: {e}")
        raise e
    finally:
        conn.close()

def save_meta_data(key, value_dict):
    conn = get_connection()
    c = conn.cursor()
    c.execute("REPLACE INTO meta_data (key, value) VALUES (?, ?)", (key, json.dumps(value_dict, ensure_ascii=False)))
    conn.commit()
    conn.close()
    
def load_meta_data(key):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM meta_data WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return None

def load_latest_daily_results():
    """
    Reconstructs the payload format previously stored in meta_data by fetching the latest 
    results from the strategy_daily_results table.
    """
    conn = get_connection()
    c = conn.cursor()
    # Find the latest result_date
    c.execute("SELECT MAX(result_date) FROM strategy_daily_results")
    latest_date_row = c.fetchone()
    if not latest_date_row or not latest_date_row[0]:
        conn.close()
        return None
        
    latest_date = latest_date_row[0]
    
    # Fetch all strategies for that date
    c.execute("SELECT strategy, result_json FROM strategy_daily_results WHERE result_date=?", (latest_date,))
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return None
        
    payload = {
        "mode": "global_12_grid",
        "snapshot_date": latest_date,
        "results": {},
        "diff": {},
        "stage_counts": {}
    }
    
    for strategy, json_str in rows:
        try:
            strat_data = json.loads(json_str)
            payload["results"][strategy] = strat_data.get("results", [])
            payload["diff"][strategy] = strat_data.get("diff", {})
            payload["stage_counts"][strategy] = len(payload["results"][strategy])
        except Exception as e:
            print(f"Error parsing json for strategy {strategy}: {e}")
            
    return payload
