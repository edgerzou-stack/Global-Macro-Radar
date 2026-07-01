import yfinance as yf
import pandas as pd
import concurrent.futures
import time
import random
from data_provider import disk_cache

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
        if market_cap is not None and market_cap / 1e8 > args.market_cap_min_yi:
            if roe is not None and roe > args.growth_roe_min:
                if net_margin is not None and net_margin > args.avg_net_profit_margin_min:
                    if debt_to_asset is None or debt_to_asset < args.debt_ratio_max:
                        if revenue_growth is not None and revenue_growth * 100 > args.growth_yoy_min:
                            if earnings_growth is not None and earnings_growth * 100 > args.growth_yoy_min:
                                try:
                                    q_stmt = fetch_yf_quarterly_income_stmt_cached(ticker_symbol)
                                    if not q_stmt.empty and len(q_stmt.columns) >= 5:
                                        if "Net Income" in q_stmt.index and "Total Revenue" in q_stmt.index:
                                            net_income = q_stmt.loc["Net Income"].values[:5]
                                            total_rev = q_stmt.loc["Total Revenue"].values[:5]
                                            
                                            # Instead of strictly accelerating, just check if they are all positive
                                            try:
                                                ni_g = [(net_income[i] - net_income[i+1]) / abs(net_income[i+1]) for i in range(4)]
                                                rev_g = [(total_rev[i] - total_rev[i+1]) / abs(total_rev[i+1]) for i in range(4)]
                                                
                                                ni_accel = all(g > 0 for g in ni_g)
                                                rev_accel = all(g > 0 for g in rev_g)
                                                
                                                if ni_accel and rev_accel:
                                                    pass_gro_precheck = True
                                            except ZeroDivisionError:
                                                pass
                                except Exception as e:
                                    pass # If missing data, just fail
                    
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
            "资产负债率": debt_to_asset,
            "最新价": info.get("currentPrice", info.get("previousClose")),
            "所处行业": info.get("sector")
        }
    except Exception as e:
        print(f"Failed to fetch {ticker_symbol}: {e}")
        return None

from tqdm import tqdm

def screen_us_hk(tickers, args, market_type="US"):
    frames = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_yf_data, t, args) for t in tickers]
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc=f"Scanning {market_type} stocks"):
            try:
                res = future.result(timeout=30)
            except concurrent.futures.TimeoutError:
                continue
            if res is not None:
                frames.append(res)
    
    if not frames:
        return pd.DataFrame(), pd.DataFrame()
        
    df = pd.DataFrame(frames)
    
    # Dividend Screen (Disabled for US/HK due to high taxes)
    df_div = pd.DataFrame()
    
    # 2. Filter growth
    mask_gro = (
        df["总市值(亿元)"].notna() & (df["总市值(亿元)"] > args.market_cap_min_yi)
        & df["净利润同比增长率"].notna() 
        & df["营业总收入同比增长率"].notna() 
        & (df["PE"].isna() | ((df["PE"] < df["净利润同比增长率"]) & (df["PE"] < df["营业总收入同比增长率"])))
        & (df["资产负债率"].isna() | (df["资产负债率"] < args.debt_ratio_max))
    )
    
    df_growth = df[mask_gro].copy()
    
    return df_div, df_growth
