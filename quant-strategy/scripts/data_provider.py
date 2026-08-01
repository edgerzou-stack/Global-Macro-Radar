import os
import time
import pickle
import hashlib
import logging
from datetime import datetime
import functools

import akshare as ak
import pandas as pd
import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cache")
DEFAULT_EXPIRE_HOURS = 12
LIVE_QUOTE_MODES = {"shadow", "live-shadow", "production"}
QUOTE_CACHE_SCHEMA_VERSION = 2
FINANCIAL_CACHE_SCHEMA_VERSION = 2
logger = logging.getLogger(__name__)


class QuoteSnapshotModeError(RuntimeError):
    """Raised when a quote request conflicts with the explicit run context."""


class FinancialDataFetchError(RuntimeError):
    """Raised when an authoritative financial report cannot be fetched whole."""

def clear_cache():
    if not os.path.exists(CACHE_DIR):
        return
    for f in os.listdir(CACHE_DIR):
        if f.endswith(".pkl"):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
            except Exception as e:
                import logging
                logging.error(f"Failed to remove cache file {f}: {e}")

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

def disk_cache(
    expire_hours=DEFAULT_EXPIRE_HOURS,
    *,
    context_keys=(),
    schema_version=1,
    validator=None,
):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            force_refresh = kwargs.pop("force_refresh", False)
            
            if not os.path.exists(CACHE_DIR):
                os.makedirs(CACHE_DIR, exist_ok=True)

            context = tuple((key, os.environ.get(key)) for key in context_keys)
            if schema_version == 1 and not context:
                # Preserve existing keys for unrelated financial/universe
                # caches; only explicitly versioned adapters invalidate data.
                key_str = f"{func.__name__}_{args}_{kwargs}"
            else:
                key_str = (
                    f"v{schema_version}_{func.__name__}_{args}_{kwargs}_{context}"
                )
            key_hash = hashlib.md5(key_str.encode("utf-8")).hexdigest()
            cache_file = os.path.join(CACHE_DIR, f"{key_hash}.pkl")
            
            if not force_refresh and os.path.exists(cache_file):
                mtime = os.path.getmtime(cache_file)
                from core.clock import clock
                if clock.now().timestamp() - mtime < expire_hours * 3600:
                    try:
                        with open(cache_file, "rb") as f:
                            cached = pickle.load(f)
                        return (
                            validator(cached, *args, **kwargs)
                            if validator
                            else cached
                        )
                    except Exception as e:
                        import logging
                        quarantine_dir = os.path.join(CACHE_DIR, "quarantine")
                        os.makedirs(quarantine_dir, exist_ok=True)
                        quarantine_name = (
                            os.path.basename(cache_file)
                            + f".invalid-{int(time.time())}"
                        )
                        quarantine_path = os.path.join(
                            quarantine_dir, quarantine_name
                        )
                        try:
                            os.replace(cache_file, quarantine_path)
                        except OSError as quarantine_error:
                            logging.error(
                                "Failed to quarantine cache %s: %s",
                                cache_file,
                                quarantine_error,
                            )
                        logging.error(
                            "Rejected cached value %s: %s", cache_file, e
                        )

            result = func(*args, **kwargs)
            if validator:
                result = validator(result, *args, **kwargs)
            
            try:
                if not os.path.exists(CACHE_DIR):
                    os.makedirs(CACHE_DIR, exist_ok=True)
                with open(cache_file, "wb") as f:
                    pickle.dump(result, f)
            except Exception as e:
                import logging
                logging.error(f"Failed to write cache {cache_file}: {e}")
                
            return result
        return wrapper
    return decorator


def _quote_snapshot_mode():
    """Resolve quote routing from run identity, never from calendar-day gaps."""
    pipeline_mode = os.environ.get("PIPELINE_MODE")
    if pipeline_mode in LIVE_QUOTE_MODES:
        effective_date = os.environ.get("PIPELINE_EFFECTIVE_DATE") or os.environ.get(
            "EFFECTIVE_DATE"
        )
        if effective_date:
            from core.market import AShareMarket

            market = AShareMarket()
            market_session = market.get_effective_trading_date()
            try:
                effective_day = datetime.strptime(
                    effective_date, "%Y-%m-%d"
                ).date()
            except ValueError as error:
                raise QuoteSnapshotModeError(
                    f"invalid pipeline effective date: {effective_date!r}"
                ) from error
            current_closed_day = (
                effective_day == market.get_current_time().date()
                and not market.is_trading_date(effective_day)
                and market_session == market.get_latest_completed_trading_date()
            )
            if effective_date != market_session and not current_closed_day:
                raise QuoteSnapshotModeError(
                    f"{pipeline_mode} effective date {effective_date} does not match "
                    f"the A-share market session {market_session}; use an offline "
                    "point-in-time fixture for historical screening"
                )
            if current_closed_day:
                logger.info(
                    "Current A-share closed day %s uses the latest completed "
                    "live snapshot session %s",
                    effective_date,
                    market_session,
                )
        return "live"
    if pipeline_mode == "offline":
        raise QuoteSnapshotModeError(
            "offline screening must use GLOBAL_SCREEN_FIXTURE; live quote adapters "
            "and synthetic historical fundamentals are forbidden"
        )
    if pipeline_mode:
        raise QuoteSnapshotModeError(f"unsupported PIPELINE_MODE: {pipeline_mode!r}")

    # Compatibility for standalone historical callers.  Pipeline runs always
    # export PIPELINE_MODE and therefore never enter this implicit branch.
    from core.clock import clock
    import datetime as datetime_module

    return (
        "historical"
        if clock.today() < datetime_module.date.today()
        else "live"
    )


def _validate_live_quote_snapshot(frame, codes):
    if frame is None or frame.empty:
        raise ValueError("A-share live quote snapshot is empty")
    required = {"股票代码", "最新价", "总市值"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(
            "A-share live quote snapshot is missing columns: " + ", ".join(missing)
        )

    requested = {str(code).zfill(6) for code in codes}
    if not requested:
        return frame
    symbols = frame["股票代码"].astype(str).str.zfill(6)
    prices = pd.to_numeric(frame["最新价"], errors="coerce")
    market_caps = pd.to_numeric(frame["总市值"], errors="coerce")
    valid_symbols = set(
        symbols[(prices > 0) & (market_caps > 0) & symbols.isin(requested)]
    )
    coverage = len(valid_symbols) / len(requested)
    minimum = float(os.environ.get("A_SHARE_QUOTE_MIN_COVERAGE", "0.99"))
    if coverage < minimum:
        raise ValueError(
            f"A-share live quote transport coverage {coverage:.2%} below "
            f"required {minimum:.2%}"
        )
    return frame


def _validate_quote_cache_value(frame, codes):
    if _quote_snapshot_mode() == "live":
        return _validate_live_quote_snapshot(frame, codes)
    return frame

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
            except Exception as e:
                if attempt == 2:
                    import logging
                    logging.error(f"Failed to fetch quote batch: {e}", exc_info=True)
                    return []
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

@disk_cache(
    expire_hours=2,
    context_keys=("PIPELINE_MODE", "PIPELINE_EFFECTIVE_DATE", "EFFECTIVE_DATE"),
    schema_version=QUOTE_CACHE_SCHEMA_VERSION,
    validator=_validate_quote_cache_value,
)
@with_retry(max_retries=3, delay=2)
def fetch_quote_snapshot_cached(codes: list[str]) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=["股票代码", "股票简称", "最新价", "PE", "PB", "总市值"])
    
    quote_mode = _quote_snapshot_mode()

    if quote_mode == "historical":
        from core.clock import clock
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
                    "股票简称": code,
                    "最新价": price,
                    "今开": price,
                    "昨收": price,
                    # Price history cannot supply point-in-time fundamentals.
                    # Missing values must be excluded by downstream filters rather
                    # than replaced with numbers engineered to pass them.
                    "PE": None,
                    "PB": None,
                    "总市值": None,
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
    result = df[["股票代码", "股票简称", "最新价", "今开", "昨收", "PE", "PB", "总市值"]]
    return _validate_live_quote_snapshot(result, codes)

import asyncio
import aiohttp

async def _fetch_em_page(session, url, params, page, semaphore):
    async with semaphore:
        p = params.copy()
        p["pageNumber"] = page
        last_error = None
        for attempt in range(3):
            try:
                timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
                async with session.get(url, params=p, timeout=timeout) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    result = data.get("result") if isinstance(data, dict) else None
                    rows = result.get("data") if isinstance(result, dict) else None
                    if not isinstance(rows, list) or not rows:
                        raise FinancialDataFetchError(
                            f"Eastmoney {params.get('reportName', 'unknown')} page "
                            f"{page} returned no rows despite advertised page count"
                        )
                    return rows
            except Exception as e:
                last_error = e
                if attempt == 2:
                    break
                import random
                await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
        report_name = params.get("reportName", "unknown")
        raise FinancialDataFetchError(
            f"Eastmoney {report_name} page {page} failed after 3 attempts"
        ) from last_error


def _em_report_max_concurrency() -> int:
    raw = os.environ.get("EM_REPORT_MAX_CONCURRENCY", "4")
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("EM_REPORT_MAX_CONCURRENCY must be an integer") from error
    if not 1 <= value <= 8:
        raise ValueError("EM_REPORT_MAX_CONCURRENCY must be between 1 and 8")
    return value

async def _fetch_em_report_async(date, report_name):
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    filter_expr = f"(REPORTDATE='{date[:4]}-{date[4:6]}-{date[6:]}')" if report_name == "RPT_LICO_FN_CPD" else f"""(SECURITY_TYPE_CODE in ("058001001","058001008"))(TRADE_MARKET_CODE!="069001017")\n        (REPORT_DATE='{date[:4]}-{date[4:6]}-{date[6:]}')"""
    params = {
        "sortColumns": "UPDATE_DATE,SECURITY_CODE" if report_name == "RPT_LICO_FN_CPD" else "NOTICE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1", "pageSize": "500", "pageNumber": "1",
        "reportName": report_name, "columns": "ALL", "filter": filter_expr,
    }
    
    async with aiohttp.ClientSession() as session:
        for attempt in range(3):
            try:
                async with session.get(url, params=params, timeout=30) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    if not data or not data.get("result"): return []
                    pages = data["result"]["pages"]
                    first_page = data["result"]["data"]
                    break
            except Exception as e:
                logger.warning(f"Fetch failed on attempt {attempt} for {url}: {e}")
                if attempt == 2:
                    raise FinancialDataFetchError(
                        f"Eastmoney {report_name} initial page failed after 3 attempts"
                    ) from e
                await asyncio.sleep(2 ** attempt)
        
        # Report periods are fetched sequentially by screen_a_share.  Keep the
        # per-report fan-out small as well: the previous 3 x 15 nested
        # concurrency burst caused Eastmoney to time out and silently produced
        # partial financial tables.
        sem = asyncio.Semaphore(_em_report_max_concurrency())
        tasks = [_fetch_em_page(session, url, params, p, sem) for p in range(2, pages + 1)]
        results = await asyncio.gather(*tasks)
        
        all_data = first_page
        for r in results: all_data.extend(r)
        return all_data

@disk_cache(expire_hours=24, schema_version=FINANCIAL_CACHE_SCHEMA_VERSION)
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

@disk_cache(expire_hours=24, schema_version=FINANCIAL_CACHE_SCHEMA_VERSION)
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
@disk_cache(expire_hours=24)
@with_retry(max_retries=3, delay=2)
def stock_dividend_cninfo_cached(symbol: str) -> pd.DataFrame:
    import concurrent.futures
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(ak.stock_dividend_cninfo, symbol=symbol)
    try:
        return future.result(timeout=10)
    except concurrent.futures.TimeoutError:
        raise Exception("Timeout fetching dividend data")
    finally:
        executor.shutdown(wait=False)
@disk_cache(expire_hours=24)
@with_retry(max_retries=3, delay=2)
def stock_info_a_code_name_cached() -> pd.DataFrame:
    return ak.stock_info_a_code_name()

@disk_cache(expire_hours=24)
@with_retry(max_retries=3, delay=2)
def stock_gdhs_cached() -> pd.DataFrame:
    """Fetch shareholder counts for all A-shares."""
    return ak.stock_zh_a_gdhs(symbol="最新")
