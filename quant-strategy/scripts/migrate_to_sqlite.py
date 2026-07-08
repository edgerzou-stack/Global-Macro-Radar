import json
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "quant_system.db")
JSON_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "global_screen.json")

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
            pnl REAL
        )
    ''')
    # Table to store other config/results like "results" array
    c.execute('''
        CREATE TABLE IF NOT EXISTS meta_data (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    return conn

def migrate():
    if not os.path.exists(JSON_PATH):
        print(f"{JSON_PATH} not found.")
        return
        
    with open(JSON_PATH, 'r') as f:
        data = json.load(f)
        
    conn = init_db()
    c = conn.cursor()
    
    # Clear existing data just in case it's a re-run
    c.execute("DELETE FROM portfolio")
    c.execute("DELETE FROM trade_history")
    
    # Migrate portfolio
    port = data.get("portfolio", {})
    for strategy, holdings in port.items():
        for name_or_code, info in holdings.items():
            entry_date = info.get("entry_date", "")
            entry_price = info.get("entry_price", 0.0)
            c.execute("INSERT INTO portfolio (strategy, name_or_code, entry_date, entry_price) VALUES (?, ?, ?, ?)", 
                      (strategy, name_or_code, entry_date, entry_price))
                      
    # Migrate trades
    trades = data.get("trade_history", [])
    for t in trades:
        c.execute("INSERT INTO trade_history (strategy, name_or_code, entry_date, entry_price, exit_date, exit_price, pnl) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (t.get("strategy", ""), t.get("name", ""), t.get("entry_date", ""), t.get("entry_price", 0.0), 
                   t.get("exit_date", ""), t.get("exit_price", 0.0), t.get("pnl", 0.0)))
                   
    # Save the "results" array (the daily ranking results) into meta_data
    if "results" in data:
        c.execute("REPLACE INTO meta_data (key, value) VALUES (?, ?)", 
                  ("daily_results", json.dumps(data["results"], ensure_ascii=False)))
                  
    conn.commit()
    conn.close()
    print("Migration to SQLite successful.")

if __name__ == "__main__":
    migrate()
