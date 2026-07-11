import yfinance as yf
import pandas as pd
import concurrent.futures
import time
import random
from data_provider import disk_cache
import os
import requests

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
    def _fetch(): return yf.Ticker(ticker_symbol).info
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_fetch).result(timeout=20)

@disk_cache(expire_hours=24*30)
def fetch_yf_financials_cached(ticker_symbol):
    """Fetch and cache yfinance Ticker.financials for 30 days."""
    time.sleep(random.uniform(0.1, 0.3))  # Rate-limit protection
    def _fetch(): return yf.Ticker(ticker_symbol).financials
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_fetch).result(timeout=20)

@disk_cache(expire_hours=24*30)
def fetch_yf_quarterly_income_stmt_cached(ticker_symbol):
    time.sleep(random.uniform(0.1, 0.3))
    def _fetch(): return yf.Ticker(ticker_symbol).quarterly_income_stmt
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_fetch).result(timeout=20)

def fetch_yf_data(ticker_symbol, args):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            info = fetch_yf_info_cached(ticker_symbol)
            
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
                            if debt_to_asset is None or debt_to_asset < args.debt_ratio_max:
                                pass_div_precheck = True
                        
            # 2. Growth Pre-check
            pass_gro_precheck = False
            latest_qoq_dual_growth = False
            if market_cap is not None and market_cap / 1e8 > args.market_cap_min_yi:
                if roe is not None and roe > args.growth_roe_min:
                    if net_margin is not None and net_margin > args.avg_net_profit_margin_min:
                        if debt_to_asset is None or debt_to_asset < args.debt_ratio_max:
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
                                                latest_qoq_dual_growth = True # Fallback if missing rows
                                        else:
                                            latest_qoq_dual_growth = True # Fallback if missing stmt
                                    except Exception:
                                        latest_qoq_dual_growth = True # Fallback on error
                                    
                                    if latest_qoq_dual_growth:
                                        pass_gro_precheck = True
                        
            if not pass_div_precheck and not pass_gro_precheck:
                # Skip fetching 3-year financials
                return None
    
            return {
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
                "最新价": info.get("currentPrice", info.get("previousClose")),
                "所处行业": info.get("sector")
            }
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt + random.uniform(0, 1))
            else:
                print(f"Failed to fetch {ticker_symbol} after {max_retries} attempts: {e}")
                return None

from tqdm import tqdm

def screen_us_hk(tickers, args, market_type="US"):
    frames = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_yf_data, t, args) for t in tickers]
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc=f"Scanning {market_type} stocks"):
            try:
                res = future.result(timeout=60) # Increased timeout to 60s
            except concurrent.futures.TimeoutError:
                print(f"Timeout occurred while waiting for yfinance fetch.")
                continue
            if res is not None:
                frames.append(res)
    
    if not frames:
        if tickers:
            raise ConnectionError(f"CRITICAL: All data fetching failed for {market_type}. Aborting pipeline to prevent empty portfolio.")
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
        & (df["资产负债率"].isna() | (df["资产负债率"] < args.debt_ratio_max))
    )
    
    df_growth = df[mask_gro].copy()
    
    return df_div, df_growth
