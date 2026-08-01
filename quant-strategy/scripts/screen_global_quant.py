#!/usr/bin/env python3
import os
import json
import argparse
import sqlite3
import hashlib
import math
import tempfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from screen_a_share import (
    DIVIDEND_AVG_ROE_MIN,
    build_quote_table,
    attach_latest_financial_fields,
    attach_dynamic_cagr_fields,
    filter_dividend_strategy,
    filter_dividend_quality,
    filter_growth_strategy,
    attach_ttm_dividend_yield,
    output_columns,
    threshold_payload,
    number_or_none,
    string_or_none
)

from us_hk_quant import screen_us_hk
from screen_global_quant_deps import STRATEGIES, load_universes, load_hot_spot_today

def get_key(row, strat):
    # 修改 P0.6：所有市场统一使用股票代码作为 key，不再使用中文简称
    return row.get("股票代码", "")
import db_utils
from core.portfolio_limits import MAX_HOLDINGS_PER_STRATEGY, ordered_unique_symbols
from core.strategy import ADividendStrategy, AGrowthStrategy, USHKQuantStrategy, HotSpotStrategy
from core.quarantine import quarantine_filter
from core.trade_intents import TradeIntentLedger
from core.clock import clock


GLOBAL_SCREEN_FIXTURE_ENV = "GLOBAL_SCREEN_FIXTURE"
GLOBAL_SCREEN_FIXTURE_VERSION = 1
LLM_SECONDARY_GROWTH_STRATEGIES = ()


def select_add_tranche_symbols(results, portfolio, snapshot_date):
    """Select retained positions that crossed a fixed-tranche drawdown gate.

    A missing or malformed return is never treated as a signal.  The first
    add is allowed at -10%, the second at -15.5%, and three tranches is the
    hard cap.  Same-day entries are excluded because they have no completed
    holding-period price evidence yet.
    """
    selected = {}
    for strategy, rows in results.items():
        positions = portfolio.get(strategy) or {}
        additions = []
        for row in rows:
            symbol = str(get_key(row, strategy) or "")
            position = positions.get(symbol)
            if not symbol or not position:
                continue
            if str(position.get("entry_date") or "")[:10] >= str(snapshot_date):
                continue
            shares = max(1, int(position.get("shares") or 1))
            threshold = {1: -10.0, 2: -15.5}.get(shares)
            if threshold is None:
                continue
            raw_return = row.get("累计涨跌幅")
            try:
                observed_return = float(
                    str(raw_return).strip().rstrip("%")
                )
            except (TypeError, ValueError):
                continue
            if not math.isfinite(observed_return):
                continue
            if observed_return <= threshold:
                additions.append(symbol)
        if additions:
            selected[strategy] = ordered_unique_symbols(additions)
    return selected


def _plan_trade_intents(
    strategy_targets,
    snapshot_date,
    old_portfolio,
    add_tranche_by_strategy=None,
):
    """Persist target changes without claiming that a market fill occurred."""
    run_id = (
        os.environ.get("PIPELINE_RUN_ID")
        or os.environ.get("RUN_ID")
        or f"manual-screen-{snapshot_date}"
    )
    connection = db_utils.get_connection()
    try:
        connection.execute("BEGIN")
        for strategy in STRATEGIES:
            connection.execute(
                "INSERT OR IGNORE INTO strategy_accounts "
                "(strategy_id,total_capital,available_cash) VALUES (?,1000000,1000000)",
                (strategy,),
            )
        ledger = TradeIntentLedger(connection)
        signal_timestamp = clock.now(timezone.utc)
        diff = {strategy: {"added": [], "removed": []} for strategy in STRATEGIES}
        summary = {strategy: [] for strategy in STRATEGIES}
        for strategy in STRATEGIES:
            execution_targets = ordered_unique_symbols(
                strategy_targets.get(strategy, [])
            )[:MAX_HOLDINGS_PER_STRATEGY]
            intents = ledger.plan_strategy(
                run_id=run_id,
                signal_date=snapshot_date,
                strategy_id=strategy,
                ranked_targets=execution_targets,
                reason="quantitative target change; awaiting eligible-session raw open",
                manage_transaction=False,
                signal_timestamp=signal_timestamp,
                add_tranche_symbols=(
                    (add_tranche_by_strategy or {}).get(strategy, [])
                ),
            )
            for intent in intents:
                item = {
                    "intent_id": intent["intent_id"],
                    "name": intent["symbol"],
                    "action": intent["action"],
                    "state": intent["state"],
                    "eligible_session": intent["eligible_session"],
                    "reason": intent.get("reason") or "",
                    "source_run_id": intent["source_run_id"],
                    "signal_date": intent["signal_date"],
                }
                summary[strategy].append(item)
                if intent["action"] == "SELL_ALL":
                    position = old_portfolio.get(strategy, {}).get(intent["symbol"], {})
                    diff[strategy]["removed"].append(
                        {
                            **item,
                            "entry_price": float(position.get("entry_price") or 0),
                            "exit_price": 0.0,
                            "pnl": 0.0,
                        }
                    )
                else:
                    diff[strategy]["added"].append(
                        {**item, "entry_price": 0.0}
                    )
        connection.commit()
        return diff, summary
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def apply_final_result_limits(results, appendix, args):
    """Cap dividend output only; every mechanically qualified growth stock remains."""
    final_limits = {strategy: 10 for strategy in STRATEGIES}
    final_limits["dividend_a_stock"] = args.dividend_max_results
    for strategy in ("growth_a_stock", "growth_us_stock", "growth_hk_stock"):
        final_limits[strategy] = None
    for strategy in STRATEGIES:
        limit = final_limits[strategy]
        if limit is None or len(results[strategy]) <= limit:
            continue
        if not appendix.get(strategy):
            appendix[strategy] = results[strategy][limit:]
        else:
            appendix[strategy].extend(results[strategy][limit:])
        results[strategy] = results[strategy][:limit]


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
    """Plan the real settlement flow while blocking every screen/data fetch."""
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
    old_portfolio, _ = db_utils.load_portfolio_and_trades()
    portfolio = old_portfolio
    inject_portfolio_metrics(
        results,
        portfolio,
        snapshot_date,
        gateway_instance=_OfflineFixtureGateway(),
    )

    strategy_targets = {
        strategy: [get_key(row, strategy) for row in results[strategy]]
        for strategy in STRATEGIES
    }
    add_tranche_by_strategy = select_add_tranche_symbols(
        results,
        portfolio,
        snapshot_date,
    )
    diff, intent_summary = _plan_trade_intents(
        strategy_targets,
        snapshot_date,
        old_portfolio,
        add_tranche_by_strategy,
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
        "trade_intents": intent_summary,
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
        f"dividend_selected={len(div_df)} (quality gates + industry cap; no score), "
        f"growth_prefilter={len(gro_pre)}, growth_selected={len(gro_df)} (uncapped)",
        flush=True,
    )
    return div_df, gro_df, a_prices

def inject_portfolio_metrics(results, portfolio, snapshot_date, gateway_instance=None):
    from core.data_gateway import DataGateway
    import logging

    gateway = gateway_instance if gateway_instance else DataGateway()

    for strat in STRATEGIES:
        strategy_positions = portfolio.get(strat) or {}
        for row in results.get(strat, []):
            key = get_key(row, strat)
            position = strategy_positions.get(key) or {}
            ep = position.get("entry_price", 0.0)
            ed = position.get("entry_date", snapshot_date)
            shares = position.get("shares", 1)
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

def build_parser():
    parser = argparse.ArgumentParser(description="Global Macro Quant Screener V2")
    parser.add_argument("--report-date", type=str)
    parser.add_argument("--valuation-formula-max", type=float, default=10.0)
    parser.add_argument("--dividend-yield-min", type=float, default=3.0)
    parser.add_argument("--market-cap-min-yi", type=float, default=100.0)
    parser.add_argument(
        "--avg-net-profit-margin-min",
        type=float,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--require-continuous-growth",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--peg-max", type=float, default=1.0)
    parser.add_argument("--profit-cagr-min", type=float, default=5.0)
    parser.add_argument(
        "--dividend-roe-min",
        type=float,
        default=DIVIDEND_AVG_ROE_MIN,
    )
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
    llm_group = parser.add_mutually_exclusive_group()
    llm_group.add_argument(
        "--disable-llm",
        dest="disable_llm",
        action="store_true",
        help="Disable LLM secondary filtering (safe default)",
    )
    llm_group.add_argument(
        "--enable-llm",
        dest="disable_llm",
        action="store_false",
        help="Explicitly allow candidate and holding context to leave the process",
    )
    parser.set_defaults(disable_llm=True)
    from config import PROJECT_ROOT
    parser.add_argument("--output-file", type=str, default=os.path.join(PROJECT_ROOT, "global_screen.json"))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
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

    as_of_date = clock.today()
    snapshot_date = as_of_date.isoformat()

    # Init Strategy classes
    strategies = {
        "dividend_a_stock": ADividendStrategy(args.dividend_max_results),
        "growth_a_stock": AGrowthStrategy(args.max_stocks),
        "growth_us_stock": USHKQuantStrategy("growth_us_stock", args.max_stocks),
        "growth_hk_stock": USHKQuantStrategy("growth_hk_stock", args.max_stocks),
    }
    for hs in ["hot_spot_a_stock", "hot_spot_us_stock", "hot_spot_hk_stock"]:
        strategies[hs] = HotSpotStrategy(hs)

    universes = load_universes()
    hot_spot_data = load_hot_spot_today()

    from screen_a_share import load_code_name_table
    a_tickers = load_code_name_table()["股票代码"].tolist()

    div_a_df, gro_a_df, a_prices = None, None, {}
    gro_us_df = None
    gro_hk_df = None

    import concurrent.futures
    with concurrent.futures.ProcessPoolExecutor(max_workers=3) as executor:
        future_a = executor.submit(process_a_share_data, args, a_tickers, as_of_date)
        future_us = executor.submit(screen_us_hk, universes["US"], args, "US")
        future_hk = executor.submit(screen_us_hk, universes["HK"], args, "HK")

        div_a_df, gro_a_df, a_prices = future_a.result()
        _, gro_us_df = future_us.result()
        _, gro_hk_df = future_hk.result()

    # Load old portfolio early to prevent flapping
    old_portfolio, _ = db_utils.load_portfolio_and_trades()

    # Generate signals via OOP Strategy pattern
    results = {
        "dividend_a_stock": strategies["dividend_a_stock"].get_signals(df=div_a_df, previous_holdings=list(old_portfolio.get("dividend_a_stock", {}).keys())),
        "growth_a_stock": strategies["growth_a_stock"].get_signals(df=gro_a_df, previous_holdings=list(old_portfolio.get("growth_a_stock", {}).keys())),
        "growth_us_stock": strategies["growth_us_stock"].get_signals(df=gro_us_df, previous_holdings=list(old_portfolio.get("growth_us_stock", {}).keys())),
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

    for strat in LLM_SECONDARY_GROWTH_STRATEGIES:
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

    apply_final_result_limits(results, appendix, args)

    # Phase 2: attach auditable holding-period metrics before planning.  A
    # missing adjusted-entry observation fails closed and cannot create an add.
    portfolio = old_portfolio
    inject_portfolio_metrics(results, portfolio, snapshot_date)
    add_tranche_by_strategy = select_add_tranche_symbols(
        results,
        portfolio,
        snapshot_date,
    )

    # Persist market-aware intents. Screening never claims a fill.
    strategy_targets = {strat: [get_key(r, strat) for r in results[strat]] for strat in STRATEGIES}
    diff, intent_summary = _plan_trade_intents(
        strategy_targets,
        snapshot_date,
        old_portfolio,
        add_tranche_by_strategy,
    )

    payload = {
        "mode": "global_12_grid",
        "snapshot_date": snapshot_date,
        "thresholds": threshold_payload(args),
        "stage_counts": {s: len(results[s]) for s in STRATEGIES},
        "results": results,
        "appendix": appendix,
        "diff": diff,
        "trade_intents": intent_summary,
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
