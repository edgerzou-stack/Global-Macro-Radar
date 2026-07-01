import os
import time
import pickle
import hashlib
from datetime import datetime
import functools

import akshare as ak
import pandas as pd
import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cache")
DEFAULT_EXPIRE_HOURS = 12

def clear_cache():
    if not os.path.exists(CACHE_DIR):
        return
    for f in os.listdir(CACHE_DIR):
        if f.endswith(".pkl"):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
            except Exception:
                pass

def with_retry(max_retries=3, delay=2):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    time.sleep(delay * (attempt + 1))
            raise last_err
        return wrapper
    return decorator

def disk_cache(expire_hours=DEFAULT_EXPIRE_HOURS):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            force_refresh = kwargs.pop("force_refresh", False)
            
            if not os.path.exists(CACHE_DIR):
                os.makedirs(CACHE_DIR, exist_ok=True)
            
            key_str = f"{func.__name__}_{args}_{kwargs}"
            key_hash = hashlib.md5(key_str.encode("utf-8")).hexdigest()
            cache_file = os.path.join(CACHE_DIR, f"{key_hash}.pkl")
            
            if not force_refresh and os.path.exists(cache_file):
                mtime = os.path.getmtime(cache_file)
                if datetime.now().timestamp() - mtime < expire_hours * 3600:
                    try:
                        with open(cache_file, "rb") as f:
                            return pickle.load(f)
                    except Exception:
                        pass
            
            result = func(*args, **kwargs)
            
            try:
                if not os.path.exists(CACHE_DIR):
                    os.makedirs(CACHE_DIR, exist_ok=True)
                with open(cache_file, "wb") as f:
                    pickle.dump(result, f)
            except Exception:
                pass
                
            return result
        return wrapper
    return decorator

def to_secid(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "1." + code
    return "0." + code

@with_retry(max_retries=3, delay=2)
def fetch_quote_snapshot_cached(codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=["股票代码", "股票简称", "最新价", "PE", "PB", "总市值"])

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    url = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    rows = []
    for i in range(0, len(codes), 200):
        batch = codes[i : i + 200]
        secids = ",".join(to_secid(code) for code in batch)
        resp = session.get(url, params={"secids": secids, "fields": "f12,f14,f2,f20,f23,f9,f115"}, timeout=20)
        resp.raise_for_status()
        rows.extend(((resp.json().get("data") or {}).get("diff") or []))
        time.sleep(0.03)
        
    df = pd.DataFrame(rows).rename(
        columns={
            "f12": "股票代码", "f14": "股票简称", "f2": "最新价_raw", 
            "f20": "总市值", "f23": "PB_raw", "f9": "PE_dynamic_raw", "f115": "PE_ttm_raw"
        }
    )
    df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
    df["最新价"] = pd.to_numeric(df["最新价_raw"], errors="coerce")
    df["最新价"] = df["最新价"].where(~(df["最新价"].notna() & (df["最新价"] % 1 == 0)), df["最新价"] / 100)
    df["PE"] = pd.to_numeric(df["PE_ttm_raw"], errors="coerce")
    pe_dynamic = pd.to_numeric(df["PE_dynamic_raw"], errors="coerce")
    df["PE"] = df["PE"].where(~df["PE"].isna(), pe_dynamic)
    df["PE"] = df["PE"].where(~((df["PE"].abs() >= 200) & (df["PE"] % 1 == 0)), df["PE"] / 100)
    df["PB"] = pd.to_numeric(df["PB_raw"], errors="coerce")
    df["PB"] = df["PB"].where(~((df["PB"].abs() >= 20) & (df["PB"] % 1 == 0)), df["PB"] / 100)
    df["总市值"] = pd.to_numeric(df["总市值"], errors="coerce")
    return df[["股票代码", "股票简称", "最新价", "PE", "PB", "总市值"]]

@disk_cache(expire_hours=12)
@with_retry(max_retries=3, delay=2)
def stock_yjbb_em_cached(date: str) -> pd.DataFrame:
    return ak.stock_yjbb_em(date=date)

@disk_cache(expire_hours=12)
@with_retry(max_retries=3, delay=2)
def stock_zcfz_em_cached(date: str) -> pd.DataFrame:
    return ak.stock_zcfz_em(date=date)

@disk_cache(expire_hours=12)
@with_retry(max_retries=3, delay=2)
def stock_dividend_cninfo_cached(symbol: str) -> pd.DataFrame:
    return ak.stock_dividend_cninfo(symbol=symbol)

@disk_cache(expire_hours=24)
@with_retry(max_retries=3, delay=2)
def stock_info_a_code_name_cached() -> pd.DataFrame:
    return ak.stock_info_a_code_name()
