import json
import os
import yfinance as yf

CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "ticker_names.json")

def get_stock_name(code):
    try:
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
    except:
        cache = {}
        
    if code in cache:
        return cache[code]
        
    name = code
    if code.isdigit() and len(code) == 6:
        # A-share
        try:
            import akshare as ak
            df = ak.stock_info_a_code_name()
            matched = df[df["证券代码"] == code]
            if not matched.empty:
                name = matched.iloc[0]["证券简称"]
        except:
            pass
    else:
        # US/HK
        try:
            info = yf.Ticker(code).info
            name = info.get("shortName", code)
        except:
            pass
            
    cache[code] = name
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, ensure_ascii=False)
    except:
        pass
    return name
