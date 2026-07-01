import yfinance as yf
import pandas as pd
from core.clock import clock

def get_historical_closes_bulk(symbols: list[str], as_of_date: str) -> dict[str, float]:
    """
    Downloads the closest available close price for a bulk of symbols on or before `as_of_date`.
    symbols: list of standard symbols (e.g. 600000.SS, AAPL, 0700.HK)
    as_of_date: YYYY-MM-DD
    Returns: dict mapping symbol -> close_price
    """
    # Fetch 10 days prior to as_of_date to ensure we catch a trading day
    import datetime
    end_dt = datetime.datetime.strptime(as_of_date, "%Y-%m-%d") + datetime.timedelta(days=1)
    start_dt = end_dt - datetime.timedelta(days=10)
    
    try:
        data = yf.download(symbols, start=start_dt.strftime("%Y-%m-%d"), end=end_dt.strftime("%Y-%m-%d"), progress=False)
        if data.empty or "Close" not in data:
            return {}
            
        closes = data["Close"].ffill().iloc[-1]
        
        result = {}
        if isinstance(closes, pd.Series):
            for sym, price in closes.items():
                if pd.notna(price):
                    result[sym] = float(price)
        else:
            if pd.notna(closes):
                result[symbols[0]] = float(closes)
                
        return result
    except Exception as e:
        print(f"Failed to fetch historical prices: {e}")
        return {}

def a_share_to_yf(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"{code}.SS"
    return f"{code}.SZ"

def yf_to_a_share(yf_sym: str) -> str:
    return yf_sym.split(".")[0]
