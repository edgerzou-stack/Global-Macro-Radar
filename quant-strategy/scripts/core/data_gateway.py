import os
import sqlite3
import math
import json
import hashlib
import re
import threading
from contextlib import contextmanager
import pandas as pd
from core.data_anomaly import DataAnomalyError

import datetime
import requests
import yfinance as yf
import akshare as ak
import baostock as bs
import baostock.util.socketutil as baostock_socket_util
import baostock.common.context as baostock_context
import baostock.common.contants as baostock_constants
import logging
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from functools import lru_cache

logger = logging.getLogger(__name__)
HISTORICAL_PRICE_FIXTURE_SCHEMA_VERSION = 1
SINA_HTTP_TIMEOUT = (5.0, 10.0)
_SINA_REQUEST_PATCH_LOCK = threading.Lock()
BAOSTOCK_SOCKET_TIMEOUT_SECONDS = 10.0
_BAOSTOCK_SESSION_LOCK = threading.Lock()
A_SHARE_CLOSE_HTTP_TIMEOUT = (5.0, 10.0)
TENCENT_A_SHARE_KLINE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
)
SINA_A_SHARE_QUOTE_URL = "https://hq.sinajs.cn/list={market_symbol}"
A_SHARE_CLOSE_RELATIVE_TOLERANCE = 0.001


def _call_sina_with_bounded_http(fetch, *args, **kwargs):
    """Call AkShare's Sina adapter while supplying its missing HTTP timeout.

    ``ak.stock_zh_a_daily`` currently performs several module-level
    ``requests.get`` calls without a timeout.  A single half-open response can
    therefore block a worker and its parent future forever.  Serialize this
    narrow compatibility patch so concurrent Sina calls cannot restore each
    other's function pointer out of order.
    """

    with _SINA_REQUEST_PATCH_LOCK:
        original_get = requests.get

        def bounded_get(*get_args, **get_kwargs):
            get_kwargs.setdefault("timeout", SINA_HTTP_TIMEOUT)
            return original_get(*get_args, **get_kwargs)

        requests.get = bounded_get
        try:
            return fetch(*args, **kwargs)
        finally:
            requests.get = original_get


@contextmanager
def _bounded_baostock_session():
    """Serialize Baostock's global socket and add a finite socket timeout.

    The upstream SDK stores one ``default_socket`` in module-global state, so
    concurrent login/query/logout sequences corrupt each other.  Its socket is
    blocking by default as well.  Keep the entire session behind one lock and
    patch only the SDK's connection method while that lock is held.
    """

    with _BAOSTOCK_SESSION_LOCK:
        original_connect = baostock_socket_util.SocketUtil.connect

        def bounded_connect(_socket_util):
            client = baostock_socket_util.socket.socket(
                baostock_socket_util.socket.AF_INET,
                baostock_socket_util.socket.SOCK_STREAM,
            )
            client.settimeout(BAOSTOCK_SOCKET_TIMEOUT_SECONDS)
            client.connect(
                (
                    baostock_constants.BAOSTOCK_SERVER_IP,
                    baostock_constants.BAOSTOCK_SERVER_PORT,
                )
            )
            setattr(baostock_context, "default_socket", client)

        baostock_socket_util.SocketUtil.connect = bounded_connect
        try:
            yield
        finally:
            baostock_socket_util.SocketUtil.connect = original_connect

class DataIntegrityError(Exception):
    """Raised when fetched data fails integrity checks (e.g., price <= 0)."""
    pass


class InvalidMarketDataRequest(DataIntegrityError):
    """Raised for a locally invalid range before any provider is contacted."""
    pass

class CircuitBreakerError(Exception):
    """Raised when a data source triggers a circuit breaker."""
    pass

class FatalSystemError(Exception):
    """Raised when all data sources are broken (double circuit breaker)."""
    pass


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
        """Return normalized price data or fail closed on any corrupt row."""
        if df is None or df.empty:
            raise DataIntegrityError(f"Empty market data for {symbol}")

        required = ["日期", "开盘", "收盘", "最高", "最低", "成交量"]
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise DataIntegrityError(
                f"Market data for {symbol} is missing columns: {', '.join(missing)}"
            )

        result = df.copy()
        result["日期"] = result["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
        if result["日期"].eq("").any() or result["日期"].duplicated().any():
            raise DataIntegrityError(f"Invalid or duplicate dates in market data for {symbol}")
        if not result["日期"].is_monotonic_increasing:
            raise DataIntegrityError(f"Market data dates are not monotonic for {symbol}")

        numeric_columns = ["开盘", "收盘", "最高", "最低", "成交量"]
        for column in numeric_columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
            if not result[column].map(lambda value: math.isfinite(float(value))).all():
                raise DataIntegrityError(
                    f"Non-finite {column} value in market data for {symbol}"
                )

        if (result[["开盘", "收盘", "最高", "最低"]] <= 0).any().any():
            raise DataIntegrityError(f"Non-positive OHLC value in market data for {symbol}")
        if (result["成交量"] < 0).any():
            raise DataIntegrityError(f"Negative volume in market data for {symbol}")
        if (
            (result["最低"] > result["开盘"])
            | (result["最低"] > result["收盘"])
            | (result["最高"] < result["开盘"])
            | (result["最高"] < result["收盘"])
            | (result["最低"] > result["最高"])
        ).any():
            raise DataIntegrityError(f"Inconsistent OHLC values in market data for {symbol}")

        return result

    @staticmethod
    def _validate_closing_prices(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Validate a close-only valuation view without accepting bad closes.

        Some upstream HK rows contain an invalid opening auction field while
        their close remains bounded by the reported high/low.  NAV never uses
        the open, so it may isolate that field; close/high/low integrity stays
        mandatory and the degraded row is never written to the OHLC cache.
        """
        if df is None or df.empty:
            raise DataIntegrityError(f"Empty closing-price data for {symbol}")
        required = ["日期", "收盘", "最高", "最低"]
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise DataIntegrityError(
                f"Closing-price data for {symbol} is missing columns: {', '.join(missing)}"
            )

        result = df.copy()
        result["日期"] = result["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
        if result["日期"].eq("").any() or result["日期"].duplicated().any():
            raise DataIntegrityError(f"Invalid or duplicate closing dates for {symbol}")
        if not result["日期"].is_monotonic_increasing:
            raise DataIntegrityError(f"Closing-price dates are not monotonic for {symbol}")
        for column in ("收盘", "最高", "最低"):
            result[column] = pd.to_numeric(result[column], errors="coerce")
            if not result[column].map(lambda value: math.isfinite(float(value))).all():
                raise DataIntegrityError(f"Non-finite {column} value for {symbol}")
            if (result[column] <= 0).any():
                raise DataIntegrityError(f"Non-positive {column} value for {symbol}")
        if (
            (result["最低"] > result["收盘"])
            | (result["最高"] < result["收盘"])
            | (result["最低"] > result["最高"])
        ).any():
            raise DataIntegrityError(f"Inconsistent close/high/low values for {symbol}")
        return result[["日期", "收盘"]]

    @staticmethod
    def _require_exact_close_range(
        frame: pd.DataFrame, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Reject a non-empty but stale series that omits a requested endpoint."""
        if frame is None or frame.empty or not {"日期", "收盘"}.issubset(frame.columns):
            raise DataIntegrityError(
                f"Closing-price range is empty or incomplete for {symbol}"
            )
        closes = frame[["日期", "收盘"]].copy()
        closes["日期"] = (
            closes["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
        )
        available = set(closes["日期"])
        missing = [
            date for date in dict.fromkeys((start_date, end_date)) if date not in available
        ]
        if missing:
            raise DataIntegrityError(
                f"Closing-price range for {symbol} is missing exact endpoint(s): "
                + ", ".join(missing)
            )
        return closes

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
        if '.' in symbol and symbol.upper().endswith(('HK', 'US', 'SS', 'SZ', 'BJ')):
            return symbol
        symbol_str = str(symbol)
        if len(symbol_str) == 6 and symbol_str.isdigit():
            if symbol_str.startswith('6'):
                return f"{symbol_str}.SS"
            elif symbol_str.startswith(('8', '4', '9')):
                return f"{symbol_str}.BJ"
            else:
                return f"{symbol_str}.SZ"
        return symbol

    def _to_bs_symbol(self, symbol: str) -> str:
        """Converts internal symbol (like 600519) to baostock symbol (sh.600519)."""
        symbol_str = str(symbol)
        if len(symbol_str) == 6 and symbol_str.isdigit():
            if symbol_str.startswith('6'):
                return f"sh.{symbol_str}"
            elif symbol_str.startswith(('8', '4', '9')):
                return f"bj.{symbol_str}"
            else:
                return f"sz.{symbol_str}"
        return symbol

    def _to_sina_symbol(self, symbol: str) -> str:
        """Converts internal symbol (like 600519) to sina symbol (sh600519)."""
        symbol_str = str(symbol)
        if len(symbol_str) == 6 and symbol_str.isdigit():
            if symbol_str.startswith('6'):
                return f"sh{symbol_str}"
            elif symbol_str.startswith(('8', '4', '9')):
                return f"bj{symbol_str}"
            else:
                return f"sz{symbol_str}"
        return symbol

    def _to_tencent_symbol(self, symbol: str) -> str:
        """Convert an internal A-share code to Tencent's market prefix."""
        symbol_str = str(symbol)
        if len(symbol_str) != 6 or not symbol_str.isdigit():
            raise InvalidMarketDataRequest(
                f"Tencent A-share fallback cannot route symbol {symbol!r}"
            )
        if symbol_str.startswith("6"):
            return f"sh{symbol_str}"
        if symbol_str.startswith(("8", "4", "9")):
            return f"bj{symbol_str}"
        return f"sz{symbol_str}"

    @staticmethod
    def _quote_number(value, field, symbol):
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise DataIntegrityError(
                f"Invalid {field} in close snapshot for {symbol}"
            ) from error
        if not math.isfinite(number) or number <= 0:
            raise DataIntegrityError(
                f"Invalid {field} in close snapshot for {symbol}"
            )
        return number

    def _fetch_tencent_a_share_daily(
        self, symbol: str, start_date: str, end_date: str, adjust: str
    ):
        """Fetch Tencent daily bars plus its timestamped quote envelope."""
        if adjust not in {"", "hfq"}:
            raise InvalidMarketDataRequest(
                f"Unsupported Tencent adjustment mode {adjust!r}"
            )
        market_symbol = self._to_tencent_symbol(symbol)
        start_fmt = datetime.datetime.strptime(start_date, "%Y%m%d").strftime(
            "%Y-%m-%d"
        )
        end_fmt = datetime.datetime.strptime(end_date, "%Y%m%d").strftime(
            "%Y-%m-%d"
        )
        response = requests.get(
            TENCENT_A_SHARE_KLINE_URL,
            params={
                "param": (
                    f"{market_symbol},day,{start_fmt},{end_fmt},"
                    f"640,{adjust}"
                )
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://gu.qq.com/",
            },
            timeout=A_SHARE_CLOSE_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise DataIntegrityError(
                f"Tencent close endpoint rejected {symbol}: {payload.get('msg', '')}"
            )
        data = (payload.get("data") or {}).get(market_symbol) or {}
        series_key = "hfqday" if adjust == "hfq" else "day"
        rows = data.get(series_key) or []
        if not rows:
            raise DataIntegrityError(
                f"Tencent returned no {adjust or 'raw'} daily bars for {symbol}"
            )
        normalized_rows = []
        corporate_action = None
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise DataIntegrityError(
                    f"Tencent returned a malformed daily bar for {symbol}"
                )
            normalized_rows.append(row[:6])
            row_date = str(row[0]).replace("-", "")
            if row_date == end_date and len(row) > 6 and isinstance(row[6], dict):
                corporate_action = dict(row[6])
        frame = pd.DataFrame(
            normalized_rows,
            columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"],
        )
        frame["日期"] = frame["日期"].astype(str).str.replace("-", "", regex=False)
        frame = self._validate_prices(frame, symbol)

        quote_values = ((data.get("qt") or {}).get(market_symbol) or [])
        if len(quote_values) < 35:
            raise DataIntegrityError(
                f"Tencent returned an incomplete quote envelope for {symbol}"
            )
        timestamp = str(quote_values[30])
        if not re.fullmatch(r"\d{14}", timestamp):
            raise DataIntegrityError(
                f"Tencent returned an invalid quote timestamp for {symbol}"
            )
        quote = {
            "symbol": str(quote_values[2]).zfill(6),
            "date": timestamp[:8],
            "time": f"{timestamp[8:10]}:{timestamp[10:12]}:{timestamp[12:14]}",
            "open": self._quote_number(quote_values[5], "open", symbol),
            "close": self._quote_number(quote_values[3], "close", symbol),
            "high": self._quote_number(quote_values[33], "high", symbol),
            "low": self._quote_number(quote_values[34], "low", symbol),
            "previous_close": self._quote_number(
                quote_values[4], "previous close", symbol
            ),
            "corporate_action": corporate_action,
        }
        return frame, quote

    def _fetch_sina_a_share_quote(self, symbol: str):
        """Fetch a second, independently timestamped A-share close snapshot."""
        market_symbol = self._to_sina_symbol(symbol)
        response = requests.get(
            SINA_A_SHARE_QUOTE_URL.format(market_symbol=market_symbol),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=A_SHARE_CLOSE_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        text = response.content.decode("gb18030", errors="strict")
        match = re.search(r'=\s*"(.*)";\s*$', text.strip())
        if not match:
            raise DataIntegrityError(
                f"Sina returned an invalid quote envelope for {symbol}"
            )
        values = match.group(1).split(",")
        if len(values) < 32:
            raise DataIntegrityError(
                f"Sina returned an incomplete quote envelope for {symbol}"
            )
        date_text = str(values[30]).replace("-", "")
        return {
            "symbol": str(symbol).zfill(6),
            "date": date_text,
            "time": str(values[31]),
            "open": self._quote_number(values[1], "open", symbol),
            "close": self._quote_number(values[3], "close", symbol),
            "high": self._quote_number(values[4], "high", symbol),
            "low": self._quote_number(values[5], "low", symbol),
            "previous_close": self._quote_number(
                values[2], "previous close", symbol
            ),
        }

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
            return {key: value.copy() for key, value in cached.items()}

        raw, tencent_quote = self._fetch_tencent_a_share_daily(
            symbol, start_date, end_date, ""
        )
        hfq, hfq_quote = self._fetch_tencent_a_share_daily(
            symbol, start_date, end_date, "hfq"
        )
        sina_quote = self._fetch_sina_a_share_quote(symbol)

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(CircuitBreakerError),
    )
    def _fetch_from_baostock(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        self._ensure_source_available("baostock")
        bs_sym = self._to_bs_symbol(symbol)
        start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        
        # adjustflag: '1': hfq (post-adjust), '2': qfq (pre-adjust), '3': non-adjust
        flag = "2" if adjust == "qfq" else ("1" if adjust == "hfq" else "3")
        
        with _bounded_baostock_session():
            try:
                login_result = bs.login()
                if getattr(login_result, "error_code", None) != "0":
                    raise ConnectionError(
                        "BaoStock login failed: "
                        f"{getattr(login_result, 'error_msg', 'unknown error')}"
                    )
                rs = bs.query_history_k_data_plus(
                    bs_sym, "date,open,high,low,close,volume",
                    start_date=start_fmt, end_date=end_fmt, frequency="d", adjustflag=flag
                )

                if rs.error_code != '0':
                    raise ValueError(f"BaoStock query error: {rs.error_msg}")

                data_list = []
                while (rs.error_code == '0') & rs.next():
                    data_list.append(rs.get_row_data())

                if not data_list:
                    raise ValueError(f"BaoStock returned empty dataframe for {bs_sym}")

                df = pd.DataFrame(data_list, columns=rs.fields)
                df = df.rename(columns={
                    'date': '日期', 'open': '开盘', 'close': '收盘',
                    'high': '最高', 'low': '最低', 'volume': '成交量'
                })
                df['日期'] = df['日期'].str.replace('-', '')

                # Numeric conversion
                for col in ['开盘', '收盘', '最高', '最低', '成交量']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')

                self.breakers["baostock"].record_success()
                return df
            except Exception as e:
                if not isinstance(e, CircuitBreakerError):
                    self.breakers["baostock"].record_failure()
                raise e
            finally:
                bs.logout()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(CircuitBreakerError),
    )
    def _fetch_from_sina(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        self._ensure_source_available("sina")
        sina_sym = self._to_sina_symbol(symbol)
        try:
            df = _call_sina_with_bounded_http(
                ak.stock_zh_a_daily,
                symbol=sina_sym,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if df.empty:
                raise ValueError(f"Sina returned empty dataframe for {sina_sym}")
            
            df = df.reset_index()
            if 'date' in df.columns:
                df = df.rename(columns={
                    'date': '日期', 'open': '开盘', 'close': '收盘',
                    'high': '最高', 'low': '最低', 'volume': '成交量'
                })
                # Check if it's datetime or str
                if pd.api.types.is_datetime64_any_dtype(df['日期']):
                    df['日期'] = df['日期'].dt.strftime('%Y%m%d')
                else:
                    df['日期'] = df['日期'].astype(str).str.replace('-', '')
            
            self.breakers["sina"].record_success()
            return df
        except Exception as e:
            if not isinstance(e, CircuitBreakerError):
                self.breakers["sina"].record_failure()
            raise e

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(CircuitBreakerError),
    )
    def _fetch_from_yfinance(self, symbol: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
        self._ensure_source_available("yfinance")
        yf_sym = self._to_yf_symbol(symbol)
        start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_dt = datetime.datetime.strptime(end_date, "%Y%m%d") + datetime.timedelta(days=1)
        end_fmt = end_dt.strftime("%Y-%m-%d")
        
        auto_adj = True if adjust == "hfq" else False
        try:
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(start=start_fmt, end=end_fmt, auto_adjust=auto_adj)
            
            if df.empty:
                raise ValueError(f"YFinance returned empty dataframe for {yf_sym}")
                
            df = df.reset_index()
            if 'Date' in df.columns:
                df = df.rename(columns={
                    'Date': '日期', 'Open': '开盘', 'Close': '收盘',
                    'High': '最高', 'Low': '最低', 'Volume': '成交量'
                })
                df['日期'] = df['日期'].dt.strftime('%Y%m%d')
                
            self.breakers["yfinance"].record_success()
            return df
        except Exception as e:
            if not isinstance(e, CircuitBreakerError):
                self.breakers["yfinance"].record_failure()
            raise e

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
                logger.warning(f"DataGateway: baostock failed for {symbol}: {e_bs}. Falling back to Sina.")
                try:
                    df_new = self._fetch_validated_source(
                        "sina", self._fetch_from_sina,
                        symbol, start_date, end_date, adjust
                    )
                except Exception as e_sina:
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
        price = self.get_exact_open_price(symbol_text, target.isoformat())
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
            logger.error(f"Failed to get current price for {symbol}: {e}")
            
        return 0.0
