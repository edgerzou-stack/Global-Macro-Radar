import os
import sqlite3
import math
import json
import hashlib
import re
import time as time_module
import pandas as pd
from core.data_anomaly import DataAnomalyError
from core.market_data_adapters import (
    A_SHARE_CLOSE_HTTP_TIMEOUT,
    BAOSTOCK_SOCKET_TIMEOUT_SECONDS,
    SINA_HTTP_TIMEOUT,
    SINA_A_SHARE_QUOTE_URL,
    TENCENT_A_SHARE_KLINE_URL,
    AShareQuoteAdapter,
    MarketDataAdapters,
    _bounded_baostock_session,
    _call_sina_with_bounded_http,
    baostock_constants,
    baostock_context,
    baostock_socket_util,
)
from core.market_data_contracts import (
    CircuitBreakerError,
    DataIntegrityError,
    FatalSystemError,
    InvalidMarketDataRequest,
)
from core.market_data_normalization import (
    require_exact_close_range,
    validate_closing_prices,
    validate_prices,
)
from core.market_symbols import (
    to_baostock_symbol,
    to_sina_symbol,
    to_tencent_symbol,
    to_yfinance_symbol,
)
from core.provider_errors import log_provider_error
from core.run_telemetry import metric_line

import datetime
import requests
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)
HISTORICAL_PRICE_FIXTURE_SCHEMA_VERSION = 1
A_SHARE_CLOSE_FALLBACK_ERROR_TTL_SECONDS = 30.0
A_SHARE_CLOSE_CACHE_POLICY_VERSION = "validated-close-pair-v1"
A_SHARE_CLOSE_RELATIVE_TOLERANCE = 0.001


def _emit_a_share_close_cache_metric(**counters):
    print(
        metric_line(
            "a_share_close_pair_cache",
            counters,
            dimensions={
                "policy_version": A_SHARE_CLOSE_CACHE_POLICY_VERSION
            },
        ),
        flush=True,
    )

@lru_cache(maxsize=8)
def _load_historical_fixture_cached(path, mtime_ns):
    del mtime_ns  # File rewrites produce a distinct cache key.
    try:
        with open(path, "r", encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise DataIntegrityError(
            f"Cannot load historical price fixture {path}: {error}"
        ) from error
    if not isinstance(fixture, dict):
        raise DataIntegrityError("historical price fixture must be an object")
    if set(fixture) != {"schema_version", "series"}:
        raise DataIntegrityError(
            "historical price fixture has invalid top-level fields"
        )
    if fixture.get("schema_version") != HISTORICAL_PRICE_FIXTURE_SCHEMA_VERSION:
        raise DataIntegrityError("unsupported historical price fixture schema_version")
    series = fixture.get("series")
    if not isinstance(series, list):
        raise DataIntegrityError("historical price fixture series must be a list")

    result = {}
    for index, entry in enumerate(series):
        if not isinstance(entry, dict) or set(entry) != {
            "symbol", "adjust", "start_date", "end_date", "rows"
        }:
            raise DataIntegrityError(
                f"historical price fixture series {index} has invalid fields"
            )
        symbol = entry.get("symbol")
        adjust = entry.get("adjust")
        coverage_start = str(entry.get("start_date", "")).replace("-", "")
        coverage_end = str(entry.get("end_date", "")).replace("-", "")
        rows = entry.get("rows")
        if not isinstance(symbol, str) or not symbol.strip():
            raise DataIntegrityError(
                f"historical price fixture series {index} has invalid symbol"
            )
        if adjust not in {"", "qfq", "hfq"}:
            raise DataIntegrityError(
                f"historical price fixture series {index} has invalid adjust"
            )
        try:
            if (
                not re.fullmatch(r"\d{8}", coverage_start)
                or not re.fullmatch(r"\d{8}", coverage_end)
                or coverage_start > coverage_end
            ):
                raise ValueError
            datetime.datetime.strptime(coverage_start, "%Y%m%d")
            datetime.datetime.strptime(coverage_end, "%Y%m%d")
        except ValueError as error:
            raise DataIntegrityError(
                f"historical price fixture series {index} has invalid coverage dates"
            ) from error
        if not isinstance(rows, list) or not rows:
            raise DataIntegrityError(
                f"historical price fixture series {index} rows must be non-empty"
            )
        key = (symbol, adjust)
        if key in result:
            raise DataIntegrityError(
                f"duplicate historical price fixture series for {symbol}/{adjust}"
            )
        result[key] = {
            "start_date": coverage_start,
            "end_date": coverage_end,
            "rows": rows,
        }
    return result

class CircuitBreaker:
    def __init__(self, name: str, threshold: int = 2):
        self.name = name
        self.threshold = threshold
        self.failures = 0
        self.tripped = False
        
    def record_failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            if not self.tripped:
                logger.error(f"[CIRCUIT BREAKER TRIPPED] Data source '{self.name}' has failed {self.failures} times sequentially. Bypassing it entirely for this run.")
            self.tripped = True
            
    def record_success(self):
        # Only reset if not tripped. Once tripped, it stays tripped for the run.
        if not self.tripped:
            self.failures = 0

class DataGateway:
    # Deprecated compatibility aliases. Runtime requests use instance-scoped
    # breakers so one test or pipeline run cannot poison another run.
    CB_BAOSTOCK = CircuitBreaker("baostock", threshold=10)
    CB_SINA = CircuitBreaker("sina_akshare", threshold=10)
    CB_YFINANCE = CircuitBreaker("yfinance", threshold=10)

    def __init__(self, db_path=None):
        self.historical_fixture_path = os.environ.get("HISTORICAL_PRICE_FIXTURE")
        if db_path is None:
            if self.historical_fixture_path:
                db_path = None
            else:
                scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                cache_dir = os.path.join(scripts_dir, ".cache")
                os.makedirs(cache_dir, exist_ok=True)
                db_path = os.path.join(cache_dir, "market_data_cache.db")
        
        self.db_path = db_path
        self.breakers = {
            "baostock": CircuitBreaker("baostock", threshold=10),
            "sina": CircuitBreaker("sina_akshare", threshold=10),
            "yfinance": CircuitBreaker("yfinance", threshold=10),
        }
        self._a_share_close_fallback_cache = {}
        self._a_share_close_fallback_errors = {}
        self.adapters = MarketDataAdapters(
            ensure_source_available=lambda source: (
                self._ensure_source_available(source)
            ),
            record_success=lambda source: self.breakers[
                source
            ].record_success(),
            record_failure=lambda source: self.breakers[
                source
            ].record_failure(),
            logger=logger,
        )
        self.a_share_quote_adapter = AShareQuoteAdapter(
            validate_prices=self._validate_prices,
        )
        if self.db_path is not None:
            self._init_db()

    def _get_from_historical_fixture(
        self, symbol: str, start_date: str, end_date: str, adjust: str
    ) -> pd.DataFrame:
        path = os.path.abspath(os.fspath(self.historical_fixture_path))
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError as error:
            raise DataIntegrityError(
                f"Cannot stat historical price fixture {path}: {error}"
            ) from error
        series = _load_historical_fixture_cached(path, mtime_ns)
        key = (symbol, adjust)
        if key not in series:
            raise DataIntegrityError(
                f"historical price fixture has no exact series for {symbol}/{adjust}"
            )
        fixture_series = series[key]
        frame = self._validate_prices(pd.DataFrame(fixture_series["rows"]), symbol)
        parsed_fixture_dates = pd.to_datetime(
            frame["日期"], format="%Y%m%d", errors="coerce"
        )
        if parsed_fixture_dates.isna().any():
            raise DataIntegrityError(
                f"historical price fixture contains invalid dates for {symbol}/{adjust}"
            )
        if (
            (frame["日期"] < fixture_series["start_date"])
            | (frame["日期"] > fixture_series["end_date"])
        ).any():
            raise DataIntegrityError(
                f"historical price fixture rows exceed declared coverage for "
                f"{symbol}/{adjust}"
            )
        if (
            start_date < fixture_series["start_date"]
            or end_date > fixture_series["end_date"]
        ):
            raise DataIntegrityError(
                f"historical price fixture does not cover {symbol}/{adjust} "
                f"range {start_date}-{end_date}; "
                f"available={fixture_series['start_date']}-{fixture_series['end_date']}"
            )
        result = frame[
            (frame["日期"] >= start_date) & (frame["日期"] <= end_date)
        ].copy()
        if result.empty:
            raise DataIntegrityError(
                f"historical price fixture has no rows for {symbol}/{adjust} "
                f"range {start_date}-{end_date}"
            )
        return result

    def _ensure_source_available(self, source: str):
        breaker = self.breakers[source]
        if breaker.tripped:
            raise CircuitBreakerError(
                f"Data source '{breaker.name}' is disabled for this gateway run"
            )

    def _call_source(self, source: str, fetch, *args, **kwargs):
        # Check outside the adapter too, so mocks and future adapters cannot
        # accidentally bypass a tripped breaker.
        self._ensure_source_available(source)
        return fetch(*args, **kwargs)

    @staticmethod
    def _validate_prices(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        return validate_prices(df, symbol)

    @staticmethod
    def _validate_closing_prices(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Validate a close-only valuation view without accepting bad closes.

        Some upstream HK rows contain an invalid opening auction field while
        their close remains bounded by the reported high/low.  NAV never uses
        the open, so it may isolate that field; close/high/low integrity stays
        mandatory and the degraded row is never written to the OHLC cache.
        """
        return validate_closing_prices(df, symbol)

    @staticmethod
    def _require_exact_close_range(
        frame: pd.DataFrame, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        return require_exact_close_range(
            frame,
            symbol,
            start_date,
            end_date,
        )

    def _init_db(self):
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            c = conn.cursor()
            c.execute('PRAGMA journal_mode=WAL;')
            c.execute("""
                CREATE TABLE IF NOT EXISTS daily_prices (
                    symbol TEXT,
                    date TEXT,
                    open REAL,
                    close REAL,
                    high REAL,
                    low REAL,
                    volume REAL,
                    adjust TEXT,
                    PRIMARY KEY (symbol, date, adjust)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS quarantined_daily_prices (
                    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL,
                    close REAL,
                    high REAL,
                    low REAL,
                    volume REAL,
                    adjust TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    quarantined_at TEXT NOT NULL
                )
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_quarantined_daily_prices_lookup
                ON quarantined_daily_prices (symbol, adjust, date)
            """)
            conn.commit()

    def _to_yf_symbol(self, symbol: str) -> str:
        return to_yfinance_symbol(symbol)

    def _to_bs_symbol(self, symbol: str) -> str:
        """Converts internal symbol (like 600519) to baostock symbol (sh.600519)."""
        return to_baostock_symbol(symbol)

    def _to_sina_symbol(self, symbol: str) -> str:
        """Converts internal symbol (like 600519) to sina symbol (sh600519)."""
        return to_sina_symbol(symbol)

    def _to_tencent_symbol(self, symbol: str) -> str:
        """Convert an internal A-share code to Tencent's market prefix."""
        return to_tencent_symbol(symbol)

    @staticmethod
    def _quote_number(value, field, symbol):
        return AShareQuoteAdapter._quote_number(value, field, symbol)

    def _fetch_tencent_a_share_daily(
        self, symbol: str, start_date: str, end_date: str, adjust: str
    ):
        return self.a_share_quote_adapter.fetch_tencent_daily(
            symbol,
            start_date,
            end_date,
            adjust,
        )

    def _fetch_sina_a_share_quote(self, symbol: str):
        return self.a_share_quote_adapter.fetch_sina_quote(symbol)

    @staticmethod
    def _relative_gap(left, right):
        return abs(float(left) - float(right)) / max(float(left), float(right))

    def _validate_a_share_close_quote(
        self, quote, symbol: str, end_date: str, source: str
    ):
        if str(quote.get("symbol", "")).zfill(6) != str(symbol).zfill(6):
            raise DataIntegrityError(
                f"{source} close snapshot symbol mismatch for {symbol}"
            )
        if str(quote.get("date", "")).replace("-", "") != end_date:
            raise DataIntegrityError(
                f"{source} close snapshot date mismatch for {symbol}"
            )
        try:
            quote_time = datetime.time.fromisoformat(str(quote.get("time", "")))
        except ValueError as error:
            raise DataIntegrityError(
                f"{source} close snapshot has invalid time for {symbol}"
            ) from error
        if quote_time < datetime.time(15, 0):
            raise DataIntegrityError(
                f"{source} close snapshot is not after-close for {symbol}"
            )

        validated = {
            key: self._quote_number(quote.get(key), key.replace("_", " "), symbol)
            for key in ("open", "close", "high", "low", "previous_close")
        }
        if (
            validated["low"] > validated["open"]
            or validated["low"] > validated["close"]
            or validated["high"] < validated["open"]
            or validated["high"] < validated["close"]
            or validated["low"] > validated["high"]
        ):
            raise DataIntegrityError(
                f"{source} close snapshot has inconsistent OHLC for {symbol}"
            )
        return validated

    def _get_validated_a_share_close_pair(
        self, symbol: str, start_date: str, end_date: str
    ):
        """Return cross-checked raw/HFQ closes for a just-completed session.

        Tencent commonly publishes the raw final bar before its HFQ endpoint is
        refreshed.  Extending yesterday's factor is safe only when two sources
        agree on today's close and the exchange reference previous close still
        equals yesterday's raw close.  A corporate-action day therefore fails
        closed instead of silently carrying the old factor forward.
        """
        cache_key = (str(symbol), start_date, end_date)
        cached = self._a_share_close_fallback_cache.get(cache_key)
        if cached is not None:
            _emit_a_share_close_cache_metric(hit=1, saved_external_calls=3)
            return {key: value.copy() for key, value in cached.items()}
        errors = getattr(self, "_a_share_close_fallback_errors", {})
        self._a_share_close_fallback_errors = errors
        cached_error = errors.get(cache_key)
        if cached_error is not None:
            failed_at, error_text = cached_error
            if (
                time_module.monotonic() - failed_at
                < A_SHARE_CLOSE_FALLBACK_ERROR_TTL_SECONDS
            ):
                _emit_a_share_close_cache_metric(
                    negative_hit=1,
                    saved_external_calls=3,
                )
                raise DataIntegrityError(
                    f"cached A-share close-pair failure for {symbol}: {error_text}"
                )
            errors.pop(cache_key, None)

        _emit_a_share_close_cache_metric(miss=1)
        try:
            raw, tencent_quote = self._fetch_tencent_a_share_daily(
                symbol, start_date, end_date, ""
            )
            hfq, hfq_quote = self._fetch_tencent_a_share_daily(
                symbol, start_date, end_date, "hfq"
            )
            sina_quote = self._fetch_sina_a_share_quote(symbol)
        except (DataIntegrityError, requests.RequestException, ValueError) as error:
            errors[cache_key] = (
                time_module.monotonic(),
                f"{type(error).__name__}: {error}",
            )
            _emit_a_share_close_cache_metric(negative_write=1)
            raise

        tencent_values = self._validate_a_share_close_quote(
            tencent_quote, symbol, end_date, "Tencent"
        )
        hfq_quote_values = self._validate_a_share_close_quote(
            hfq_quote, symbol, end_date, "Tencent HFQ"
        )
        sina_values = self._validate_a_share_close_quote(
            sina_quote, symbol, end_date, "Sina"
        )
        for field in ("close", "previous_close"):
            if self._relative_gap(tencent_values[field], hfq_quote_values[field]) > A_SHARE_CLOSE_RELATIVE_TOLERANCE:
                raise DataIntegrityError(
                    f"Tencent quote envelopes disagree on {field.replace('_', ' ')} "
                    f"for {symbol}"
                )
            if self._relative_gap(tencent_values[field], sina_values[field]) > A_SHARE_CLOSE_RELATIVE_TOLERANCE:
                label = "cross-source close" if field == "close" else "cross-source previous close"
                raise DataIntegrityError(f"{label} mismatch for {symbol}")

        raw = raw[(raw["日期"] >= start_date) & (raw["日期"] <= end_date)].copy()
        hfq = hfq[(hfq["日期"] >= start_date) & (hfq["日期"] <= end_date)].copy()
        raw_end = raw[raw["日期"] == end_date]
        if raw_end.empty:
            raise DataIntegrityError(
                f"Tencent raw series has no exact-session close for {symbol}"
            )
        raw_end_close = float(raw_end.iloc[-1]["收盘"])
        if (
            self._relative_gap(raw_end_close, tencent_values["close"])
            > A_SHARE_CLOSE_RELATIVE_TOLERANCE
            or self._relative_gap(raw_end_close, sina_values["close"])
            > A_SHARE_CLOSE_RELATIVE_TOLERANCE
        ):
            raise DataIntegrityError(
                f"cross-source close mismatch for {symbol}"
            )

        raw_prior = raw[raw["日期"] < end_date]
        if raw_prior.empty:
            raise DataIntegrityError(
                f"Cannot verify previous close for {symbol}"
            )
        prior_date = str(raw_prior.iloc[-1]["日期"])
        prior_raw_close = float(raw_prior.iloc[-1]["收盘"])
        previous_close_gap = self._relative_gap(
            prior_raw_close, tencent_values["previous_close"]
        )
        if previous_close_gap > A_SHARE_CLOSE_RELATIVE_TOLERANCE:
            action = tencent_quote.get("corporate_action")
            action_date = (
                str(action.get("cqr", "")).replace("-", "")
                if isinstance(action, dict)
                else ""
            )
            if not action or action_date != end_date:
                raise DataIntegrityError(
                    f"previous close indicates an unverified corporate action "
                    f"for {symbol}"
                )

        if hfq[hfq["日期"] == end_date].empty:
            hfq_prior = hfq[hfq["日期"] == prior_date]
            if hfq_prior.empty:
                raise DataIntegrityError(
                    f"Tencent HFQ series has no factor anchor for {symbol}"
                )
            prior_hfq_close = float(hfq_prior.iloc[-1]["收盘"])
            # The exchange reference previous close, independently confirmed
            # by Tencent and Sina, incorporates any ex-right/ex-dividend event.
            # Using it as the denominator preserves total-return continuity.
            factor = prior_hfq_close / tencent_values["previous_close"]
            if not math.isfinite(factor) or factor <= 0:
                raise DataIntegrityError(
                    f"Invalid HFQ factor anchor for {symbol}"
                )
            end_row = raw_end.iloc[-1].copy()
            for column in ("开盘", "收盘", "最高", "最低"):
                end_row[column] = float(end_row[column]) * factor
            hfq = pd.concat([hfq, end_row.to_frame().T], ignore_index=True)
            hfq = self._validate_prices(hfq, symbol)

        pair = {
            "": raw[["日期", "收盘"]].copy(),
            "hfq": hfq[["日期", "收盘"]].copy(),
        }
        for adjust, frame in pair.items():
            if frame[frame["日期"] == end_date].empty:
                raise DataIntegrityError(
                    f"Validated {adjust or 'raw'} close is missing for {symbol}"
                )
        self._a_share_close_fallback_cache[cache_key] = {
            key: value.copy() for key, value in pair.items()
        }
        _emit_a_share_close_cache_metric(write=1)
        errors.pop(cache_key, None)
        return pair

    
    def verify_extreme_move(self, symbol: str, duration_days: int, entry_price: float, exit_price: float) -> bool:
        """
        Verifies if a price move is mathematically possible given the duration and market limits.
        Raises DataAnomalyError if the move is impossible (indicating mixed adjusted/unadjusted data).
        """
        if entry_price <= 0: return True
        
        ret = (exit_price / entry_price) - 1
        
        is_a_share = len(symbol) == 6 and symbol.isdigit()
        if is_a_share:
            # A-shares have a daily limit of 10% or 20% (STAR/ChiNext).
            # We allow a maximum of 25% per trading day (to account for edge cases / new IPOs / resumptions).
            # Over multiple days, the max return compounds.
            # If duration is very short (e.g. 1-3 days) and return is absurdly high/low, block it.
            max_daily_return = 0.25
            min_daily_return = -0.25
            
            # Simple linear approximation for bounds check to prevent extreme silent corruption
            max_allowed = (1 + max_daily_return) ** duration_days - 1
            min_allowed = (1 + min_daily_return) ** duration_days - 1
            
            # Allow slightly more leniency for compounding, but block obvious anomalies like -92% or +80% in 1 day
            if duration_days <= 5:
                if ret < -0.50 or ret > 0.80:
                    raise DataAnomalyError(f"Impossible A-share return of {ret:.2%} over {duration_days} days for {symbol}. Suspect unadjusted ex-dividend data.")
                    
        return True

    def _get_from_cache(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute('PRAGMA journal_mode=WAL;')
            query = """
                SELECT date as 日期, open as 开盘, close as 收盘, high as 最高, low as 最低, volume as 成交量
                FROM daily_prices
                WHERE symbol = ? AND adjust = ? AND date >= ? AND date <= ?
                ORDER BY date ASC
            """
            df = pd.read_sql_query(query, conn, params=(symbol, adjust, start_date, end_date))
        df.attrs["data_provider"] = "validated_cache"
        return df

    def _quarantine_cached_prices(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjust: str,
        reason: str,
    ) -> int:
        """Atomically retain corrupt cache evidence and remove it from active use."""
        return self._quarantine_cached_price_series(
            symbol,
            start_date,
            end_date,
            (adjust,),
            reason,
        )

    def _quarantine_cached_price_series(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjustments,
        reason: str,
    ) -> int:
        """Atomically quarantine related adjustment series for one symbol.

        A cached OHLC series can be internally valid while belonging to a stale
        corporate-action scale.  Raw and adjusted valuation series therefore
        have to be invalidated together before a consistency retry.
        """
        if self.db_path is None:
            raise DataIntegrityError("Cannot quarantine prices without a cache database")

        adjustments = tuple(dict.fromkeys(str(value) for value in adjustments))
        if not adjustments:
            return 0

        row_count = 0
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("BEGIN IMMEDIATE")
            for adjust in adjustments:
                parameters = (symbol, adjust, start_date, end_date)
                series_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM daily_prices
                    WHERE symbol = ? AND adjust = ? AND date >= ? AND date <= ?
                    """,
                    parameters,
                ).fetchone()[0]
                row_count += series_count
                if series_count:
                    conn.execute(
                        """
                        INSERT INTO quarantined_daily_prices (
                            symbol, date, open, close, high, low, volume, adjust,
                            reason, quarantined_at
                        )
                        SELECT
                            symbol, date, open, close, high, low, volume, adjust,
                            ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        FROM daily_prices
                        WHERE symbol = ? AND adjust = ? AND date >= ? AND date <= ?
                        """,
                        (str(reason), *parameters),
                    )
                    conn.execute(
                        """
                        DELETE FROM daily_prices
                        WHERE symbol = ? AND adjust = ? AND date >= ? AND date <= ?
                        """,
                        parameters,
                    )
            conn.commit()
        return int(row_count)

    def refresh_valuation_closes(
        self, symbol: str, start_date: str, end_date: str, reason: str
    ):
        """Discard a mismatched raw/HFQ cache pair and fetch both series anew."""
        quarantined = self._quarantine_cached_price_series(
            symbol,
            start_date,
            end_date,
            ("", "hfq"),
            reason,
        )
        logger.error(
            "DataGateway: quarantined %s cross-adjustment rows for %s in "
            "%s-%s: %s",
            quarantined,
            symbol,
            start_date,
            end_date,
            reason,
        )
        return {
            adjust: self.get_historical_closes(
                symbol, start_date, end_date, adjust=adjust
            )
            for adjust in ("hfq", "")
        }

    def _fetch_validated_source(self, source: str, fetch, symbol: str, *args):
        """Fetch from one source and reject it before fallback/cache on bad OHLC."""
        try:
            frame = self._call_source(source, fetch, symbol, *args)
            frame = self._validate_prices(frame, symbol)
            frame.attrs["data_provider"] = source
            return frame
        except DataIntegrityError:
            # Fetch adapters record transport success before returning.  A payload
            # that fails integrity is still a source failure for breaker purposes.
            self.breakers[source].record_failure()
            raise

    def _save_to_cache(self, symbol: str, df: pd.DataFrame, adjust: str):
        if df is None or df.empty:
            return

        # Defense in depth: only a fully validated OHLC frame may become active
        # cache state, even if a future caller bypasses get_historical_prices().
        df = self._validate_prices(df, symbol)
            
        if '日期' in df.columns:
            date_series = df['日期'].astype(str).str.replace('-', '').str.slice(0, 8)
            open_series = pd.to_numeric(df.get('开盘', 0.0), errors='coerce').fillna(0.0)
            close_series = pd.to_numeric(df.get('收盘', 0.0), errors='coerce').fillna(0.0)
            high_series = pd.to_numeric(df.get('最高', 0.0), errors='coerce').fillna(0.0)
            low_series = pd.to_numeric(df.get('最低', 0.0), errors='coerce').fillna(0.0)
            vol_series = pd.to_numeric(df.get('成交量', 0.0), errors='coerce').fillna(0.0)
        else:
            date_series = pd.to_datetime(df.index).strftime('%Y%m%d')
            open_series = pd.to_numeric(df.get('Open', 0.0), errors='coerce').fillna(0.0)
            close_series = pd.to_numeric(df.get('Close', 0.0), errors='coerce').fillna(0.0)
            high_series = pd.to_numeric(df.get('High', 0.0), errors='coerce').fillna(0.0)
            low_series = pd.to_numeric(df.get('Low', 0.0), errors='coerce').fillna(0.0)
            vol_series = pd.to_numeric(df.get('Volume', 0.0), errors='coerce').fillna(0.0)

        df_db = pd.DataFrame({
            'symbol': symbol,
            'date': date_series,
            'open': open_series,
            'close': close_series,
            'high': high_series,
            'low': low_series,
            'volume': vol_series,
            'adjust': adjust
        })
        
        df_db = df_db[(df_db['date'] != '') & (df_db['open'] > 0) & (df_db['close'] > 0)]
        
        if not df_db.empty:
            to_insert = list(df_db.itertuples(index=False, name=None))
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                conn.execute('PRAGMA journal_mode=WAL;')
                c = conn.cursor()
                c.executemany("""
                    INSERT OR REPLACE INTO daily_prices 
                    (symbol, date, open, close, high, low, volume, adjust)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, to_insert)
                conn.commit()

    def _fetch_from_baostock(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        return self.adapters.fetch_baostock(
            symbol,
            start_date,
            end_date,
            adjust,
        )

    def _fetch_from_sina(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        return self.adapters.fetch_sina(
            symbol,
            start_date,
            end_date,
            adjust,
        )

    def _fetch_from_yfinance(self, symbol: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
        return self.adapters.fetch_yfinance(
            symbol,
            start_date,
            end_date,
            adjust,
        )

    def get_historical_prices(self, symbol: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
        start_date = str(start_date).replace('-', '')
        end_date = str(end_date).replace('-', '')
        if not re.fullmatch(r"\d{8}", start_date) or not re.fullmatch(
            r"\d{8}", end_date
        ) or start_date > end_date:
            raise InvalidMarketDataRequest(
                f"Invalid historical price range {start_date}-{end_date}"
            )
        try:
            datetime.datetime.strptime(start_date, "%Y%m%d")
            datetime.datetime.strptime(end_date, "%Y%m%d")
        except ValueError as error:
            raise InvalidMarketDataRequest(
                f"Invalid historical price range {start_date}-{end_date}"
            ) from error
        if self.historical_fixture_path:
            return self._get_from_historical_fixture(
                str(symbol), start_date, end_date, adjust
            )
        
        df_cache = self._get_from_cache(symbol, start_date, end_date, adjust)
        
        if not df_cache.empty:
            try:
                df_cache = self._validate_prices(df_cache, symbol)
            except DataIntegrityError as error:
                quarantined = self._quarantine_cached_prices(
                    symbol,
                    start_date,
                    end_date,
                    adjust,
                    str(error),
                )
                logger.error(
                    "DataGateway: quarantined %s corrupt cached rows for "
                    "%s/%s in %s-%s: %s",
                    quarantined,
                    symbol,
                    adjust or "raw",
                    start_date,
                    end_date,
                    error,
                )
                df_cache = pd.DataFrame()

        if not df_cache.empty:
            cache_min = str(df_cache['日期'].min()).replace('-', '')[:8]
            cache_max = str(df_cache['日期'].max()).replace('-', '')[:8]
            if cache_min <= start_date and cache_max >= end_date:
                return df_cache

        df_new = pd.DataFrame()
        is_a_share = len(symbol) == 6 and symbol.isdigit()
        
        if is_a_share:
            # Multi-level Failover Strategy for A-Shares
            try:
                df_new = self._fetch_validated_source(
                    "baostock", self._fetch_from_baostock,
                    symbol, start_date, end_date, adjust
                )
            except Exception as e_bs:
                log_provider_error(
                    logger,
                    e_bs,
                    provider="baostock",
                    operation="historical_prices_fallback",
                    retryable=False,
                    degraded_allowed=True,
                    symbol=symbol,
                    effective_date=end_date,
                )
                logger.warning(f"DataGateway: baostock failed for {symbol}: {e_bs}. Falling back to Sina.")
                try:
                    df_new = self._fetch_validated_source(
                        "sina", self._fetch_from_sina,
                        symbol, start_date, end_date, adjust
                    )
                except Exception as e_sina:
                    log_provider_error(
                        logger,
                        e_sina,
                        provider="sina_akshare",
                        operation="historical_prices_fallback",
                        retryable=False,
                        degraded_allowed=adjust != "hfq",
                        symbol=symbol,
                        effective_date=end_date,
                    )
                    logger.warning(f"DataGateway: Sina failed for {symbol}: {e_sina}. Falling back to YFinance.")
                    if adjust == "hfq":
                        logger.error(f"DataGateway: Cannot use YFinance for A-Share HFQ data for {symbol}. It returns unadjusted prices and causes cache poisoning.")
                        raise FatalSystemError(f"A-Share HFQ data unavailable for {symbol} (Baostock/Sina failed). Aborting pipeline to prevent severe PnL corruption.")
                    try:
                        df_new = self._fetch_validated_source(
                            "yfinance", self._fetch_from_yfinance,
                            symbol, start_date, end_date, adjust
                        )
                    except Exception as e_yf:
                        log_provider_error(
                            logger,
                            e_yf,
                            provider="yfinance",
                            operation="historical_prices_fallback",
                            retryable=False,
                            degraded_allowed=False,
                            symbol=symbol,
                            effective_date=end_date,
                            level="error",
                        )
                        logger.error(f"DataGateway: All A-share sources failed for {symbol}.")
                        raise FatalSystemError(
                            f"All A-share data sources failed for {symbol}. Aborting pipeline."
                        ) from e_yf
        else:
            # Non-A shares (US/HK)
            try:
                df_new = self._fetch_validated_source(
                    "yfinance", self._fetch_from_yfinance,
                    symbol, start_date, end_date, adjust
                )
            except DataIntegrityError:
                # Preserve the integrity error type for callers which may apply
                # an explicitly narrower close-only valuation policy.
                raise
            except Exception as e:
                log_provider_error(
                    logger,
                    e,
                    provider="yfinance",
                    operation="historical_prices",
                    retryable=False,
                    degraded_allowed=False,
                    symbol=symbol,
                    effective_date=end_date,
                    level="error",
                )
                logger.error(f"DataGateway: YFinance failed for {symbol}: {e}.")
                raise FatalSystemError(
                    f"YFinance failed for non-A-share asset {symbol}. Aborting pipeline."
                ) from e
                
        if not df_new.empty:
            if not df_cache.empty:
                df_new = pd.concat([df_cache, df_new]).drop_duplicates(subset=['日期']).sort_values('日期')
                df_new = self._validate_prices(df_new, symbol)

            df_new['日期'] = df_new['日期'].astype(str).str.replace('-', '').str[:8]
            df_new = df_new[(df_new['日期'] >= start_date) & (df_new['日期'] <= end_date)]
            if df_new.empty:
                raise DataIntegrityError(
                    f"No market data for {symbol} in requested range {start_date}-{end_date}"
                )
            self._save_to_cache(symbol, df_new, adjust)
            return df_new
            
        return df_cache

    def get_historical_closes(
        self, symbol: str, start_date: str, end_date: str, adjust: str = ""
    ) -> pd.DataFrame:
        """Return exact closing prices, isolating only unrelated OHLC fields.

        The normal strict OHLC path remains authoritative.  The degraded path
        for non-A shares performs a fresh bounded fetch, validates close against
        high/low, and deliberately bypasses the cache.  A shares may use the
        narrower cross-checked Tencent/Sina close path only after the normal
        BaoStock/Sina-daily chain fails.
        """
        start_date = str(start_date).replace("-", "")
        end_date = str(end_date).replace("-", "")
        is_a_share = len(str(symbol)) == 6 and str(symbol).isdigit()
        try:
            frame = self.get_historical_prices(symbol, start_date, end_date, adjust)
            return self._require_exact_close_range(
                frame, str(symbol), start_date, end_date
            )
        except InvalidMarketDataRequest:
            raise
        except (DataIntegrityError, FatalSystemError) as original_error:
            if self.historical_fixture_path:
                raise
            if is_a_share:
                logger.warning(
                    "DataGateway: primary A-share valuation failed for %s; "
                    "attempting cross-checked exact-session closes: %s",
                    symbol,
                    original_error,
                )
                try:
                    pair = self._get_validated_a_share_close_pair(
                        str(symbol), start_date, end_date
                    )
                    if adjust not in pair:
                        raise InvalidMarketDataRequest(
                            f"Unsupported A-share close adjustment {adjust!r}"
                        )
                    return self._require_exact_close_range(
                        pair[adjust], str(symbol), start_date, end_date
                    )
                except InvalidMarketDataRequest:
                    raise
                except Exception as fallback_error:
                    log_provider_error(
                        logger,
                        fallback_error,
                        provider="tencent_sina_crosscheck",
                        operation="exact_close_pair",
                        retryable=False,
                        degraded_allowed=False,
                        symbol=symbol,
                        effective_date=end_date,
                        level="error",
                    )
                    raise FatalSystemError(
                        f"Cross-checked exact-session A-share closes are "
                        f"unavailable for {symbol}: {fallback_error}"
                    ) from fallback_error
            if not isinstance(original_error, DataIntegrityError):
                raise
            logger.warning(
                "DataGateway: strict OHLC valuation failed for %s; isolating "
                "non-close fields and revalidating exact closes: %s",
                symbol,
                original_error,
            )

        try:
            degraded = self._call_source(
                "yfinance",
                self._fetch_from_yfinance,
                symbol,
                start_date,
                end_date,
                adjust,
            )
            closes = self._validate_closing_prices(degraded, symbol)
            closes = closes[
                (closes["日期"] >= start_date) & (closes["日期"] <= end_date)
            ]
            if closes.empty:
                raise DataIntegrityError(
                    f"No validated closing prices for {symbol} in "
                    f"{start_date}-{end_date}"
                )
            return self._require_exact_close_range(
                closes, str(symbol), start_date, end_date
            )
        except Exception as error:
            log_provider_error(
                logger,
                error,
                provider="yfinance",
                operation="validated_close_only",
                retryable=False,
                degraded_allowed=False,
                symbol=symbol,
                effective_date=end_date,
                level="error",
            )
            raise FatalSystemError(
                f"Validated closing prices are unavailable for {symbol}"
            ) from error

    def get_open_price(self, symbol: str, target_date: str) -> float:
        from core.market import AShareMarket, HKMarket, USMarket
        if symbol.endswith('.HK'):
            market = HKMarket()
        elif len(symbol) == 6 and symbol.isdigit():
            market = AShareMarket()
        else:
            market = USMarket()
            
        target_date_str = str(target_date).replace('-', '')[:8]
        target_dt = datetime.datetime.strptime(target_date_str, "%Y%m%d").date()
        
        exact_trade_date = market.get_previous_trading_date(target_dt)
        exact_date_str = exact_trade_date.strftime("%Y%m%d")
        
        try:
            df = self.get_historical_prices(symbol, exact_date_str, exact_date_str, adjust="")
            if not df.empty and '日期' in df.columns:
                dt_series = df['日期'].astype(str).str.replace('-', '')
                match = df[dt_series == exact_date_str]
                if not match.empty:
                    return float(match.iloc[0]['开盘'])
        except FatalSystemError:
            raise
        except Exception as e:
            log_provider_error(
                logger,
                e,
                provider="market_data_chain",
                operation="legacy_open_price",
                retryable=False,
                degraded_allowed=True,
                symbol=symbol,
                effective_date=exact_date_str,
            )
            logger.error(f"Failed to get open price for {symbol} around {target_date}: {e}")
            
        return 0.0

    def get_exact_open_price(self, symbol: str, target_date: str) -> float:
        """Return the unadjusted open for exactly one exchange session.

        Unlike the legacy compatibility method, this never rolls a holiday or
        missing bar backward. Settlement must remain pending when the requested
        session itself cannot be proven.
        """
        from core.market import AShareMarket, HKMarket, USMarket

        symbol_text = str(symbol).strip()
        if symbol_text.upper().endswith(".HK"):
            market = HKMarket()
        elif len(symbol_text) == 6 and symbol_text.isdigit():
            market = AShareMarket()
        else:
            market = USMarket()
        target = datetime.date.fromisoformat(str(target_date)[:10])
        if not market.is_trading_date(target):
            raise InvalidMarketDataRequest(
                f"{target.isoformat()} is not a {market.name} trading session"
            )
        target_text = target.strftime("%Y%m%d")
        frame = self.get_historical_prices(
            symbol_text, target_text, target_text, adjust=""
        )
        if frame is None or frame.empty or "日期" not in frame.columns or "开盘" not in frame.columns:
            raise FatalSystemError(
                f"Exact-session raw open is unavailable for {symbol_text}/{target}"
            )
        dates = frame["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
        exact = frame.loc[dates == target_text]
        if len(exact) != 1:
            raise FatalSystemError(
                f"Expected one raw open for {symbol_text}/{target}; found {len(exact)}"
            )
        value = exact.iloc[0]["开盘"]
        try:
            price = float(value)
        except (TypeError, ValueError) as error:
            raise DataIntegrityError(
                f"Invalid raw open for {symbol_text}/{target}: {value!r}"
            ) from error
        if not math.isfinite(price) or price <= 0:
            raise DataIntegrityError(
                f"Invalid raw open for {symbol_text}/{target}: {value!r}"
            )
        return price

    def get_exact_open_quote(self, symbol: str, target_date: str) -> dict:
        """Return an exact-session raw open plus immutable evidence metadata."""
        symbol_text = str(symbol).strip()
        target = datetime.date.fromisoformat(str(target_date)[:10])
        target_text = target.strftime("%Y%m%d")
        frame = self.get_historical_prices(
            symbol_text, target_text, target_text, adjust=""
        )
        if frame is None or frame.empty:
            raise FatalSystemError(
                f"Exact-session raw open is unavailable for {symbol_text}/{target}"
            )
        dates = frame["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
        exact = frame.loc[dates == target_text]
        if len(exact) != 1:
            raise FatalSystemError(
                f"Expected one raw open for {symbol_text}/{target}; found {len(exact)}"
            )
        row = exact.iloc[0]
        try:
            price = float(row["开盘"])
        except (KeyError, TypeError, ValueError) as error:
            raise DataIntegrityError(
                f"Invalid raw open for {symbol_text}/{target}"
            ) from error
        if not math.isfinite(price) or price <= 0:
            raise DataIntegrityError(
                f"Invalid raw open for {symbol_text}/{target}: {price!r}"
            )
        payload = {
            "schema_version": 1,
            "symbol": symbol_text,
            "session": target.isoformat(),
            "price_field": "open",
            "adjustment": "raw",
            "provider": str(frame.attrs.get("data_provider") or "validated_cache"),
            "open": price,
            "close": float(row["收盘"]),
            "high": float(row["最高"]),
            "low": float(row["最低"]),
            "volume": float(row["成交量"]),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return {
            "price": price,
            "provider": payload["provider"],
            "price_field": "open",
            "adjustment": "raw",
            "payload": payload,
            "payload_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        }

    def get_current_price(self, symbol: str) -> float:
        from core.market import AShareMarket, HKMarket, USMarket
        if symbol.endswith('.HK'):
            market = HKMarket()
        elif len(symbol) == 6 and symbol.isdigit():
            market = AShareMarket()
        else:
            market = USMarket()
            
        effective_date_str = market.get_effective_trading_date().replace('-', '')
        
        try:
            df = self.get_historical_prices(symbol, effective_date_str, effective_date_str, adjust="")
            if not df.empty:
                price = float(df.iloc[-1]['收盘'])
                if not math.isfinite(price) or price <= 0:
                    raise DataIntegrityError(f"Invalid current price for {symbol}: {price}")
                return price
        except FatalSystemError:
            raise
        except DataIntegrityError:
            raise
        except Exception as e:
            log_provider_error(
                logger,
                e,
                provider="market_data_chain",
                operation="current_price",
                retryable=False,
                degraded_allowed=True,
                symbol=symbol,
                effective_date=effective_date_str,
            )
            logger.error(f"Failed to get current price for {symbol}: {e}")
            
        return 0.0
