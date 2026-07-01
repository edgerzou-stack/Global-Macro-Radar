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
            import random
            last_err = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    time.sleep(delay * (2 ** attempt) + random.uniform(0, 1))
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
                from core.clock import clock
                if clock.now().timestamp() - mtime < expire_hours * 3600:
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

async def _fetch_quote_batch(session, url, codes, headers, semaphore):
    async with semaphore:
        secids = ",".join(to_secid(code) for code in codes)
        for attempt in range(3):
            try:
                async with session.get(url, params={"secids": secids, "fields": "f1,f12,f14,f2,f17,f18,f20,f23,f9,f115"}, headers=headers, timeout=10) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    return ((data.get("data") or {}).get("diff") or [])
            except Exception:
                if attempt == 2: return []
                import random
                await asyncio.sleep(2 ** attempt + random.uniform(0, 1))

async def _fetch_quote_snapshot_async(codes):
    url = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(15)
        tasks = []
        for i in range(0, len(codes), 200):
            tasks.append(_fetch_quote_batch(session, url, codes[i : i + 200], headers, sem))
        results = await asyncio.gather(*tasks)
        
        rows = []
        for r in results: rows.extend(r)
        return rows

@disk_cache(expire_hours=2)
@with_retry(max_retries=3, delay=2)
def fetch_quote_snapshot_cached(codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=["股票代码", "股票简称", "最新价", "PE", "PB", "总市值"])
    
    from core.clock import clock
    import datetime
    real_today = datetime.date.today()
    
    if clock.today() < real_today:
        from core.historical_price import get_historical_closes_bulk, a_share_to_yf
        yf_symbols = [a_share_to_yf(c) for c in codes]
        all_prices = {}
        chunk_size = 500
        for i in range(0, len(yf_symbols), chunk_size):
            chunk = yf_symbols[i:i+chunk_size]
            prices = get_historical_closes_bulk(chunk, clock.today().strftime("%Y-%m-%d"))
            all_prices.update(prices)
            
        rows = []
        for code in codes:
            yf_sym = a_share_to_yf(code)
            price = all_prices.get(yf_sym, None)
            if price is not None:
                rows.append({
                    "股票代码": code,
                    "股票简称": f"Hist_{code}",
                    "最新价": price,
                    "今开": price,
                    "昨收": price,
                    "PE": 15.0,
                    "PB": 1.5,
                    "总市值": 1e10
                })
        return pd.DataFrame(rows)

    rows = asyncio.run(_fetch_quote_snapshot_async(codes))
    
    df = pd.DataFrame(rows).rename(
        columns={
            "f1": "decimal_scale",
            "f12": "股票代码", "f14": "股票简称", "f2": "最新价_raw", 
            "f17": "今开_raw", "f18": "昨收_raw",
            "f20": "总市值", "f23": "PB_raw", "f9": "PE_dynamic_raw", "f115": "PE_ttm_raw"
        }
    )
    df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
    
    # Scale price based on decimal_scale (usually f1 specifies the number of decimal places)
    scale = 10 ** pd.to_numeric(df["decimal_scale"], errors="coerce").fillna(0)
    df["最新价"] = pd.to_numeric(df["最新价_raw"], errors="coerce") / scale
    df["今开"] = pd.to_numeric(df["今开_raw"], errors="coerce") / scale
    df["昨收"] = pd.to_numeric(df["昨收_raw"], errors="coerce") / scale
    df["PE"] = pd.to_numeric(df["PE_ttm_raw"], errors="coerce")
    pe_dynamic = pd.to_numeric(df["PE_dynamic_raw"], errors="coerce")
    df["PE"] = df["PE"].where(~df["PE"].isna(), pe_dynamic)
    df["PE"] = df["PE"].where(~((df["PE"].abs() >= 200) & (df["PE"] % 1 == 0)), df["PE"] / 100)
    df["PB"] = pd.to_numeric(df["PB_raw"], errors="coerce")
    df["PB"] = df["PB"].where(~((df["PB"].abs() >= 20) & (df["PB"] % 1 == 0)), df["PB"] / 100)
    df["总市值"] = pd.to_numeric(df["总市值"], errors="coerce")
    return df[["股票代码", "股票简称", "最新价", "今开", "昨收", "PE", "PB", "总市值"]]

import asyncio
import aiohttp

async def _fetch_em_page(session, url, params, page, semaphore):
    async with semaphore:
        p = params.copy()
        p["pageNumber"] = page
        for attempt in range(3):
            try:
                async with session.get(url, params=p, timeout=10) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    return data["result"]["data"] if data and data.get("result") else []
            except Exception:
                if attempt == 2: return []
                import random
                await asyncio.sleep(2 ** attempt + random.uniform(0, 1))

async def _fetch_em_report_async(date, report_name):
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    filter_expr = f"(REPORTDATE='{date[:4]}-{date[4:6]}-{date[6:]}')" if report_name == "RPT_LICO_FN_CPD" else f"""(SECURITY_TYPE_CODE in ("058001001","058001008"))(TRADE_MARKET_CODE!="069001017")\n        (REPORT_DATE='{date[:4]}-{date[4:6]}-{date[6:]}')"""
    params = {
        "sortColumns": "UPDATE_DATE,SECURITY_CODE" if report_name == "RPT_LICO_FN_CPD" else "NOTICE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1", "pageSize": "500", "pageNumber": "1",
        "reportName": report_name, "columns": "ALL", "filter": filter_expr,
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=10) as resp:
            data = await resp.json(content_type=None)
            if not data or not data.get("result"): return []
            pages = data["result"]["pages"]
            first_page = data["result"]["data"]
        
        sem = asyncio.Semaphore(15)
        tasks = [_fetch_em_page(session, url, params, p, sem) for p in range(2, pages + 1)]
        results = await asyncio.gather(*tasks)
        
        all_data = first_page
        for r in results: all_data.extend(r)
        return all_data

@disk_cache(expire_hours=24*30)
@with_retry(max_retries=3, delay=2)
def stock_yjbb_em_cached(date: str) -> pd.DataFrame:
    rows = asyncio.run(_fetch_em_report_async(date, "RPT_LICO_FN_CPD"))
    expected_columns = [
        "股票代码", "股票简称", "每股收益", "营业总收入-营业总收入", "营业总收入-同比增长",
        "营业总收入-季度环比增长", "净利润-净利润", "净利润-同比增长", "净利润-季度环比增长",
        "每股净资产", "净资产收益率", "每股经营现金流量", "销售毛利率", "所处行业", "最新公告日期"
    ]
    if not rows: return pd.DataFrame(columns=expected_columns)
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "SECURITY_CODE": "股票代码", "SECURITY_NAME_ABBR": "股票简称", "BASIC_EPS": "每股收益",
        "TOTAL_OPERATE_INCOME": "营业总收入-营业总收入", "YSTZ": "营业总收入-同比增长",
        "YSHZ": "营业总收入-季度环比增长", "PARENT_NETPROFIT": "净利润-净利润",
        "SJLTZ": "净利润-同比增长", "SJLHZ": "净利润-季度环比增长",
        "BPS": "每股净资产", "WEIGHTAVG_ROE": "净资产收益率", "MGJYXJJE": "每股经营现金流量",
        "XSMLL": "销售毛利率", "PUBLISHNAME": "所处行业", "NOTICE_DATE": "最新公告日期"
    })
    for col in expected_columns:
        if col not in df.columns:
            df[col] = None
    return df

@disk_cache(expire_hours=24*30)
@with_retry(max_retries=3, delay=2)
def stock_zcfz_em_cached(date: str) -> pd.DataFrame:
    rows = asyncio.run(_fetch_em_report_async(date, "RPT_DMSK_FN_BALANCE"))
    expected_columns = [
        "股票代码", "股票简称", "资产-总资产", "资产-总资产同比", "负债-总负债", "负债-总负债同比",
        "资产负债率", "股东权益合计", "公告日期"
    ]
    if not rows: return pd.DataFrame(columns=expected_columns)
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "SECURITY_CODE": "股票代码", "SECURITY_NAME_ABBR": "股票简称",
        "TOTAL_ASSETS": "资产-总资产", "TOTAL_ASSETS_RATIO": "资产-总资产同比",
        "TOTAL_LIABILITIES": "负债-总负债", "TOTAL_LIAB_RATIO": "负债-总负债同比",
        "DEBT_ASSET_RATIO": "资产负债率", "TOTAL_EQUITY": "股东权益合计", "NOTICE_DATE": "公告日期"
    })
    for col in expected_columns:
        if col not in df.columns:
            df[col] = None
    return df
@disk_cache(expire_hours=24*30)
@with_retry(max_retries=3, delay=2)
def stock_dividend_cninfo_cached(symbol: str) -> pd.DataFrame:
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(ak.stock_dividend_cninfo, symbol=symbol)
        try:
            return future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            raise Exception("Timeout fetching dividend data")
@disk_cache(expire_hours=24)
@with_retry(max_retries=3, delay=2)
def stock_info_a_code_name_cached() -> pd.DataFrame:
    return ak.stock_info_a_code_name()
