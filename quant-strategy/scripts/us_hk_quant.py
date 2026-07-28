import yfinance as yf
import pandas as pd
import multiprocessing
import json
import time
import random
from data_provider import disk_cache
import os
import requests
import hashlib
import pickle
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache


@dataclass(frozen=True)
class FetchOutcome:
    ticker: str
    status: str
    row: dict = None
    reason: str = ""
    period_end: str = ""
    filing_date: str = ""
    source_document: str = ""
    financial_attempted: bool = None
    financial_usable: bool = None


CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cache")
CACHE_SCHEMA_VERSION = 4
DEFAULT_US_HK_MAX_WORKERS = 4
DEFAULT_US_HK_STAGE_TIMEOUT_SECONDS = 900.0
DEFAULT_US_SECONDS_PER_TICKER_BUDGET = 15.0
DEFAULT_HK_SECONDS_PER_TICKER_BUDGET = 120.0
DEFAULT_MIN_FINANCIAL_USABLE_COVERAGE = {
    "US": 0.80,
    "HK": 0.45,
}
DEFAULT_MAX_FINANCIAL_CONFLICT_RATE = {
    "US": 0.10,
    "HK": 0.10,
}


def _cache_file(source, ticker, as_of_date):
    effective_date = as_of_date.isoformat() if isinstance(as_of_date, date) else str(as_of_date or "live")
    key = f"v{CACHE_SCHEMA_VERSION}_{source}_{ticker}_{effective_date}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.pkl")


def _has_data(value):
    if value is None:
        return False
    if isinstance(value, pd.DataFrame):
        return not value.empty
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _read_cache(path):
    try:
        with open(path, "rb") as handle:
            value = pickle.load(handle)
        if not _has_data(value):
            raise ValueError("cached value is empty")
        return value
    except Exception:
        quarantine_path = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, quarantine_path)
        except OSError:
            pass
        return None


def _write_cache_atomic(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def get_cached_or_fetch(
    source,
    ticker,
    fetch_func,
    *,
    as_of_date=None,
    expire_hours=24,
    max_stale_hours=24 * 7,
):
    """Use an effective-date-isolated cache and never return unbounded stale data."""
    cache_path = _cache_file(source, ticker, as_of_date)
    stale_data = None
    cache_age_hours = None
    if os.path.exists(cache_path):
        cache_age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600.0
        cached = _read_cache(cache_path)
        if cached is not None and cache_age_hours < expire_hours:
            return cached
        if cached is not None and cache_age_hours <= max_stale_hours:
            stale_data = cached

    try:
        fresh = fetch_func()
        if not _has_data(fresh):
            raise ValueError("provider returned empty data")
        _write_cache_atomic(cache_path, fresh)
        return fresh
    except Exception:
        if stale_data is not None:
            return stale_data
        raise


@dataclass(frozen=True)
class QuarterlyFinancialAssessment:
    usable: bool
    reason: str = ""
    latest_qoq_dual_growth: bool = False
    reporting_frequency: str = ""
    required_growth_intervals: int = 0
    latest_period: str = ""
    previous_period: str = ""
    latest_revenue_yoy: float = None
    latest_net_income_yoy: float = None


def _infer_reporting_frequency(dated_columns, declared_frequency=""):
    if len(dated_columns) < 2:
        return None
    first_gap = (dated_columns[0][0] - dated_columns[1][0]).days
    inferred = None
    if 60 <= first_gap <= 120:
        inferred = "quarterly"
    elif 140 <= first_gap <= 230:
        inferred = "semiannual"
    if declared_frequency in {"quarterly", "semiannual"}:
        return declared_frequency if inferred in {None, declared_frequency} else None
    return inferred


def wrap_yahoo_statement(ticker, frame, *, as_of_date):
    """Attach explicit aggregator provenance without pretending Yahoo is a filing."""
    from free_financials import FinancialDataUnavailableError

    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        raise FinancialDataUnavailableError("Yahoo returned an empty statement")
    wrapped = frame.copy()
    dated_columns = []
    for column in wrapped.columns:
        parsed = pd.to_datetime(column, errors="coerce")
        if not pd.isna(parsed) and parsed.date() <= as_of_date:
            dated_columns.append((parsed.date(), column))
    dated_columns = sorted(dated_columns, key=lambda item: item[0], reverse=True)
    frequency = _infer_reporting_frequency(dated_columns)
    if frequency is None:
        raise FinancialDataUnavailableError(
            "Cannot infer Yahoo statement reporting frequency"
        )
    wrapped = wrapped[[column for _date, column in dated_columns]]
    wrapped.attrs.update(
        source="yahoo_finance_unofficial",
        reporting_frequency=frequency,
        source_documents=[f"yfinance://Ticker/{ticker}/quarterly_income_stmt"],
        fetched_at=datetime.now().astimezone().isoformat(),
        provenance_quality="aggregator_unverified",
        point_in_time_safe=False,
    )
    return wrapped


def assess_quarterly_financials(stmt, *, as_of_date: date, market_type: str):
    """Evaluate cadence-aware continuous growth and filing-derived latest YoY."""
    if market_type not in {"US", "HK"}:
        raise ValueError(f"Unsupported market type: {market_type!r}")
    if stmt is None or not isinstance(stmt, pd.DataFrame) or stmt.empty:
        return QuarterlyFinancialAssessment(False, "quarterly_financials_empty")
    required_rows = {"Total Revenue", "Net Income"}
    if not required_rows.issubset(stmt.index):
        return QuarterlyFinancialAssessment(
            False, "quarterly_financials_missing_required_rows"
        )

    dated_columns = []
    for column in stmt.columns:
        parsed = pd.to_datetime(column, errors="coerce")
        if pd.isna(parsed):
            continue
        report_date = parsed.date()
        if report_date <= as_of_date:
            dated_columns.append((report_date, column))
    dated_columns = sorted(set(dated_columns), key=lambda item: item[0], reverse=True)
    if len(dated_columns) < 2:
        return QuarterlyFinancialAssessment(
            False, "quarterly_financials_insufficient_periods"
        )

    frequency = _infer_reporting_frequency(
        dated_columns, getattr(stmt, "attrs", {}).get("reporting_frequency", "")
    )
    if frequency == "quarterly":
        frequency = "quarterly"
        minimum_gap, maximum_gap = 60, 120
        required_intervals = 3
        yoy_lag = 4
    elif frequency == "semiannual":
        frequency = "semiannual"
        minimum_gap, maximum_gap = 140, 230
        required_intervals = 2
        yoy_lag = 2
    else:
        return QuarterlyFinancialAssessment(
            False, "quarterly_financials_unsupported_frequency"
        )

    required_periods = required_intervals + 1
    if len(dated_columns) < required_periods:
        return QuarterlyFinancialAssessment(
            False,
            "quarterly_financials_insufficient_periods",
            reporting_frequency=frequency,
            required_growth_intervals=required_intervals,
        )
    selected = dated_columns[:required_periods]
    gaps = [
        (selected[index][0] - selected[index + 1][0]).days
        for index in range(required_intervals)
    ]
    if any(gap < minimum_gap or gap > maximum_gap for gap in gaps):
        return QuarterlyFinancialAssessment(
            False,
            "quarterly_financials_nonconsecutive_periods",
            reporting_frequency=frequency,
            required_growth_intervals=required_intervals,
        )

    generic_max_age = os.environ.get("US_HK_FINANCIAL_MAX_AGE_DAYS")
    if generic_max_age is not None:
        max_age_days = int(generic_max_age)
    elif frequency == "quarterly":
        max_age_days = int(os.environ.get("US_HK_QUARTERLY_MAX_AGE_DAYS", "180"))
    else:
        max_age_days = int(os.environ.get("US_HK_SEMIANNUAL_MAX_AGE_DAYS", "270"))
    if max_age_days <= 0:
        raise ValueError("US/HK financial maximum age must be positive")
    if (as_of_date - selected[0][0]).days > max_age_days:
        return QuarterlyFinancialAssessment(
            False,
            "quarterly_financials_stale",
            reporting_frequency=frequency,
            required_growth_intervals=required_intervals,
        )

    selected_columns = [column for _date, column in selected]
    revenue = pd.to_numeric(
        stmt.loc["Total Revenue", selected_columns], errors="coerce"
    )
    net_income = pd.to_numeric(
        stmt.loc["Net Income", selected_columns], errors="coerce"
    )
    if revenue.isna().any() or net_income.isna().any():
        return QuarterlyFinancialAssessment(
            False,
            "quarterly_financials_missing_values",
            reporting_frequency=frequency,
            required_growth_intervals=required_intervals,
        )
    continuous_growth = all(
        revenue.iloc[index] > revenue.iloc[index + 1]
        and net_income.iloc[index] > net_income.iloc[index + 1]
        for index in range(required_intervals)
    )
    latest_revenue_yoy = None
    latest_net_income_yoy = None
    if len(dated_columns) > yoy_lag:
        latest_column = dated_columns[0][1]
        year_ago_column = dated_columns[yoy_lag][1]
        latest_revenue = pd.to_numeric(
            pd.Series([stmt.at["Total Revenue", latest_column]]), errors="coerce"
        ).iloc[0]
        prior_revenue = pd.to_numeric(
            pd.Series([stmt.at["Total Revenue", year_ago_column]]), errors="coerce"
        ).iloc[0]
        latest_income = pd.to_numeric(
            pd.Series([stmt.at["Net Income", latest_column]]), errors="coerce"
        ).iloc[0]
        prior_income = pd.to_numeric(
            pd.Series([stmt.at["Net Income", year_ago_column]]), errors="coerce"
        ).iloc[0]
        if pd.notna(latest_revenue) and pd.notna(prior_revenue) and prior_revenue > 0:
            latest_revenue_yoy = (float(latest_revenue) / float(prior_revenue) - 1.0) * 100.0
        if pd.notna(latest_income) and pd.notna(prior_income) and prior_income > 0:
            latest_net_income_yoy = (float(latest_income) / float(prior_income) - 1.0) * 100.0
    return QuarterlyFinancialAssessment(
        True,
        latest_qoq_dual_growth=bool(continuous_growth),
        reporting_frequency=frequency,
        required_growth_intervals=required_intervals,
        latest_period=selected[0][0].isoformat(),
        previous_period=selected[1][0].isoformat(),
        latest_revenue_yoy=latest_revenue_yoy,
        latest_net_income_yoy=latest_net_income_yoy,
    )


def passes_growth_valuation(assessment, pe):
    try:
        pe = float(pe)
        revenue_yoy = float(assessment.latest_revenue_yoy)
        income_yoy = float(assessment.latest_net_income_yoy)
    except (TypeError, ValueError):
        return False
    return bool(
        assessment.usable
        and assessment.latest_qoq_dual_growth
        and pe > 0
        and revenue_yoy > pe
        and income_yoy > pe
    )


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
        if status not in {
            "accepted",
            "rejected",
            "strategy_rejected",
            "financial_unavailable",
            "financial_conflict",
            "source_error",
        }:
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
        default_attempted = status in {
            "accepted",
            "financial_unavailable",
            "financial_conflict",
        }
        default_usable = status == "accepted"
        financial_attempted = raw.get("financial_attempted", default_attempted)
        financial_usable = raw.get("financial_usable", default_usable)
        if not isinstance(financial_attempted, bool) or not isinstance(
            financial_usable, bool
        ):
            raise ValueError(
                f"US/HK fixture {ticker} financial flags must be boolean"
            )
        if financial_usable and not financial_attempted:
            raise ValueError(
                f"US/HK fixture {ticker} cannot be usable without an attempt"
            )
        validated[ticker] = FetchOutcome(
            ticker,
            status,
            row=row,
            reason=reason,
            financial_attempted=financial_attempted,
            financial_usable=financial_usable,
        )
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
    financial_attempted = False
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
            
            net_margin = info.get("profitMargins")
            if net_margin is not None:
                net_margin = net_margin * 100
    
            # ---- Early Reject Logic ----
            valuation_val = (pe * (pb - 1) / pb) if pe and pb and pb != 0 else None
            pass_growth_fundamentals = bool(
                market_cap is not None
                and market_cap / 1e8 > args.market_cap_min_yi
                and pe is not None
                and pe > 0
            )
            if not pass_growth_fundamentals:
                return FetchOutcome(
                    ticker=ticker_symbol,
                    status="strategy_rejected",
                    reason="fundamental_precheck",
                    financial_attempted=False,
                    financial_usable=False,
                )

            financial_attempted = True
            try:
                from free_financials import load_free_financial_statement
                import sec_financials
                import hkex_financials
                from core.clock import clock

                market = "HK" if ticker_symbol.upper().endswith(".HK") else "US"

                def primary_loader(ticker, as_of_date):
                    def fetch_primary():
                        if market == "US":
                            return sec_financials.load_sec_financials(ticker, as_of_date)
                        return hkex_financials.load_hkex_financials(ticker, as_of_date)

                    obs = get_cached_or_fetch(
                        "SEC" if market == "US" else "HKEX",
                        ticker,
                        fetch_primary,
                        as_of_date=as_of_date,
                        expire_hours=24,
                    )
                    from free_financials import (
                        FinancialDataUnavailableError,
                        normalize_cumulative_observations_detailed,
                        observations_to_dataframe,
                    )
                    normalization = normalize_cumulative_observations_detailed(obs)
                    normalization.raise_for_blocking_issues()
                    frame = observations_to_dataframe(
                        normalization.observations,
                        normalization_diagnostics=normalization,
                    )
                    if frame.empty:
                        raise FinancialDataUnavailableError(
                            "Official filing observations did not form a usable statement"
                        )
                    return frame

                def yahoo_loader(ticker):
                    from free_financials import FinancialDataUnavailableError

                    try:
                        stmt_frame = get_cached_or_fetch(
                            "YAHOO_STMT",
                            ticker,
                            lambda: fetch_yf_quarterly_income_stmt_cached(ticker),
                            as_of_date=clock.today(),
                            expire_hours=24 * 30,
                        )
                    except ValueError as exc:
                        if str(exc) != "provider returned empty data":
                            raise
                        raise FinancialDataUnavailableError(
                            "Yahoo returned an empty statement"
                        ) from exc
                    return wrap_yahoo_statement(
                        ticker, stmt_frame, as_of_date=clock.today()
                    )

                stmt = load_free_financial_statement(
                    ticker_symbol,
                    clock.today(),
                    primary_loader=primary_loader,
                    yahoo_loader=yahoo_loader
                )
            except Exception as error:
                from free_financials import (
                    FinancialConflictError,
                    FinancialDataUnavailableError,
                )

                if isinstance(error, FinancialConflictError):
                    return FetchOutcome(
                        ticker=ticker_symbol,
                        status="financial_conflict",
                        reason=f"financial_conflict:{error}",
                        financial_attempted=True,
                        financial_usable=False,
                    )
                if isinstance(error, FinancialDataUnavailableError):
                    return FetchOutcome(
                        ticker=ticker_symbol,
                        status="financial_unavailable",
                        reason=f"financial_data_unavailable:{error}",
                        financial_attempted=True,
                        financial_usable=False,
                    )
                return FetchOutcome(
                    ticker=ticker_symbol,
                    status="source_error",
                    reason=(
                        "quarterly_financials_source_error:"
                        f"{type(error).__name__}: {error}"
                    ),
                    financial_attempted=True,
                    financial_usable=False,
                )
            from core.clock import clock

            financials = assess_quarterly_financials(
                stmt,
                as_of_date=clock.today(),
                market_type="HK" if ticker_symbol.upper().endswith(".HK") else "US",
            )
            if not financials.usable:
                return FetchOutcome(
                    ticker=ticker_symbol,
                    status="financial_unavailable",
                    reason=financials.reason,
                    financial_attempted=True,
                    financial_usable=False,
                )
            if not financials.latest_qoq_dual_growth:
                return FetchOutcome(
                    ticker=ticker_symbol,
                    status="strategy_rejected",
                    reason="continuous_report_growth_not_met",
                    financial_attempted=True,
                    financial_usable=True,
                )
            if not passes_growth_valuation(financials, pe):
                return FetchOutcome(
                    ticker=ticker_symbol,
                    status="strategy_rejected",
                    reason="latest_statement_yoy_not_above_pe",
                    financial_attempted=True,
                    financial_usable=True,
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
                "净利润同比增长率": financials.latest_net_income_yoy,
                "营业总收入同比增长率": financials.latest_revenue_yoy,
                "最新单季环比双增": financials.latest_qoq_dual_growth,
                "财报披露频率": financials.reporting_frequency,
                "连续环比增长次数": financials.required_growth_intervals,
                "最新财务报告期": financials.latest_period,
                "上一财务报告期": financials.previous_period,
                "资产负债率": debt_to_asset,
                "最新价": info.get("currentPrice") or info.get("previousClose"),
                "所处行业": info.get("sector"),
                "财报来源": getattr(stmt, "attrs", {}).get("source", "unknown"),
                "财报源文档": ", ".join(getattr(stmt, "attrs", {}).get("source_documents", []))
            }, financial_attempted=True, financial_usable=True)
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt + random.uniform(0, 1))
            else:
                print(f"Failed to fetch {ticker_symbol} after {max_retries} attempts: {e}")
                return FetchOutcome(
                    ticker=ticker_symbol,
                    status="source_error",
                    reason=f"{type(e).__name__}: {e}",
                    financial_attempted=financial_attempted,
                    financial_usable=False,
                )

from tqdm import tqdm


def _run_fetches_in_bounded_processes(tickers, args, *, max_workers, deadline):
    """Run provider calls in killable workers so a stuck curl cannot outlive the stage."""
    start_method = os.environ.get("US_HK_WORKER_START_METHOD", "spawn")
    try:
        context = multiprocessing.get_context(start_method)
    except ValueError as error:
        raise ValueError(
            f"Unsupported US_HK_WORKER_START_METHOD: {start_method}"
        ) from error

    pool = context.Pool(processes=max_workers)
    pending = {
        ticker: pool.apply_async(fetch_yf_data, (ticker, args))
        for ticker in tickers
    }
    completed = []
    deadline_at = time.monotonic() + deadline
    terminate = False
    try:
        while pending and time.monotonic() < deadline_at:
            made_progress = False
            for ticker, result in list(pending.items()):
                if not result.ready():
                    continue
                made_progress = True
                del pending[ticker]
                try:
                    completed.append(result.get(timeout=0))
                except Exception as error:
                    completed.append(
                        FetchOutcome(
                            ticker,
                            "source_error",
                            reason=f"{type(error).__name__}: {error}",
                        )
                    )
            if not made_progress and pending:
                time.sleep(min(0.05, max(0.0, deadline_at - time.monotonic())))

        if pending:
            terminate = True
            completed.extend(
                FetchOutcome(ticker, "source_error", reason="stage_timeout")
                for ticker in pending
            )
        return completed
    finally:
        if terminate:
            # Thread workers cannot be stopped while curl/yfinance is blocked.
            # A process pool gives the stage a real wall-clock upper bound.
            pool.terminate()
        else:
            pool.close()
        pool.join()


def _stage_timeout_seconds(ticker_count, max_workers, market_type):
    if ticker_count < 0 or max_workers <= 0:
        raise ValueError("US/HK ticker count and worker count are invalid")
    if market_type not in {"US", "HK"}:
        raise ValueError(f"Unsupported US/HK market type: {market_type}")
    floor_raw = os.environ.get(
        f"US_HK_{market_type}_STAGE_TIMEOUT_SECONDS",
        os.environ.get(
            "US_HK_STAGE_TIMEOUT_SECONDS",
            str(DEFAULT_US_HK_STAGE_TIMEOUT_SECONDS),
        ),
    )
    default_per_ticker = (
        DEFAULT_US_SECONDS_PER_TICKER_BUDGET
        if market_type == "US"
        else DEFAULT_HK_SECONDS_PER_TICKER_BUDGET
    )
    per_ticker_raw = os.environ.get(
        f"US_HK_{market_type}_SECONDS_PER_TICKER_BUDGET",
        str(default_per_ticker),
    )
    floor = float(floor_raw)
    per_ticker = float(per_ticker_raw)
    if floor <= 0 or per_ticker <= 0:
        raise ValueError("US/HK stage and per-ticker budgets must be positive")
    batches = (ticker_count + max_workers - 1) // max_workers
    return max(floor, batches * per_ticker)


def _canonical_outcome_status(outcome):
    if outcome.status != "rejected":
        return outcome.status
    if str(outcome.reason).startswith(
        ("quarterly_financials_", "financial_data_unavailable:")
    ):
        return "financial_unavailable"
    return "strategy_rejected"


def _outcome_financial_attempted(outcome, status):
    if outcome.financial_attempted is not None:
        return bool(outcome.financial_attempted)
    if status in {"accepted", "financial_unavailable", "financial_conflict"}:
        return True
    return status == "strategy_rejected" and outcome.reason != "fundamental_precheck"


def _outcome_financial_usable(outcome, status):
    if outcome.financial_usable is not None:
        return bool(outcome.financial_usable)
    return status in {"accepted", "strategy_rejected"} and (
        outcome.reason != "fundamental_precheck"
    )


def _build_screen_health(outcomes, market_type, *, attempted):
    canonical = [(outcome, _canonical_outcome_status(outcome)) for outcome in outcomes]
    status_counts = Counter(status for _outcome, status in canonical)
    source_errors = status_counts["source_error"]
    strategy_decisions = (
        status_counts["accepted"] + status_counts["strategy_rejected"]
    )
    financial_attempted = sum(
        _outcome_financial_attempted(outcome, status)
        for outcome, status in canonical
    )
    financial_usable = sum(
        _outcome_financial_usable(outcome, status)
        for outcome, status in canonical
    )
    financial_unavailable = status_counts["financial_unavailable"]
    financial_conflicts = status_counts["financial_conflict"]
    transport_coverage = (
        (attempted - source_errors) / attempted if attempted else 1.0
    )
    decision_coverage = strategy_decisions / attempted if attempted else 1.0
    financial_usable_coverage = (
        financial_usable / financial_attempted if financial_attempted else 1.0
    )
    financial_conflict_rate = (
        financial_conflicts / financial_attempted if financial_attempted else 0.0
    )

    def reasons_for(status_name):
        return dict(
            sorted(
                Counter(
                    outcome.reason or "unspecified"
                    for outcome, status in canonical
                    if status == status_name
                ).items()
            )
        )

    reporting_frequencies = dict(
        sorted(
            Counter(
                str(outcome.row.get("财报披露频率"))
                for outcome, status in canonical
                if status == "accepted"
                and isinstance(outcome.row, dict)
                and outcome.row.get("财报披露频率")
            ).items()
        )
    )
    return {
        "market": market_type,
        "attempted": attempted,
        # Compatibility fields now use honest decision/transport semantics.
        "evaluated": strategy_decisions,
        "accepted": status_counts["accepted"],
        "rejected": (
            status_counts["strategy_rejected"]
            + financial_unavailable
            + financial_conflicts
        ),
        "source_errors": source_errors,
        "source_error_reasons": reasons_for("source_error"),
        "coverage": transport_coverage,
        "transport_coverage": transport_coverage,
        "decision_coverage": decision_coverage,
        "strategy_rejected": status_counts["strategy_rejected"],
        "strategy_rejection_reasons": reasons_for("strategy_rejected"),
        "financial_attempted": financial_attempted,
        "financial_usable": financial_usable,
        "financial_usable_coverage": financial_usable_coverage,
        "financial_unavailable": financial_unavailable,
        "financial_unavailable_reasons": reasons_for("financial_unavailable"),
        "financial_conflicts": financial_conflicts,
        "financial_conflict_rate": financial_conflict_rate,
        "financial_conflict_reasons": reasons_for("financial_conflict"),
        # Retained for report consumers that have not migrated yet.
        "rejection_reasons": {
            **reasons_for("strategy_rejected"),
            **reasons_for("financial_unavailable"),
            **reasons_for("financial_conflict"),
        },
        "accepted_reporting_frequencies": reporting_frequencies,
    }


def _coverage_setting(name, market_type, *, fallback=None):
    market_name = f"US_HK_{market_type}_{name}"
    generic_name = f"US_HK_{name}"
    raw = os.environ.get(market_name, os.environ.get(generic_name, fallback))
    if raw is None:
        return None
    value = float(raw)
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{market_name}/{generic_name} must be between 0 and 1")
    return value


def _enforce_screen_health(health):
    market_type = health["market"]
    minimum_transport = _coverage_setting(
        "MIN_TRANSPORT_COVERAGE",
        market_type,
        fallback=os.environ.get("US_HK_MIN_DATA_COVERAGE", "0.80"),
    )
    if health["attempted"] and health["transport_coverage"] < minimum_transport:
        raise ConnectionError(
            f"{market_type} transport coverage "
            f"{health['transport_coverage']:.1%} is below "
            f"the required {minimum_transport:.1%}"
        )

    minimum_financial = _coverage_setting(
        "MIN_FINANCIAL_USABLE_COVERAGE",
        market_type,
        fallback=str(DEFAULT_MIN_FINANCIAL_USABLE_COVERAGE[market_type]),
    )
    if (
        minimum_financial is not None
        and health["financial_attempted"]
        and health["financial_usable_coverage"] < minimum_financial
    ):
        raise ConnectionError(
            f"{market_type} financial usable coverage "
            f"{health['financial_usable_coverage']:.1%} is below "
            f"the required {minimum_financial:.1%}"
        )

    maximum_conflicts = _coverage_setting(
        "MAX_FINANCIAL_CONFLICT_RATE",
        market_type,
        fallback=str(DEFAULT_MAX_FINANCIAL_CONFLICT_RATE[market_type]),
    )
    if (
        maximum_conflicts is not None
        and health["financial_attempted"]
        and health["financial_conflict_rate"] > maximum_conflicts
    ):
        raise ConnectionError(
            f"{market_type} financial conflict rate "
            f"{health['financial_conflict_rate']:.1%} exceeds "
            f"the allowed {maximum_conflicts:.1%}"
        )


def screen_us_hk(tickers, args, market_type="US"):
    frames = []
    tickers = list(dict.fromkeys(str(ticker) for ticker in tickers))
    outcomes = []
    if tickers:
        max_workers = min(
            len(tickers),
            int(os.environ.get("US_HK_MAX_WORKERS", str(DEFAULT_US_HK_MAX_WORKERS))),
        )
        deadline = _stage_timeout_seconds(len(tickers), max_workers, market_type)
        if max_workers <= 0 or deadline <= 0:
            raise ValueError("US/HK worker count and stage timeout must be positive")
        raw_outcomes = _run_fetches_in_bounded_processes(
            tickers,
            args,
            max_workers=max_workers,
            deadline=deadline,
        )
        for res in tqdm(
            raw_outcomes,
            total=len(tickers),
            desc=f"Scanning {market_type} stocks",
        ):
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
                        "unknown",
                        "source_error",
                        reason="invalid outcome",
                    )
                )

    attempted = len(tickers)
    health = _build_screen_health(outcomes, market_type, attempted=attempted)
    LAST_SCREEN_HEALTH[market_type] = health
    print(f"{market_type} data health: {health}")
    _enforce_screen_health(health)

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
        & df["PE"].notna() & (df["PE"] > 0)
        & (df["PE"] < df["净利润同比增长率"])
        & (df["PE"] < df["营业总收入同比增长率"])
    )
    
    df_growth = df[mask_gro].copy()
    
    return df_div, df_growth
