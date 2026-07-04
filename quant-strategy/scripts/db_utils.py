import sqlite3
import json
import os

def get_db_path():
    default_db = os.path.join(os.environ.get("PROJECT_ROOT", "/Users/zouzhengting/Workplace/Global-Macro-Radar-Core/a_share_factor_flow"), "quant_system.db")
    return os.environ.get("SQLITE_DB_PATH", default_db)

def init_db():
    conn = sqlite3.connect(get_db_path())
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            name_or_code TEXT NOT NULL,
            entry_date TEXT,
            entry_price REAL,
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
            reason TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS meta_data (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    return conn

def get_connection():
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return init_db()
    return sqlite3.connect(db_path)

def load_portfolio_and_trades():
    conn = get_connection()
    c = conn.cursor()
    
    c.execute("SELECT strategy, name_or_code, entry_date, entry_price FROM portfolio")
    portfolio = {}
    for row in c.fetchall():
        strategy, name_or_code, entry_date, entry_price = row
        if strategy not in portfolio:
            portfolio[strategy] = {}
        portfolio[strategy][name_or_code] = {"entry_date": entry_date, "entry_price": entry_price}
        
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
        c.execute("INSERT INTO trade_history (strategy, name_or_code, entry_date, entry_price, exit_date, exit_price, pnl, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (t.get("strategy"), t.get("name"), t.get("entry_date"), t.get("entry_price", 0), t.get("exit_date"), t.get("exit_price", 0), t.get("pnl", 0), t.get("reason", "")))
    conn.commit()
    conn.close()

def update_portfolio_and_trades(portfolio_dict, trades_list):
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN TRANSACTION")
        
        c.execute("DELETE FROM portfolio")
        for strat, holdings in portfolio_dict.items():
            for key, info in holdings.items():
                c.execute("INSERT INTO portfolio (strategy, name_or_code, entry_date, entry_price) VALUES (?, ?, ?, ?)",
                          (strat, key, info.get("entry_date"), info.get("entry_price", 0)))
                          
        if trades_list:
            for t in trades_list:
                c.execute("INSERT INTO trade_history (strategy, name_or_code, entry_date, entry_price, exit_date, exit_price, pnl, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (t.get("strategy"), t.get("name"), t.get("entry_date"), t.get("entry_price", 0), t.get("exit_date"), t.get("exit_price", 0), t.get("pnl", 0), t.get("reason", "")))
                          
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
