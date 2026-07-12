import os
import sys
from bs4 import BeautifulSoup
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
html_path = os.path.join(ROOT, "reports", "screening_results.html")
db_path = os.path.join(ROOT, "quant-strategy", "quant_system.db")

def recover():
    if not os.path.exists(html_path):
        print(f"Error: HTML not found at {html_path}")
        sys.exit(1)
        
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
        
    soup = BeautifulSoup(html, 'html.parser')
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    inserted = 0
    strategies = soup.find_all('h3')
    
    strat_map_rev = {
        "A股核心红利精选": "dividend_a_stock",
        "美股核心红利精选": "dividend_us_stock",
        "港股核心红利精选": "dividend_hk_stock",
        "A股高增成长精选": "growth_a_stock",
        "美股高增成长精选": "growth_us_stock",
        "港股高增成长精选": "growth_hk_stock",
        "A股热点突击 (个股)": "hot_spot_a_stock",
        "美股热点突击 (个股)": "hot_spot_us_stock",
        "港股热点突击 (个股)": "hot_spot_hk_stock",
    }
    
    for h3 in strategies:
        strat_title = h3.get_text(strip=True)
        if strat_title not in strat_map_rev:
            continue
        strategy_id = strat_map_rev[strat_title]
        
        next_sibling = h3.find_next_sibling()
        history_table = None
        while next_sibling and next_sibling.name != 'h3':
            if next_sibling.name == 'h4' and '历史平仓交割单明细' in next_sibling.get_text():
                t = next_sibling.find_next_sibling()
                while t and t.name != 'h3' and t.name != 'h4':
                    if t.name == 'table':
                        history_table = t
                        break
                    t = t.find_next_sibling()
                break
            next_sibling = next_sibling.find_next_sibling()
            
        if history_table:
            tbody = history_table.find('tbody')
            if tbody:
                rows = tbody.find_all('tr')
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) >= 7:
                        code_name = cols[0].get_text(strip=True)
                        code = code_name.split(' ')[0]
                        in_date = cols[1].get_text(strip=True)
                        in_price_str = cols[2].get_text(strip=True)
                        out_date = cols[3].get_text(strip=True)
                        out_price_str = cols[4].get_text(strip=True)
                        pnl_str = cols[5].get_text(strip=True).replace('%', '').replace('+', '')
                        reason = cols[6].get_text(strip=True)
                        
                        in_price = float(in_price_str) if in_price_str not in ['N/A', '-', '等待开盘'] else 0.0
                        out_price = float(out_price_str) if out_price_str not in ['N/A', '-', '等待开盘'] else 0.0
                        pnl = float(pnl_str) / 100.0 if pnl_str not in ['N/A', '-'] else 0.0
                        
                        c.execute("""SELECT COUNT(*) FROM trade_history 
                                     WHERE strategy=? AND name_or_code=? AND entry_date=? AND exit_date=? 
                                     AND ABS(entry_price - ?) < 0.01 AND ABS(exit_price - ?) < 0.01""",
                                  (strategy_id, code, in_date, out_date, in_price, out_price))
                        if c.fetchone()[0] == 0:
                            c.execute("""INSERT INTO trade_history 
                                         (strategy, name_or_code, entry_date, entry_price, exit_date, exit_price, pnl, reason, weight, shares) 
                                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                      (strategy_id, code, in_date, in_price, out_date, out_price, pnl, reason, 0.0, 1))
                            inserted += 1

    conn.commit()
    conn.close()
    
    print(f"Total inserted: {inserted} trades.")

if __name__ == "__main__":
    recover()
