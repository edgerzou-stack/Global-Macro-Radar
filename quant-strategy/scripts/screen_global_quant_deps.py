import json
import os
import yfinance as yf
from screen_a_share import fetch_quote_snapshot_cached

STRATEGIES = [
    "dividend_a_stock", "growth_a_stock",
    "dividend_us_stock", "growth_us_stock",
    "dividend_hk_stock", "growth_hk_stock",
    "hot_spot_a_stock", "hot_spot_a_etf",
    "hot_spot_us_stock", "hot_spot_us_etf",
    "hot_spot_hk_stock", "hot_spot_hk_etf"
]

def load_universes():
    project_root = os.environ.get("PROJECT_ROOT", "/Users/zouzhengting/Workplace/a_share_factor_flow")
    uni_path = os.path.join(project_root, "universes.json")
    try:
        with open(uni_path, "r") as f:
            data = json.load(f)
            return {
                "US": data.get("US", []),
                "HK": data.get("HK", [])
            }
    except Exception as e:
        print(f"Failed to load universes.json: {e}")
        return {"US": [], "HK": []}

def load_hot_spot_today():
    try:
        project_root = os.environ.get("PROJECT_ROOT", "/Users/zouzhengting/Workplace/a_share_factor_flow")
        with open(os.path.join(project_root, "hot_spot_today.json"), "r") as f:
            return json.load(f)
    except:
        return {}

def get_current_prices_for_portfolio(all_portfolio, a_prices):
    current_prices = {}
    a_codes = []
    us_hk_codes = []
    
    for strat, positions in all_portfolio.items():
        if not positions: continue
        if '_a_' in strat:
            a_codes.extend([c for c in positions.keys() if c.isdigit()])
        else:
            for k in positions.keys():
                yf_sym = f"{k}.HK" if '_hk_' in strat else k
                us_hk_codes.append(yf_sym)
                
    a_codes = list(set(a_codes))
    us_hk_codes = list(set(us_hk_codes))
    
    if a_codes:
        df = fetch_quote_snapshot_cached(a_codes)
        for _, row in df.iterrows():
            current_prices[row["股票代码"]] = {"最新价": row["最新价"]}
            
    if us_hk_codes:
        try:
            import pandas as pd
            import pytz
            from datetime import datetime, timedelta
            now = datetime.now(pytz.utc)
            start_date = now - timedelta(days=7)
            data = yf.download(us_hk_codes, start=start_date.strftime("%Y-%m-%d"), progress=False)
            if not data.empty and "Close" in data:
                last_closes = data["Close"].ffill().iloc[-1]
                if isinstance(last_closes, pd.Series):
                    for sym, price in last_closes.items():
                        base_sym = sym.replace('.HK', '')
                        if pd.notna(price):
                            current_prices[base_sym] = {"最新价": price}
                else:
                    if pd.notna(last_closes):
                        base_sym = us_hk_codes[0].replace('.HK', '')
                        current_prices[base_sym] = {"最新价": last_closes}
        except Exception as e:
            print(f"Failed to fetch YF prices: {e}")
            
    return current_prices
