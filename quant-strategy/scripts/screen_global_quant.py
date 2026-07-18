#!/usr/bin/env python3
import os
import json
import argparse
import sys
import sqlite3
import hashlib
import math
import tempfile
from datetime import date, datetime, timedelta

import pandas as pd

from screen_a_share import (
    build_quote_table,
    attach_latest_financial_fields,
    attach_dynamic_cagr_fields,
    filter_dividend_strategy,
    filter_dividend_quality,
    filter_growth_strategy,
    attach_ttm_dividend_yield,
    output_columns,
    CYCLICAL_GROWTH_INDUSTRIES,
    threshold_payload,
    number_or_none,
    string_or_none
)

from us_hk_quant import screen_us_hk
from screen_global_quant_deps import STRATEGIES, load_universes, load_hot_spot_today, get_current_prices_for_portfolio

def get_key(row, strat):
    # 修改 P0.6：所有市场统一使用股票代码作为 key，不再使用中文简称
    return row.get("股票代码", "")
import db_utils
from core.portfolio import PortfolioManager
from core.strategy import ADividendStrategy, AGrowthStrategy, USHKQuantStrategy, HotSpotStrategy
from core.quarantine import quarantine_filter


GLOBAL_SCREEN_FIXTURE_ENV = "GLOBAL_SCREEN_FIXTURE"
GLOBAL_SCREEN_FIXTURE_VERSION = 1


class GlobalScreenFixtureError(ValueError):
    """Raised when an offline global-screen fixture violates its contract."""


class _OfflineFixtureGateway:
    """Fail closed if a fixture run reaches a historical market-data path."""

    def get_historical_prices(self, *args, **kwargs):
        raise GlobalScreenFixtureError(
            "offline fixture cannot fetch historical prices; provide a fresh temporary DB "
            "or fixture prices for positions that can be updated directly"
        )


def _normalise_fixture_price(symbol, value):
    raw = value.get("最新价") if isinstance(value, dict) else value
    try:
        price = float(raw)
    except (TypeError, ValueError) as exc:
        raise GlobalScreenFixtureError(
            f"current_prices[{symbol!r}] must be a positive finite number or {{'最新价': number}}"
        ) from exc
    if not math.isfinite(price) or price <= 0:
        raise GlobalScreenFixtureError(
            f"current_prices[{symbol!r}] must be a positive finite number"
        )
    return {"最新价": price}


def load_global_screen_fixture(path):
    """Load and strictly validate the versioned, network-free screen fixture."""
    fixture_path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(fixture_path, "rb") as handle:
            raw = handle.read()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GlobalScreenFixtureError(f"failed to load GLOBAL_SCREEN_FIXTURE: {exc}") from exc

    if not isinstance(payload, dict):
        raise GlobalScreenFixtureError("fixture root must be a JSON object")
    expected_keys = {"fixture_version", "snapshot_date", "results", "current_prices"}
    actual_keys = set(payload)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unknown = sorted(actual_keys - expected_keys)
        raise GlobalScreenFixtureError(
            f"fixture keys must be exactly {sorted(expected_keys)}; missing={missing}, unknown={unknown}"
        )
    if payload["fixture_version"] != GLOBAL_SCREEN_FIXTURE_VERSION:
        raise GlobalScreenFixtureError(
            f"unsupported fixture_version={payload['fixture_version']!r}; "
            f"expected {GLOBAL_SCREEN_FIXTURE_VERSION}"
        )
    try:
        datetime.strptime(payload["snapshot_date"], "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise GlobalScreenFixtureError("snapshot_date must use YYYY-MM-DD") from exc

    raw_results = payload["results"]
    if not isinstance(raw_results, dict) or set(raw_results) != set(STRATEGIES):
        raise GlobalScreenFixtureError(
            f"results must contain exactly these strategies: {sorted(STRATEGIES)}"
        )
    results = {}
    target_symbols = set()
    for strategy in STRATEGIES:
        rows = raw_results[strategy]
        if not isinstance(rows, list):
            raise GlobalScreenFixtureError(f"results[{strategy!r}] must be an array")
        seen = set()
        normalised_rows = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise GlobalScreenFixtureError(f"results[{strategy!r}][{index}] must be an object")
            symbol = row.get("股票代码")
            if not isinstance(symbol, str) or not symbol.strip():
                raise GlobalScreenFixtureError(
                    f"results[{strategy!r}][{index}].股票代码 must be a non-empty string"
                )
            symbol = symbol.strip()
            if symbol in seen:
                raise GlobalScreenFixtureError(
                    f"duplicate 股票代码 {symbol!r} in results[{strategy!r}]"
                )
            seen.add(symbol)
            target_symbols.add(symbol)
            copied = dict(row)
            copied["股票代码"] = symbol
            normalised_rows.append(copied)
        results[strategy] = normalised_rows

    raw_prices = payload["current_prices"]
    if not isinstance(raw_prices, dict):
        raise GlobalScreenFixtureError("current_prices must be an object keyed by 股票代码")
    current_prices = {
        str(symbol): _normalise_fixture_price(str(symbol), value)
        for symbol, value in raw_prices.items()
    }
    missing_prices = sorted(target_symbols - set(current_prices))
    if missing_prices:
        raise GlobalScreenFixtureError(
            f"current_prices is missing target symbols: {missing_prices}"
        )
    for strategy, rows in results.items():
        for row in rows:
            row["最新价"] = current_prices[row["股票代码"]]["最新价"]

    return {
        "path": fixture_path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "fixture_version": GLOBAL_SCREEN_FIXTURE_VERSION,
        "snapshot_date": payload["snapshot_date"],
        "results": results,
        "current_prices": current_prices,
    }


def _persist_daily_results(results, diff, snapshot_date, appendix=None, *, include_empty=False, strict=False):
    conn = None
    try:
        conn = db_utils.get_connection()
        cursor = conn.cursor()
        for strategy in STRATEGIES:
            if include_empty or results.get(strategy):
                result_payload = {
                    "results": results[strategy],
                    "diff": diff.get(strategy, {}),
                    "appendix": appendix.get(strategy, []) if appendix else [],
                }
                serialized = json.dumps(
                    result_payload, ensure_ascii=False, sort_keys=True
                )
                active_filter, active_parameters, _ = quarantine_filter(
                    conn, "strategy_daily_results"
                )
                rows = cursor.execute(
                    "SELECT id, result_json FROM strategy_daily_results "
                    "WHERE result_date=? AND strategy=?" + active_filter,
                    (snapshot_date, strategy, *active_parameters),
                ).fetchall()
                if len(rows) > 1:
                    raise RuntimeError(
                        f"multiple active daily results for {snapshot_date}/{strategy}"
                    )
                if rows:
                    row_id, previous = rows[0]
                    try:
                        unchanged = json.loads(previous) == result_payload
                    except (TypeError, json.JSONDecodeError):
                        unchanged = False
                    if not unchanged:
                        cursor.execute(
                            "UPDATE strategy_daily_results SET result_json=? WHERE id=?",
                            (serialized, row_id),
                        )
                else:
                    cursor.execute(
                        "INSERT INTO strategy_daily_results "
                        "(result_date, strategy, result_json) VALUES (?, ?, ?)",
                        (snapshot_date, strategy, serialized),
                    )
        conn.commit()
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        if strict:
            raise
        print(f"Warning: Failed to save to strategy_daily_results table: {exc}")
    finally:
        if conn is not None:
            conn.close()


def _write_json_atomic(path, payload):
    output_path = os.path.abspath(path)
    output_dir = os.path.dirname(output_path) or os.curdir
    os.makedirs(output_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{os.path.basename(output_path)}.", suffix=".tmp", dir=output_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def _run_offline_fixture(args, fixture):
    """Execute the real portfolio/database flow while blocking every screen/data fetch."""
    database_path = db_utils.get_db_path()
    database_environment = os.environ.get("QUANT_DB_ENV")
    if (
        database_path == db_utils.get_production_db_path()
        or database_environment not in {"test", "backtest"}
    ):
        raise GlobalScreenFixtureError(
            "GLOBAL_SCREEN_FIXTURE requires an explicit non-production SQLITE_DB_PATH "
            "and QUANT_DB_ENV=test or QUANT_DB_ENV=backtest"
        )
    snapshot_date = fixture["snapshot_date"]
    results = fixture["results"]
    current_prices = fixture["current_prices"]
    old_portfolio, _ = db_utils.load_portfolio_and_trades()

    required_symbols = {
        symbol
        for strategy in STRATEGIES
        for symbol in old_portfolio.get(strategy, {})
    } | {
        row["股票代码"]
        for strategy in STRATEGIES
        for row in results[strategy]
    }
    missing_prices = sorted(required_symbols - set(current_prices))
    if missing_prices:
        raise GlobalScreenFixtureError(
            f"current_prices must cover existing and target positions: {missing_prices}"
        )

    strategy_targets = {
        strategy: [get_key(row, strategy) for row in results[strategy]]
        for strategy in STRATEGIES
    }
    manager = PortfolioManager(db_utils)

    # Fixture prices are already execution-approved inputs. Bypass market-clock and
    # pending-price resolution so an offline run cannot branch on wall clock or network.
    manager.get_simulated_trade_price = lambda prices, _strategy: float(prices["最新价"])
    manager.resolve_pending_prices = lambda: None
    portfolio_module = sys.modules[PortfolioManager.__module__]
    from core import diagnose as diagnose_module
    original_gateway = portfolio_module.data_gateway
    original_diagnose = diagnose_module.diagnose_elimination
    portfolio_module.data_gateway = _OfflineFixtureGateway()
    diagnose_module.diagnose_elimination = (
        lambda _symbol, _strategy: "离线固定测试集移除"
    )
    try:
        portfolio, _new_trades, diff = manager.diff_and_update(
            strategy_targets, current_prices, snapshot_date
        )
    finally:
        portfolio_module.data_gateway = original_gateway
        diagnose_module.diagnose_elimination = original_diagnose
    inject_portfolio_metrics(
        results, portfolio, snapshot_date, gateway_instance=_OfflineFixtureGateway()
    )

    payload = {
        "mode": "global_12_grid_fixture_v1",
        "snapshot_date": snapshot_date,
        "fixture": {
            "version": fixture["fixture_version"],
            "sha256": fixture["sha256"],
            "path": fixture["path"],
        },
        "thresholds": threshold_payload(args),
        "stage_counts": {strategy: len(results[strategy]) for strategy in STRATEGIES},
        "results": results,
        "appendix": {strategy: [] for strategy in STRATEGIES},
        "diff": diff,
        "portfolio": portfolio,
        "trade_history": [],
    }
    _persist_daily_results(
        results, diff, snapshot_date, appendix=payload["appendix"], include_empty=True, strict=True
    )
    _write_json_atomic(args.output_file, payload)
    print(
        "Global screening offline fixture complete! "
        f"Saved to SQLite DB and {args.output_file}"
    )
    return payload

def _quote_coverage_metrics(quote_df):
    """Separate provider transport health from factor eligibility."""
    if quote_df is None or quote_df.empty:
        return 0.0, 0.0
    prices = pd.to_numeric(quote_df.get("最新价"), errors="coerce")
    market_caps = pd.to_numeric(quote_df.get("总市值"), errors="coerce")
    pe = pd.to_numeric(quote_df.get("PE"), errors="coerce")
    pb = pd.to_numeric(quote_df.get("PB"), errors="coerce")
    transported = (prices > 0) & (market_caps > 0)
    factors = transported & (pe > 0) & (pb > 0)
    return float(transported.mean()), float(factors.mean())


def process_a_share_data(args, a_tickers, as_of_date):
    """Refactored data fetcher for A-shares, returns raw DFs to be consumed by Strategies"""
    import pandas as pd
    print("Fetching A-share quotes...", flush=True)
    quote_df = build_quote_table(target_codes=a_tickers)

    quote_coverage, quote_factor_coverage = _quote_coverage_metrics(quote_df)
    quote_min_coverage = getattr(args, "quote_min_coverage", 0.99)
    if quote_coverage < quote_min_coverage:
        raise RuntimeError(
            f"A-share quote coverage {quote_coverage:.2%} below required {quote_min_coverage:.2%}"
        )

    a_prices = {}
    if not quote_df.empty:
        a_prices = dict(zip(quote_df["股票代码"], quote_df["最新价"]))

    print("Fetching A-share basic financials...", flush=True)
    with_financial, _, _ = attach_latest_financial_fields(
        quote_df, as_of_date=as_of_date, report_date=args.report_date
    )
    financial_coverage = float(with_financial["财务报告期"].notna().mean()) if len(with_financial) else 0.0
    financial_min_coverage = getattr(args, "financial_min_coverage", 0.90)
    if financial_coverage < financial_min_coverage:
        raise RuntimeError(
            f"A-share financial coverage {financial_coverage:.2%} below required {financial_min_coverage:.2%}"
        )

    div_pre_mask = (
        with_financial["PE"].notna() & (with_financial["PE"] > 0)
        & with_financial["PB"].notna() & (with_financial["PB"] > 0)
        & with_financial["估值公式值"].notna() & (with_financial["估值公式值"] < args.valuation_formula_max)
        & with_financial["总市值"].notna() & (with_financial["总市值"] > args.market_cap_min_yi * 1e8)
    )
    div_pre = with_financial[div_pre_mask].copy()

    gro_pre_mask = (
        with_financial["总市值"].notna() & (with_financial["总市值"] > args.market_cap_min_yi * 1e8)
        & with_financial["所处行业"].isin(CYCLICAL_GROWTH_INDUSTRIES)
    )
    gro_pre = with_financial[gro_pre_mask].copy()

    dividend_coverage = 1.0
    if not div_pre.empty:
        div_pre = attach_ttm_dividend_yield(div_pre, as_of_date)
        evaluated = div_pre["分红数据状态"].isin(["ok", "confirmed_no_dividend"])
        dividend_coverage = float(evaluated.mean())
        minimum_coverage = getattr(args, "dividend_min_data_coverage", 0.95)
        if dividend_coverage < minimum_coverage:
            errors = div_pre.loc[~evaluated, "分红数据状态"].value_counts().to_dict()
            raise RuntimeError(
                f"A-share dividend coverage {dividend_coverage:.2%} below required "
                f"{minimum_coverage:.2%}; errors={errors}"
            )

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
    dividend_fundamental_count = 0
    if not div_pre.empty:
        div_final_pool = merged_df[merged_df["股票代码"].isin(div_pre["股票代码"])].copy()
        dividend_columns = [
            "股票代码", "TTM股息率", "分红数据状态", "分红数据错误",
            "近5年分红年份数", "连续3年分红", "最近年度分红变动率",
        ]
        div_final_pool = div_final_pool.merge(div_pre[dividend_columns], on="股票代码", how="left")
        div_df = filter_dividend_strategy(div_final_pool, args)
        dividend_fundamental_count = len(div_df)
        div_df = filter_dividend_quality(div_df, args)

    if not gro_pre.empty:
        gro_final_pool = merged_df[merged_df["股票代码"].isin(gro_pre["股票代码"])].copy()
        gro_df = filter_growth_strategy(gro_final_pool, args)

    div_df = output_columns(div_df) if not div_df.empty else pd.DataFrame()
    gro_df = output_columns(gro_df) if not gro_df.empty else pd.DataFrame()

    print(
        "A-share data health: "
        f"quote_transport={quote_coverage:.2%}, "
        f"quote_factors={quote_factor_coverage:.2%}, "
        f"financials={financial_coverage:.2%}, "
        f"dividends={dividend_coverage:.2%}, dividend_prefilter={len(div_pre)}, "
        f"dividend_fundamentals={dividend_fundamental_count}, "
        f"dividend_selected={len(div_df)} (quality gates + industry cap; no score)",
        flush=True,
    )
    return div_df, gro_df, a_prices

def inject_portfolio_metrics(results, portfolio, snapshot_date, gateway_instance=None):
    from core.data_gateway import DataGateway
    import logging

    gateway = gateway_instance if gateway_instance else DataGateway()

    for strat in STRATEGIES:
        for row in results.get(strat, []):
            key = get_key(row, strat)
            ep = portfolio[strat].get(key, {}).get("entry_price", 0.0)
            ed = portfolio[strat].get(key, {}).get("entry_date", snapshot_date)
            shares = portfolio[strat].get(key, {}).get("shares", 1)
            cp = row.get("最新价", 0.0)
            if cp is None: cp = 0.0

            # Fetch QFQ price for entry_date to calculate accurate floating return
            adj_ep = ep
            fetch_qfq_success = False
            if ep > 0 and ed != snapshot_date:
                try:
                    yf_code = key
                    if '_hk_' in strat and not key.upper().endswith('.HK'):
                        yf_code = f"{key}.HK"
                    ed_str = ed[:10].replace('-', '')
                    dt = datetime.strptime(ed_str, '%Y%m%d')
                    start_str = (dt - timedelta(days=7)).strftime('%Y%m%d')
                    df_qfq = gateway.get_historical_prices(yf_code, start_date=start_str, end_date=ed_str, adjust="qfq")
                    if not df_qfq.empty:
                        adj_ep = float(df_qfq.iloc[-1]['收盘'])
                        fetch_qfq_success = True
                except Exception as e:
                    logging.warning(f"Failed to fetch qfq price for {key} on {ed}: {e}")
            else:
                # If entering today, PnL is 0
                fetch_qfq_success = True

            row["入选价格"] = float(ep)
            row["仓位份数"] = shares
            if not fetch_qfq_success:
                row["累计涨跌幅"] = "N/A"
            else:
                row["累计涨跌幅"] = f"{(cp / adj_ep - 1) * 100:.2f}%" if adj_ep > 0 else "0.00%"
            row["入选日期"] = ed

def main(argv=None):
    parser = argparse.ArgumentParser(description="Global Macro Quant Screener V2")
    parser.add_argument("--report-date", type=str)
    parser.add_argument("--valuation-formula-max", type=float, default=10.0)
    parser.add_argument("--dividend-yield-min", type=float, default=3.0)
    parser.add_argument("--market-cap-min-yi", type=float, default=100.0)
    parser.add_argument("--avg-net-profit-margin-min", type=float, default=10.0)
    parser.add_argument(
        "--require-continuous-growth",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require revenue and net profit YoY growth in each of the latest 3 annual reports",
    )
    parser.add_argument("--peg-max", type=float, default=1.0)
    parser.add_argument("--profit-cagr-min", type=float, default=5.0)
    parser.add_argument("--dividend-roe-min", type=float, default=10.0)
    parser.add_argument("--growth-roe-min", type=float, default=10.0)
    parser.add_argument("--growth-yoy-min", type=float, default=30.0)
    parser.add_argument("--max-stocks", type=int, default=10)
    parser.add_argument("--cash-profit-coverage-min", type=float, default=0.8)
    parser.add_argument("--dividend-min-years", type=int, default=4)
    parser.add_argument("--dividend-max-cut-pct", type=float, default=30.0)
    parser.add_argument("--dividend-max-per-industry", type=int, default=3)
    parser.add_argument("--dividend-max-results", type=int, default=50)
    parser.add_argument("--quote-min-coverage", type=float, default=0.99)
    parser.add_argument("--financial-min-coverage", type=float, default=0.90)
    parser.add_argument("--dividend-min-data-coverage", type=float, default=0.95)
    parser.add_argument("--disable-llm", action="store_true", help="Disable LLM secondary filtering")
    from config import PROJECT_ROOT
    parser.add_argument("--output-file", type=str, default=os.path.join(PROJECT_ROOT, "global_screen.json"))

    args = parser.parse_args(argv)
    from core.clock import clock
    from core.logger import get_quant_logger

    logger = get_quant_logger("screen_global_quant")
    logger.info("="*50)
    logger.info("Starting Global Quant Screening V2 (OOP Engine)")
    logger.info("="*50)

    fixture_path = os.environ.get(GLOBAL_SCREEN_FIXTURE_ENV)
    if fixture_path:
        logger.info("Running strict offline fixture version %s", GLOBAL_SCREEN_FIXTURE_VERSION)
        return _run_offline_fixture(args, load_global_screen_fixture(fixture_path))

    snapshot_date = clock.now().strftime("%Y-%m-%d")
    as_of_date = clock.today()

    # Init Strategy classes
    strategies = {
        "dividend_a_stock": ADividendStrategy(args.dividend_max_results),
        "growth_a_stock": AGrowthStrategy(args.max_stocks),
        "dividend_us_stock": USHKQuantStrategy("dividend_us_stock", args.max_stocks),
        "growth_us_stock": USHKQuantStrategy("growth_us_stock", args.max_stocks),
        "dividend_hk_stock": USHKQuantStrategy("dividend_hk_stock", args.max_stocks),
        "growth_hk_stock": USHKQuantStrategy("growth_hk_stock", args.max_stocks),
    }
    for hs in ["hot_spot_a_stock", "hot_spot_us_stock", "hot_spot_hk_stock"]:
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

    # Load old portfolio early to prevent flapping
    old_portfolio, _ = db_utils.load_portfolio_and_trades()

    # Generate signals via OOP Strategy pattern
    results = {
        "dividend_a_stock": strategies["dividend_a_stock"].get_signals(df=div_a_df, previous_holdings=list(old_portfolio.get("dividend_a_stock", {}).keys())),
        "growth_a_stock": strategies["growth_a_stock"].get_signals(df=gro_a_df, previous_holdings=list(old_portfolio.get("growth_a_stock", {}).keys())),
        "dividend_us_stock": strategies["dividend_us_stock"].get_signals(df=div_us_df, previous_holdings=list(old_portfolio.get("dividend_us_stock", {}).keys())),
        "growth_us_stock": strategies["growth_us_stock"].get_signals(df=gro_us_df, previous_holdings=list(old_portfolio.get("growth_us_stock", {}).keys())),
        "dividend_hk_stock": strategies["dividend_hk_stock"].get_signals(df=div_hk_df, previous_holdings=list(old_portfolio.get("dividend_hk_stock", {}).keys())),
        "growth_hk_stock": strategies["growth_hk_stock"].get_signals(df=gro_hk_df, previous_holdings=list(old_portfolio.get("growth_hk_stock", {}).keys())),
    }

    for hs in ["hot_spot_a_stock", "hot_spot_us_stock", "hot_spot_hk_stock"]:
        results[hs] = strategies[hs].get_signals(target_list=hot_spot_data.get(hs, []))

    # A-Share number parsing fix (from original screen_global_quant.py)
    for k in ["dividend_a_stock", "growth_a_stock"]:
        string_fields = [
            "股票代码", "股票简称", "财务报告期", "CAGR终点年报",
            "CAGR起点年报", "所处行业", "分红数据状态",
        ]
        results[k] = [
            {
                key: number_or_none(v) if key not in string_fields else string_or_none(v)
                for key, v in row.items()
            }
            for row in results[k]
        ]

    # LLM Secondary Filtering for Growth Strategies
    try:
        if args.disable_llm:
            call_llm = None
            print("LLM filtering disabled via --disable-llm.")
        else:
            from llm_utils import call_llm
    except:
        call_llm = None

    appendix = {s: [] for s in STRATEGIES}

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

                prev_holdings = list(old_portfolio.get(strat, {}).keys())

                prompt = f"""You are an expert quantitative portfolio manager. We have a list of {len(candidates)} candidate stocks that passed our Growth Strategy quantitative screen.
We need to select up to 10 best candidates to include in the final portfolio.
Focus on:
1. Hard tech dominance (e.g., semiconductors, AI, software, hardware).
2. Strong fundamentals and momentum.

"""
                if prev_holdings:
                    prompt += f"【重要】：以下是昨日该策略的当前持仓标的代码：{json.dumps(prev_holdings, ensure_ascii=False)}\n为了降低换手率，如果这些持仓标的依然在候选列表中且基本面没有恶化，请优先保留它们。\n\n"

                prompt += f"""Here are the candidates:
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
                        selected_results = []
                        unselected_results = []
                        for r in results[strat]:
                            if str(r.get("股票代码", "")) in selected_set:
                                selected_results.append(r)
                            else:
                                unselected_results.append(r)

                        if selected_results:
                            results[strat] = selected_results
                            appendix[strat] = unselected_results
                        else:
                            print(f"LLM returned mismatched list for {strat}, falling back to top 10 quantitative.")
                            appendix[strat] = results[strat][10:]
                            results[strat] = results[strat][:10]
                    else:
                        print(f"LLM returned invalid format for {strat}, falling back to top 10 quantitative.")
                        appendix[strat] = results[strat][10:]
                        results[strat] = results[strat][:10]
                except Exception as e:
                    print(f"LLM filtering failed for {strat}: {e}. Falling back to top 10.")
                    appendix[strat] = results[strat][10:]
                    results[strat] = results[strat][:10]
            else:
                appendix[strat] = results[strat][10:]
                results[strat] = results[strat][:10]

    final_limits = {strategy: 10 for strategy in STRATEGIES}
    final_limits["dividend_a_stock"] = args.dividend_max_results
    for strat in STRATEGIES:
        limit = final_limits[strat]
        if len(results[strat]) > limit:
            if not appendix.get(strat):
                appendix[strat] = results[strat][limit:]
            else:
                appendix[strat].extend(results[strat][limit:])
            results[strat] = results[strat][:limit]

    # Phase 2: Transactional Portfolio Update via PortfolioManager
    pm = PortfolioManager(db_utils)

    # Build a superset of old and new targets to fetch current prices
    strategy_targets = {strat: [get_key(r, strat) for r in results[strat]] for strat in STRATEGIES}

    all_portfolio = {s: {} for s in STRATEGIES}
    for s in STRATEGIES:
        if s in old_portfolio:
            all_portfolio[s].update(old_portfolio[s])
        for target in strategy_targets[s]:
            all_portfolio[s][target] = {}

    # Inject Hot Spot A-share/ETF prices into a_prices so get_current_prices_for_portfolio can map them
    for hs_key, items in hot_spot_data.items():
        if '_a_' in hs_key:
            for item in items:
                if "股票代码" in item and "最新价" in item:
                    a_prices[item["股票代码"]] = item["最新价"]

    current_prices = get_current_prices_for_portfolio(all_portfolio, a_prices)

    # 修改 P0.3：将周末判断按市场区分，不再进行全局一刀切跳过
    strategy_targets_market_filtered = {}
    from core.market import AShareMarket, HKMarket, USMarket
    for strat, targets in strategy_targets.items():
        if "_us_" in strat:
            m = USMarket()
        elif "_hk_" in strat:
            m = HKMarket()
        else:
            m = AShareMarket()
        # Instead of completely skipping the run, we keep targets empty if the market is closed for weekend/holiday.
        # This prevents accidental drops or fake transactions.
        if not m.is_trading_time() and m.get_current_time().weekday() >= 5 and os.environ.get("FORCE_RUN") != "1":
            # If weekend, we do not update this strategy's target (keep it exactly as old_portfolio)
            strategy_targets_market_filtered[strat] = list(old_portfolio.get(strat, {}).keys())
        else:
            strategy_targets_market_filtered[strat] = targets

    portfolio, new_trades, diff = pm.diff_and_update(strategy_targets_market_filtered, current_prices, snapshot_date)

    # Inject entry_price and ROI back into results for display
    inject_portfolio_metrics(results, portfolio, snapshot_date)

    payload = {
        "mode": "global_12_grid",
        "snapshot_date": snapshot_date,
        "thresholds": threshold_payload(args),
        "stage_counts": {s: len(results[s]) for s in STRATEGIES},
        "results": results,
        "appendix": appendix,
        "diff": diff
    }

    # P3.24: 将大 JSON 保存逻辑从统一的 meta_data 表迁移到按日期和策略拆分的专用表中
    _persist_daily_results(results, diff, snapshot_date, appendix=appendix)

    # DB save has been fully migrated to strategy_daily_results table

    # Save to JSON for UI / email generation backward compatibility
    payload["portfolio"] = portfolio
    payload["trade_history"] = [] # The old JSON payload requires these keys
    _write_json_atomic(args.output_file, payload)

    print(f"Global screening complete via V2 OOP Engine! Saved to SQLite DB and {args.output_file}")
    return payload

if __name__ == "__main__":
    main()
