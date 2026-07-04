import os
import json
import requests
import pandas as pd
import akshare as ak

def get_hsi_tickers():
    headers = {'User-Agent': 'QuantScreenerBot/1.0 (zouzhengting@example.com)'}
    url = 'https://en.wikipedia.org/wiki/Hang_Seng_Index'
    try:
        html = requests.get(url, headers=headers).text
        from io import StringIO
        tables = pd.read_html(StringIO(html))
        for t in tables:
            if 'Ticker' in t.columns:
                # Format: "SEHK: 5" -> "0005.HK"
                raw = t['Ticker'].astype(str)
                cleaned = []
                for x in raw:
                    num = ''.join(filter(str.isdigit, x))
                    if num:
                        cleaned.append(num.zfill(4) + ".HK")
                return list(set(cleaned))
    except Exception as e:
        print(f"Failed HSI: {e}")
    return []

def get_us_tickers():
    headers = {'User-Agent': 'QuantScreenerBot/1.0 (zouzhengting@example.com)'}
    sp500, ndx = [], []
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        html = requests.get(url, headers=headers).text
        from io import StringIO
        tables = pd.read_html(StringIO(html))
        sp500 = tables[0]['Symbol'].tolist()
    except Exception as e:
        print(f"Failed S&P 500: {e}")
        
    try:
        url = 'https://en.wikipedia.org/wiki/Nasdaq-100'
        html = requests.get(url, headers=headers).text
        from io import StringIO
        tables = pd.read_html(StringIO(html))
        for t in tables:
            if 'Ticker' in t.columns:
                ndx = t['Ticker'].tolist()
                break
    except Exception as e:
        print(f"Failed NDX 100: {e}")
        
    return list(set(sp500 + ndx))

def get_a_tickers():
    try:
        csi300 = ak.index_stock_cons(symbol="000300")['品种代码'].tolist()
        csi500 = ak.index_stock_cons(symbol="000905")['品种代码'].tolist()
        return list(set(csi300 + csi500))
    except Exception as e:
        print(f"Failed A-share components: {e}")
        return []

def main():
    project_dir = "/Users/zouzhengting/Workplace/Global-Macro-Radar-Core/a_share_factor_flow"
    out_path = os.path.join(project_dir, "universes.json")
    backup_path = os.path.join(project_dir, "universes_backup.json")
    
    # Load fallback previous universe if exists
    fallback_data = {"A": [], "US": [], "HK": []}
    if os.path.exists(out_path):
        import shutil
        shutil.copy2(out_path, backup_path)
        try:
            with open(out_path, "r") as f:
                fallback_data = json.load(f)
        except Exception:
            pass
            
    print("Fetching A-share universes...")
    a_tickers = get_a_tickers()
    if len(a_tickers) < 400:
        print(f"WARNING: A-share ticker count too low ({len(a_tickers)}). Using fallback.")
        a_tickers = fallback_data.get("A", [])
    print(f"A-share ticker count: {len(a_tickers)}")
    
    print("Fetching US universes...")
    us_tickers = get_us_tickers()
    if len(us_tickers) < 300:
        print(f"WARNING: US ticker count too low ({len(us_tickers)}). Using fallback.")
        us_tickers = fallback_data.get("US", [])
    print(f"US ticker count: {len(us_tickers)}")
    
    print("Fetching HK universes...")
    hk_tickers = get_hsi_tickers()
    if len(hk_tickers) < 20:
        print(f"WARNING: HK ticker count too low ({len(hk_tickers)}). Using fallback.")
        hk_tickers = fallback_data.get("HK", [])
    print(f"HK ticker count: {len(hk_tickers)}")
    
    data = {
        "A": a_tickers,
        "US": us_tickers,
        "HK": hk_tickers
    }
    
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Universes saved to {out_path}")

if __name__ == "__main__":
    main()
