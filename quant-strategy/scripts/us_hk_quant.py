import yfinance as yf
import pandas as pd
import concurrent.futures
import json
import time
import random
from data_provider import disk_cache
import os
import requests
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class FetchOutcome:
    ticker: str
    status: str
    row: dict = None
    reason: str = ""


LAST_SCREEN_HEALTH = {}
US_HK_FIXTURE_SCHEMA_VERSION = 1
REQUIRED_ACCEPTED_COLUMNS = {
    "股票代码",
    "总市值(亿元)",
    "净利润同比增长率",
    "营业总收入同比增长率",
    "最新单季环比双增",
    "PE",
}


@lru_cache(maxsize=8)
def _load_outcome_fixture_cached(path, mtime_ns):
    del mtime_ns  # Included in the cache key so fixture rewrites are observed.
    try:
        with open(path, "r", encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot load US/HK outcome fixture {path}: {error}") from error
    if not isinstance(fixture, dict):
        raise ValueError("US/HK outcome fixture must be an object")
    if fixture.get("schema_version") != US_HK_FIXTURE_SCHEMA_VERSION:
        raise ValueError("Unsupported US/HK outcome fixture schema_version")
    outcomes = fixture.get("outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("US/HK outcome fixture outcomes must be an object")

    validated = {}
    for ticker, raw in outcomes.items():
        if not isinstance(ticker, str) or not ticker.strip() or not isinstance(raw, dict):
            raise ValueError("US/HK fixture contains an invalid ticker outcome")
        status = raw.get("status")
        if status not in {"accepted", "rejected", "source_error"}:
            raise ValueError(f"US/HK fixture {ticker} has invalid status")
        reason = raw.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError(f"US/HK fixture {ticker} has invalid reason")
        row = raw.get("row")
        if status == "accepted":
            if not isinstance(row, dict):
                raise ValueError(f"US/HK fixture {ticker} accepted row is missing")
            missing = REQUIRED_ACCEPTED_COLUMNS.difference(row)
            if missing:
                raise ValueError(
                    f"US/HK fixture {ticker} accepted row missing {sorted(missing)}"
                )
            if str(row["股票代码"]) != ticker:
                raise ValueError(f"US/HK fixture {ticker} row ticker mismatch")
            row = dict(row)
        elif row is not None:
            raise ValueError(f"US/HK fixture {ticker} non-accepted row must be null")
        validated[ticker] = FetchOutcome(ticker, status, row=row, reason=reason)
    return validated


def load_outcome_fixture(path):
    fixture_path = os.path.abspath(os.fspath(path))
    try:
        mtime_ns = os.stat(fixture_path).st_mtime_ns
    except OSError as error:
        raise ValueError(
            f"Cannot stat US/HK outcome fixture {fixture_path}: {error}"
        ) from error
    return _load_outcome_fixture_cached(fixture_path, mtime_ns)


def _fixture_outcome(ticker_symbol):
    fixture_path = os.environ.get("US_HK_OUTCOME_FIXTURE")
    if not fixture_path:
        return None
    outcomes = load_outcome_fixture(fixture_path)
    return outcomes.get(
        ticker_symbol,
        FetchOutcome(
            ticker_symbol,
            "source_error",
            reason="ticker_missing_from_outcome_fixture",
        ),
    )

# FMP API request cached for 24 hours to preserve the 250 requests/day limit
def _load_env():
    env_path = os.environ.get("RADAR_ENV", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "industry-radar", ".env"))
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except:
        pass


# Cached wrappers for yfinance to avoid redundant network calls
@disk_cache(expire_hours=12)
def fetch_yf_info_cached(ticker_symbol):
    """Fetch and cache yfinance Ticker.info for 12 hours."""
    time.sleep(random.uniform(0.1, 0.3))  # Rate-limit protection
    return yf.Ticker(ticker_symbol).info

@disk_cache(expire_hours=24*30)
def fetch_yf_financials_cached(ticker_symbol):
    """Fetch and cache yfinance Ticker.financials for 30 days."""
    time.sleep(random.uniform(0.1, 0.3))  # Rate-limit protection
    return yf.Ticker(ticker_symbol).financials

@disk_cache(expire_hours=24*30)
def fetch_yf_quarterly_income_stmt_cached(ticker_symbol):
    time.sleep(random.uniform(0.1, 0.3))
    return yf.Ticker(ticker_symbol).quarterly_income_stmt

def fetch_yf_data(ticker_symbol, args):
    fixture = _fixture_outcome(ticker_symbol)
    if fixture is not None:
        return fixture
    max_retries = 3
    for attempt in range(max_retries):
        try:
            info = fetch_yf_info_cached(ticker_symbol)
            if not isinstance(info, dict) or not info:
                raise ValueError("yfinance info is empty")
            if info.get("marketCap") is None or (
                info.get("currentPrice") is None
                and info.get("previousClose") is None
            ):
                raise ValueError("yfinance info is missing market cap or price")
            
            # Calculate some missing fields or rename them
            pe = info.get("trailingPE")
            pb = info.get("priceToBook")
            div_yield = info.get("dividendYield", 0)
            if div_yield is not None:
                div_yield = div_yield * 100  # Convert to percentage
                
            roe = info.get("returnOnEquity")
            if roe is not None:
                roe = roe * 100
                
            # Calculate debt-to-asset ratio (资产负债率) instead of using debtToEquity
            total_debt = info.get("totalDebt")
            total_assets = info.get("totalAssets")
            debt_to_asset = (total_debt / total_assets * 100) if total_debt and total_assets and total_assets > 0 else None
            market_cap = info.get("marketCap")
            
            revenue_growth = info.get("revenueGrowth")
            earnings_growth = info.get("earningsGrowth")
            net_margin = info.get("profitMargins")
            if net_margin is not None:
                net_margin = net_margin * 100
    
            # ---- Early Reject Logic ----
            valuation_val = (pe * (pb - 1) / pb) if pe and pb and pb != 0 else None
            
            # 1. Dividend Pre-check
            pass_div_precheck = False
            if market_cap is not None and market_cap / 1e8 > args.market_cap_min_yi:
                if valuation_val is not None and valuation_val < args.valuation_formula_max:
                    if div_yield is not None and div_yield > args.dividend_yield_min:
                        if net_margin is not None and net_margin > args.avg_net_profit_margin_min:
                            pass_div_precheck = True
                        
            # 2. Growth Pre-check
            pass_gro_precheck = False
            latest_qoq_dual_growth = False
            if market_cap is not None and market_cap / 1e8 > args.market_cap_min_yi:
                if roe is not None and roe > args.growth_roe_min:
                    if net_margin is not None and net_margin > args.avg_net_profit_margin_min:
                        if revenue_growth is not None and revenue_growth > 0:
                                if earnings_growth is not None and earnings_growth > 0:
                                    # YoY passed. Now check QoQ
                                    try:
                                        # Use yfinance for both US and HK stocks for QoQ check to avoid FMP Free Plan limits
                                        stmt = fetch_yf_quarterly_income_stmt_cached(ticker_symbol)
                                        if stmt is not None and not stmt.empty and stmt.shape[1] >= 2:
                                            if 'Total Revenue' in stmt.index and 'Net Income' in stmt.index:
                                                rev = stmt.loc['Total Revenue'].values
                                                net = stmt.loc['Net Income'].values
                                                if len(rev) >= 2 and len(net) >= 2:
                                                    import math
                                                    if not math.isnan(rev[0]) and not math.isnan(rev[1]) and not math.isnan(net[0]) and not math.isnan(net[1]):
                                                        if rev[0] > rev[1] and net[0] > net[1]:
                                                            latest_qoq_dual_growth = True
                                            else:
                                                latest_qoq_dual_growth = False
                                        else:
                                            latest_qoq_dual_growth = False
                                    except Exception as e:
                                        import logging
                                        logging.error(f"Failed to calculate QoQ dual growth for {ticker_symbol}: {e}", exc_info=True)
                                        latest_qoq_dual_growth = False # DO NOT fallback on error! Reject instead.
                                    
                                    if latest_qoq_dual_growth:
                                        pass_gro_precheck = True
                        
            if not pass_div_precheck and not pass_gro_precheck:
                # Skip fetching 3-year financials
                return FetchOutcome(
                    ticker=ticker_symbol,
                    status="rejected",
                    reason="fundamental_precheck",
                )
    
            return FetchOutcome(ticker=ticker_symbol, status="accepted", row={
                "股票代码": ticker_symbol,
                "股票简称": info.get("shortName", ticker_symbol),
                "PE": pe,
                "PB": pb,
                "估值公式值": valuation_val,
                "TTM股息率": div_yield,
                "总市值(亿元)": market_cap / 1e8 if market_cap else None, # USD/HKD to Yi, rough
                "净资产收益率": roe,
                "销售净利率": net_margin,
                "净利润同比增长率": earnings_growth * 100 if earnings_growth else None,
                "营业总收入同比增长率": revenue_growth * 100 if revenue_growth else None,
                "最新单季环比双增": latest_qoq_dual_growth,
                "资产负债率": debt_to_asset,
                "最新价": info.get("currentPrice") or info.get("previousClose"),
                "所处行业": info.get("sector")
            })
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt + random.uniform(0, 1))
            else:
                print(f"Failed to fetch {ticker_symbol} after {max_retries} attempts: {e}")
                return FetchOutcome(
                    ticker=ticker_symbol,
                    status="source_error",
                    reason=f"{type(e).__name__}: {e}",
                )

from tqdm import tqdm

def screen_us_hk(tickers, args, market_type="US"):
    frames = []
    outcomes = []
    tickers = list(dict.fromkeys(str(ticker) for ticker in tickers))
    if tickers:
        max_workers = min(
            len(tickers), int(os.environ.get("US_HK_MAX_WORKERS", "8"))
        )
        deadline = float(os.environ.get("US_HK_STAGE_TIMEOUT_SECONDS", "180"))
        if max_workers <= 0 or deadline <= 0:
            raise ValueError("US/HK worker count and stage timeout must be positive")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        future_to_ticker = {
            executor.submit(fetch_yf_data, ticker, args): ticker for ticker in tickers
        }
        done, pending = concurrent.futures.wait(
            future_to_ticker, timeout=deadline
        )
        for future in tqdm(done, total=len(tickers), desc=f"Scanning {market_type} stocks"):
            try:
                res = future.result()
            except Exception as error:
                outcomes.append(
                    FetchOutcome(
                        future_to_ticker[future],
                        "source_error",
                        reason=f"{type(error).__name__}: {error}",
                    )
                )
                continue
            if isinstance(res, FetchOutcome):
                outcomes.append(res)
                if res.status == "accepted":
                    frames.append(res.row)
            elif isinstance(res, dict):
                # Compatibility for injected test/provider adapters.
                outcomes.append(FetchOutcome("unknown", "accepted", row=res))
                frames.append(res)
            else:
                outcomes.append(
                    FetchOutcome(
                        future_to_ticker[future],
                        "source_error",
                        reason="invalid outcome",
                    )
                )
        for future in pending:
            ticker = future_to_ticker[future]
            future.cancel()
            outcomes.append(FetchOutcome(ticker, "source_error", reason="stage_timeout"))
        # Do not let a stuck provider call hold the screen function forever.
        # Running threads cannot be force-killed, so providers still need their
        # own request timeouts; this bounds the stage decision and fails health.
        executor.shutdown(wait=False, cancel_futures=True)

    attempted = len(tickers)
    evaluated = sum(
        outcome.status in {"accepted", "rejected"} for outcome in outcomes
    )
    source_errors = sum(outcome.status == "source_error" for outcome in outcomes)
    coverage = evaluated / attempted if attempted else 1.0
    health = {
        "market": market_type,
        "attempted": attempted,
        "evaluated": evaluated,
        "accepted": len(frames),
        "rejected": sum(outcome.status == "rejected" for outcome in outcomes),
        "source_errors": source_errors,
        "coverage": coverage,
    }
    LAST_SCREEN_HEALTH[market_type] = health
    print(f"{market_type} data health: {health}")
    minimum_coverage = float(os.environ.get("US_HK_MIN_DATA_COVERAGE", "0.80"))
    if attempted and coverage < minimum_coverage:
        raise ConnectionError(
            f"{market_type} data coverage {coverage:.1%} is below "
            f"the required {minimum_coverage:.1%}"
        )

    if not frames:
        return pd.DataFrame(), pd.DataFrame()
        
    df = pd.DataFrame(frames)
    
    # Dividend Screen (Disabled for US/HK due to high taxes)
    df_div = pd.DataFrame()
    
    # 2. Filter growth
    # [WARNING: USER MANDATED] NEVER remove or relax this strict PEG < 1 constraint.
    mask_gro = (
        df["总市值(亿元)"].notna() & (df["总市值(亿元)"] > args.market_cap_min_yi)
        & df["净利润同比增长率"].notna() 
        & df["营业总收入同比增长率"].notna() 
        & (df["最新单季环比双增"] == True)
        & (df["PE"].isna() | ((df["PE"] < df["净利润同比增长率"]) & (df["PE"] < df["营业总收入同比增长率"])))
    )
    
    df_growth = df[mask_gro].copy()
    
    return df_div, df_growth
