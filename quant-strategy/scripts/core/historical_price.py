import yfinance as yf
import pandas as pd
import datetime
import os
import pickle
from core.clock import clock

_BULK_CACHE = {}

def get_historical_closes_bulk(symbols: list[str], as_of_date: str) -> dict[str, float]:
    """
    Downloads the closest available close price for a bulk of symbols on or before `as_of_date`.
    symbols: list of standard symbols (e.g. 600000.SS, AAPL, 0700.HK)
    as_of_date: YYYY-MM-DD
    Returns: dict mapping symbol -> close_price
    """
    global _BULK_CACHE
    
    # Generate a cache key based on the sorted symbols
    cache_key = tuple(sorted(symbols))
    
    if cache_key not in _BULK_CACHE:
        # Load from disk if available
        cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
        os.makedirs(cache_dir, exist_ok=True)
        import hashlib
        key_hash = hashlib.md5(str(cache_key).encode()).hexdigest()
        disk_cache_file = os.path.join(cache_dir, f"yf_bulk_{key_hash}.pkl")
        
        if os.path.exists(disk_cache_file):
            try:
                with open(disk_cache_file, "rb") as f:
                    _BULK_CACHE[cache_key] = pickle.load(f)
            except Exception:
                pass
                
        if cache_key not in _BULK_CACHE:
            print(f"Downloading 2 years of history for {len(symbols)} symbols to build local cache...")
            today_date = clock.today()
            end_dt = today_date + datetime.timedelta(days=1)
            start_dt = end_dt - datetime.timedelta(days=800)
            
            try:
                data = yf.download(symbols, start=start_dt.strftime("%Y-%m-%d"), end=end_dt.strftime("%Y-%m-%d"), progress=False)
                if data.empty or "Close" not in data:
                    _BULK_CACHE[cache_key] = pd.DataFrame()
                else:
                    _BULK_CACHE[cache_key] = data["Close"]
                    
                with open(disk_cache_file, "wb") as f:
                    pickle.dump(_BULK_CACHE[cache_key], f)
            except Exception as e:
                print(f"Failed to fetch historical prices: {e}")
                return {}

    df = _BULK_CACHE.get(cache_key, pd.DataFrame())
    if df.empty:
        return {}
        
    target_dt = pd.to_datetime(as_of_date)
    # Filter up to as_of_date
    df_past = df[df.index <= target_dt]
    if df_past.empty:
        return {}
        
    closes = df_past.ffill().iloc[-1]
    
    result = {}
    if isinstance(closes, pd.Series):
        for sym, price in closes.items():
            if pd.notna(price):
                result[sym] = float(price)
    else:
        if pd.notna(closes):
            result[symbols[0]] = float(closes)
            
    return result

def a_share_to_yf(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"{code}.SS"
    return f"{code}.SZ"

def yf_to_a_share(yf_sym: str) -> str:
    return yf_sym.split(".")[0]
