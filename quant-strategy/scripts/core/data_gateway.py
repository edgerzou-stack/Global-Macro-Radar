import os
import sqlite3
import math
import json
import re
import pandas as pd
from core.data_anomaly import DataAnomalyError

import datetime
import yfinance as yf
import akshare as ak
import baostock as bs
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
from functools import lru_cache

logger = logging.getLogger(__name__)
HISTORICAL_PRICE_FIXTURE_SCHEMA_VERSION = 1

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
            return self._validate_prices(frame, symbol)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_from_baostock(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        self._ensure_source_available("baostock")
        bs_sym = self._to_bs_symbol(symbol)
        start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        
        # adjustflag: '1': hfq (post-adjust), '2': qfq (pre-adjust), '3': non-adjust
        flag = "2" if adjust == "qfq" else ("1" if adjust == "hfq" else "3")
        
        try:
            bs.login()
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_from_sina(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        self._ensure_source_available("sina")
        sina_sym = self._to_sina_symbol(symbol)
        try:
            df = ak.stock_zh_a_daily(symbol=sina_sym, start_date=start_date, end_date=end_date, adjust=adjust)
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
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
        is limited to non-A-share NAV valuation, performs a fresh bounded fetch,
        validates close against high/low, and deliberately bypasses the cache.
        """
        try:
            frame = self.get_historical_prices(symbol, start_date, end_date, adjust)
            return frame[["日期", "收盘"]].copy()
        except InvalidMarketDataRequest:
            raise
        except DataIntegrityError as original_error:
            if self.historical_fixture_path or (len(symbol) == 6 and symbol.isdigit()):
                raise
            logger.warning(
                "DataGateway: strict OHLC valuation failed for %s; isolating "
                "non-close fields and revalidating exact closes: %s",
                symbol,
                original_error,
            )

        start_date = str(start_date).replace("-", "")
        end_date = str(end_date).replace("-", "")
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
            return closes
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
