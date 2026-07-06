import sqlite3
import akshare as ak
import time

db_path = '/Users/zouzhengting/Workplace/Global-Macro-Radar-Core/a_share_factor_flow/quant_system.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("Fetching A-share mappings...")
name_to_code = {}
for _ in range(5):
    try:
        df1 = ak.stock_zh_a_spot_em()
        if not df1.empty:
            for _, row in df1.iterrows():
                name_to_code[row["名称"]] = row["代码"]
        break
    except Exception as e:
        print(f"Retry stock_zh_a_spot_em: {e}")
        time.sleep(2)

for _ in range(5):
    try:
        df2 = ak.fund_etf_spot_em()
        if not df2.empty:
            for _, row in df2.iterrows():
                name_to_code[row["名称"]] = row["代码"]
        break
    except Exception as e:
        print(f"Retry fund_etf_spot_em: {e}")
        time.sleep(2)

print(f"Got {len(name_to_code)} name-to-code mappings.")

cur.execute("SELECT id, name_or_code FROM portfolio WHERE strategy LIKE '%_a_%'")
port_rows = cur.fetchall()
migrated_port = 0
for pid, name in port_rows:
    if not str(name).isdigit() and name in name_to_code:
        cur.execute("UPDATE portfolio SET name_or_code = ? WHERE id = ?", (name_to_code[name], pid))
        migrated_port += 1
    elif not str(name).isdigit():
        print(f"WARNING: Portfolio entry {name} not found in mappings!")

cur.execute("SELECT id, name_or_code FROM trade_history WHERE strategy LIKE '%_a_%'")
trade_rows = cur.fetchall()
migrated_trade = 0
for tid, name in trade_rows:
    if not str(name).isdigit() and name in name_to_code:
        cur.execute("UPDATE trade_history SET name_or_code = ? WHERE id = ?", (name_to_code[name], tid))
        migrated_trade += 1
    elif not str(name).isdigit():
        print(f"WARNING: Trade entry {name} not found in mappings!")

conn.commit()
print(f"Migrated {migrated_port} portfolio entries and {migrated_trade} trade history entries.")
conn.close()
