#!/usr/bin/env python3
import os
import json
import argparse
import sys
import sqlite3
from datetime import date, datetime, timedelta

from screen_a_share import (
    build_quote_table,
    attach_latest_financial_fields,
    attach_dynamic_cagr_fields,
    filter_dividend_strategy,
    filter_growth_strategy,
    attach_ttm_dividend_yield,
    output_columns,
    GROWTH_INDUSTRIES,
    threshold_payload,
    number_or_none,
    string_or_none
)

from us_hk_quant import screen_us_hk
from screen_global_quant_deps import STRATEGIES, load_universes, load_hot_spot_today, get_current_prices_for_portfolio

def get_key(row, strat):
    if "_a_" in strat:
        return row.get("股票简称", "")
    return row.get("股票代码", "")
import db_utils
from core.portfolio import PortfolioManager
from core.strategy import ADividendStrategy, AGrowthStrategy, USHKQuantStrategy, HotSpotStrategy

def process_a_share_data(args, a_tickers, as_of_date):
    """Refactored data fetcher for A-shares, returns raw DFs to be consumed by Strategies"""
    import pandas as pd
    print("Fetching A-share quotes...", flush=True)
    quote_df = build_quote_table(target_codes=a_tickers)
    
    a_prices = {}
    if not quote_df.empty:
        a_prices = dict(zip(quote_df["股票简称"], quote_df["最新价"]))
        
    print("Fetching A-share basic financials...", flush=True)
    with_financial, _, _ = attach_latest_financial_fields(
        quote_df, as_of_date=as_of_date, report_date=args.report_date
    )
    
    div_pre_mask = (
        with_financial["估值公式值"].notna() & (with_financial["估值公式值"] < args.valuation_formula_max)
        & with_financial["总市值"].notna() & (with_financial["总市值"] > args.market_cap_min_yi * 1e8)
        & (with_financial["资产负债率"].isna() | (with_financial["资产负债率"] < args.debt_ratio_max))
    )
    div_pre = with_financial[div_pre_mask].copy()
    
    gro_pre_mask = (
        with_financial["总市值"].notna() & (with_financial["总市值"] > args.market_cap_min_yi * 1e8)
        & with_financial["所处行业"].isin(GROWTH_INDUSTRIES)
        & (with_financial["资产负债率"].isna() | (with_financial["资产负债率"] < args.debt_ratio_max))
    )
    gro_pre = with_financial[gro_pre_mask].copy()
    
    if not div_pre.empty:
        div_pre = attach_ttm_dividend_yield(div_pre, as_of_date)
        div_pre = div_pre[div_pre["TTM股息率"].notna() & (div_pre["TTM股息率"] > args.dividend_yield_min)].copy()
        
    combined_codes = list(set(div_pre["股票代码"].tolist() + gro_pre["股票代码"].tolist()))
    if not combined_codes:
        return pd.DataFrame(), pd.DataFrame(), a_prices
        
    survivors_fin = with_financial[with_financial["股票代码"].isin(combined_codes)].copy()
    merged_df = attach_dynamic_cagr_fields(survivors_fin, as_of_date=as_of_date)
    
    col_rename_map = {
        "净利润-同比增长": "净利润同比增长率",
        "营业总收入-同比增长": "营业总收入同比增长率",
        "3年平均净资产收益率": "净资产收益率",
        "3年平均净利率": "销售净利率"
    }
    merged_df = merged_df.rename(columns={k: v for k, v in col_rename_map.items() if k in merged_df.columns})
    
    div_df, gro_df = pd.DataFrame(), pd.DataFrame()
    if not div_pre.empty:
        div_final_pool = merged_df[merged_df["股票代码"].isin(div_pre["股票代码"])].copy()
        div_final_pool = div_final_pool.merge(div_pre[["股票代码", "TTM股息率"]], on="股票代码", how="left")
        div_df = filter_dividend_strategy(div_final_pool, args)
        
    if not gro_pre.empty:
        gro_final_pool = merged_df[merged_df["股票代码"].isin(gro_pre["股票代码"])].copy()
        gro_df = filter_growth_strategy(gro_final_pool, args)
        
    div_df = output_columns(div_df) if not div_df.empty else pd.DataFrame()
    gro_df = output_columns(gro_df) if not gro_df.empty else pd.DataFrame()
    
    return div_df, gro_df, a_prices


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date", type=str)
    parser.add_argument("--valuation-formula-max", type=float, default=10.0)
    parser.add_argument("--dividend-yield-min", type=float, default=3.0)
    parser.add_argument("--market-cap-min-yi", type=float, default=100.0)
    parser.add_argument("--avg-net-profit-margin-min", type=float, default=10.0)
    parser.add_argument("--require-continuous-growth", action="store_true", default=True)
    parser.add_argument("--profit-cagr-min", type=float, default=5.0)
    parser.add_argument("--debt-ratio-max", type=float, default=50.0)
    parser.add_argument("--dividend-roe-min", type=float, default=10.0)
    parser.add_argument("--growth-roe-min", type=float, default=10.0)
    parser.add_argument("--growth-yoy-min", type=float, default=30.0)
    parser.add_argument("--max-stocks", type=int, default=10)
    parser.add_argument("--output-file", type=str, default=os.path.join(os.environ.get("PROJECT_ROOT", "/Users/zouzhengting/Workplace/a_share_factor_flow"), "global_screen.json"))
    
    args = parser.parse_args()
    from core.clock import clock
    snapshot_date = clock.now().strftime("%Y-%m-%d")
    as_of_date = clock.today()
    
    # Init Strategy classes
    strategies = {
        "dividend_a_stock": ADividendStrategy(args.max_stocks),
        "growth_a_stock": AGrowthStrategy(args.max_stocks),
        "dividend_us_stock": USHKQuantStrategy("dividend_us_stock", args.max_stocks),
        "growth_us_stock": USHKQuantStrategy("growth_us_stock", args.max_stocks),
        "dividend_hk_stock": USHKQuantStrategy("dividend_hk_stock", args.max_stocks),
        "growth_hk_stock": USHKQuantStrategy("growth_hk_stock", args.max_stocks),
    }
    for hs in ["hot_spot_a_stock", "hot_spot_a_etf", "hot_spot_us_stock", "hot_spot_us_etf", "hot_spot_hk_stock", "hot_spot_hk_etf"]:
        strategies[hs] = HotSpotStrategy(hs)
        
    universes = load_universes()
    hot_spot_data = load_hot_spot_today()

    from screen_a_share import load_code_name_table
    a_tickers = load_code_name_table()["股票代码"].tolist()

    div_a_df, gro_a_df, a_prices = None, None, {}
    div_us_df, gro_us_df = None, None
    div_hk_df, gro_hk_df = None, None

    import concurrent.futures
    with concurrent.futures.ProcessPoolExecutor(max_workers=3) as executor:
        future_a = executor.submit(process_a_share_data, args, a_tickers, as_of_date)
        future_us = executor.submit(screen_us_hk, universes["US"], args, "US")
        future_hk = executor.submit(screen_us_hk, universes["HK"], args, "HK")
        
        div_a_df, gro_a_df, a_prices = future_a.result()
        div_us_df, gro_us_df = future_us.result()
        div_hk_df, gro_hk_df = future_hk.result()

    # Generate signals via OOP Strategy pattern
    results = {
        "dividend_a_stock": strategies["dividend_a_stock"].get_signals(df=div_a_df),
        "growth_a_stock": strategies["growth_a_stock"].get_signals(df=gro_a_df),
        "dividend_us_stock": strategies["dividend_us_stock"].get_signals(df=div_us_df),
        "growth_us_stock": strategies["growth_us_stock"].get_signals(df=gro_us_df),
        "dividend_hk_stock": strategies["dividend_hk_stock"].get_signals(df=div_hk_df),
        "growth_hk_stock": strategies["growth_hk_stock"].get_signals(df=gro_hk_df),
    }
    
    for hs in ["hot_spot_a_stock", "hot_spot_a_etf", "hot_spot_us_stock", "hot_spot_us_etf", "hot_spot_hk_stock", "hot_spot_hk_etf"]:
        results[hs] = strategies[hs].get_signals(target_list=hot_spot_data.get(hs, []))

    # A-Share number parsing fix (from original screen_global_quant.py)
    for k in ["dividend_a_stock", "growth_a_stock"]:
        results[k] = [{key: number_or_none(v) if key not in ["股票代码", "股票简称", "财务报告期", "CAGR终点年报", "CAGR起点年报", "所处行业"] else string_or_none(v) for key, v in row.items()} for row in results[k]]

    # LLM Secondary Filtering for Growth Strategies
    try:
        from llm_utils import call_llm
    except:
        call_llm = None

    for strat in ["growth_a_stock", "growth_us_stock", "growth_hk_stock"]:
        if len(results[strat]) > 10:
            if call_llm:
                print(f"Applying LLM secondary filter for {strat} ({len(results[strat])} -> 10)...")
                candidates = []
                for row in results[strat]:
                    name = row.get("股票简称") or row.get("Name") or row.get("股票代码")
                    code = row.get("股票代码", "")
                    industry = row.get("所处行业", "")
                    cagr = row.get("净利润同比增长率", "")
                    candidates.append({"代码": code, "名称": name, "行业": industry, "增速": cagr})
                
                prompt = f"""You are an expert quantitative portfolio manager. We have a list of {len(candidates)} candidate stocks that passed our Growth Strategy quantitative screen.
We need to select up to 10 best candidates to include in the final portfolio.
Focus on:
1. Hard tech dominance (e.g., semiconductors, AI, software, hardware).
2. Strong fundamentals and momentum.

Here are the candidates:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Please return the selected top candidates (maximum 10) as a JSON array of their '代码' (string). Return ONLY the JSON array. Example: ["000001", "000002"]"""
                try:
                    selected_codes = call_llm(prompt, require_json=True)
                    if isinstance(selected_codes, dict):
                        for k, v in selected_codes.items():
                            if isinstance(v, list):
                                selected_codes = v
                                break
                    if isinstance(selected_codes, list):
                        selected_set = set(str(c) for c in selected_codes)
                        new_results = [r for r in results[strat] if str(r.get("股票代码", "")) in selected_set]
                        if new_results:
                            results[strat] = new_results
                        else:
                            print(f"LLM returned mismatched list for {strat}, falling back to top 10 quantitative.")
                            results[strat] = results[strat][:10]
                    else:
                        print(f"LLM returned invalid format for {strat}, falling back to top 10 quantitative.")
                        results[strat] = results[strat][:10]
                except Exception as e:
                    print(f"LLM filtering failed for {strat}: {e}. Falling back to top 10.")
                    results[strat] = results[strat][:10]
            else:
                results[strat] = results[strat][:10]

    # Phase 2: Transactional Portfolio Update via PortfolioManager
    pm = PortfolioManager(db_utils)
    
    # Build a superset of old and new targets to fetch current prices
    strategy_targets = {strat: [get_key(r, strat) for r in results[strat]] for strat in STRATEGIES}
    
    old_portfolio, _ = db_utils.load_portfolio_and_trades()
    all_portfolio = {s: {} for s in STRATEGIES}
    for s in STRATEGIES:
        if s in old_portfolio:
            all_portfolio[s].update(old_portfolio[s])
        for target in strategy_targets[s]:
            all_portfolio[s][target] = {}
            
    current_prices = get_current_prices_for_portfolio(all_portfolio, a_prices)
    
    snapshot_dt = datetime.strptime(snapshot_date[:10], "%Y-%m-%d")
    is_weekend = snapshot_dt.weekday() >= 5
    
    if is_weekend:
        print(f"Skipping diffing because {snapshot_date} is a weekend. Keeping portfolio unchanged.")
        diff = {s: {"added": [], "removed": []} for s in STRATEGIES}
        portfolio = old_portfolio
        new_trades = []
    else:
        portfolio, new_trades, diff = pm.diff_and_update(strategy_targets, current_prices, snapshot_date)
        
    # Inject entry_price and ROI back into results for display
    for strat in STRATEGIES:
        for row in results[strat]:
            key = get_key(row, strat)
            ep = portfolio[strat].get(key, {}).get("entry_price", 0)
            cp = row.get("最新价", 0)
            if cp is None: cp = 0
            row["入选价格"] = ep
            row["累计涨跌幅"] = f"{(cp / ep - 1) * 100:.2f}%" if ep > 0 else "0.00%"

    payload = {
        "mode": "global_12_grid",
        "snapshot_date": snapshot_date,
        "thresholds": threshold_payload(args),
        "stage_counts": {s: len(results[s]) for s in STRATEGIES},
        "results": results,
        "diff": diff
    }
    
    db_utils.save_meta_data("daily_results", payload)
    
    # Save to JSON for UI / email generation backward compatibility
    payload["portfolio"] = portfolio
    payload["trade_history"] = [] # The old JSON payload requires these keys
    with open(args.output_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        
    print(f"Global screening complete via V2 OOP Engine! Saved to SQLite DB and {args.output_file}")

if __name__ == "__main__":
    main()
