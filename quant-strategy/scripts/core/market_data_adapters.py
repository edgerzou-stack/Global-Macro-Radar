import datetime
import math
import re
import threading
from contextlib import contextmanager

import pandas as pd
import requests
import baostock.common.contants as baostock_constants
import baostock.common.context as baostock_context
import baostock.util.socketutil as baostock_socket_util
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.market_data_contracts import CircuitBreakerError
from core.market_symbols import (
    to_baostock_symbol,
    to_sina_symbol,
    to_tencent_symbol,
    to_yfinance_symbol,
)
from core.market_data_contracts import (
    DataIntegrityError,
    InvalidMarketDataRequest,
)
from core.provider_errors import log_provider_error


SINA_HTTP_TIMEOUT = (5.0, 10.0)
BAOSTOCK_SOCKET_TIMEOUT_SECONDS = 10.0
A_SHARE_CLOSE_HTTP_TIMEOUT = (5.0, 10.0)
TENCENT_A_SHARE_KLINE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
)
SINA_A_SHARE_QUOTE_URL = "https://hq.sinajs.cn/list={market_symbol}"
_SINA_REQUEST_PATCH_LOCK = threading.Lock()
_BAOSTOCK_SESSION_LOCK = threading.Lock()


class _BaoStockSocketGuard:
    """Turn a peer-closed BaoStock socket into a bounded provider failure.

    BaoStock's SDK loops until it sees a protocol terminator. When the peer
    closes first, ``socket.recv`` returns ``b""`` and the SDK otherwise spins
    forever because it never treats EOF as terminal.
    """

    def __init__(self, client):
        self._client = client

    def recv(self, *args, **kwargs):
        payload = self._client.recv(*args, **kwargs)
        if payload == b"":
            raise ConnectionResetError(
                "BaoStock closed the socket before the response terminator"
            )
        return payload

    def __getattr__(self, name):
        return getattr(self._client, name)


def _call_sina_with_bounded_http(fetch, *args, **kwargs):
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
            setattr(
                baostock_context,
                "default_socket",
                _BaoStockSocketGuard(client),
            )

        baostock_socket_util.SocketUtil.connect = bounded_connect
        try:
            yield
        finally:
            baostock_socket_util.SocketUtil.connect = original_connect


class MarketDataAdapters:
    """Provider SDK boundary; orchestration and validation stay in DataGateway."""

    def __init__(
        self,
        *,
        ensure_source_available,
        record_success,
        record_failure,
        logger,
    ):
        self.ensure_source_available = ensure_source_available
        self.record_success = record_success
        self.record_failure = record_failure
        self.logger = logger

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(CircuitBreakerError),
    )
    def fetch_baostock(self, symbol, start_date, end_date, adjust):
        import baostock as bs

        self.ensure_source_available("baostock")
        provider_symbol = to_baostock_symbol(symbol)
        start_fmt = (
            f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        )
        end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        flag = "2" if adjust == "qfq" else ("1" if adjust == "hfq" else "3")
        with _bounded_baostock_session():
            try:
                login_result = bs.login()
                if getattr(login_result, "error_code", None) != "0":
                    raise ConnectionError(
                        "BaoStock login failed: "
                        + str(
                            getattr(
                                login_result,
                                "error_msg",
                                "unknown error",
                            )
                        )
                    )
                result = bs.query_history_k_data_plus(
                    provider_symbol,
                    "date,open,high,low,close,volume",
                    start_date=start_fmt,
                    end_date=end_fmt,
                    frequency="d",
                    adjustflag=flag,
                )
                if result.error_code != "0":
                    raise ValueError(
                        f"BaoStock query error: {result.error_msg}"
                    )
                rows = []
                while result.error_code == "0" and result.next():
                    rows.append(result.get_row_data())
                if not rows:
                    raise ValueError(
                        f"BaoStock returned empty dataframe for {provider_symbol}"
                    )
                frame = pd.DataFrame(rows, columns=result.fields)
                frame = frame.rename(
                    columns={
                        "date": "日期",
                        "open": "开盘",
                        "close": "收盘",
                        "high": "最高",
                        "low": "最低",
                        "volume": "成交量",
                    }
                )
                frame["日期"] = frame["日期"].str.replace("-", "")
                for column in ("开盘", "收盘", "最高", "最低", "成交量"):
                    if column in frame.columns:
                        frame[column] = pd.to_numeric(
                            frame[column], errors="coerce"
                        )
                self.record_success("baostock")
                return frame
            except Exception as error:
                if not isinstance(error, CircuitBreakerError):
                    self.record_failure("baostock")
                log_provider_error(
                    self.logger,
                    error,
                    provider="baostock",
                    operation="historical_prices",
                    retryable=not isinstance(error, CircuitBreakerError),
                    degraded_allowed=True,
                    symbol=symbol,
                    effective_date=end_date,
                )
                raise
            finally:
                bs.logout()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(CircuitBreakerError),
    )
    def fetch_sina(self, symbol, start_date, end_date, adjust):
        import akshare as ak

        self.ensure_source_available("sina")
        provider_symbol = to_sina_symbol(symbol)
        try:
            frame = _call_sina_with_bounded_http(
                ak.stock_zh_a_daily,
                symbol=provider_symbol,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if frame.empty:
                raise ValueError(
                    f"Sina returned empty dataframe for {provider_symbol}"
                )
            frame = frame.reset_index()
            if "date" in frame.columns:
                frame = frame.rename(
                    columns={
                        "date": "日期",
                        "open": "开盘",
                        "close": "收盘",
                        "high": "最高",
                        "low": "最低",
                        "volume": "成交量",
                    }
                )
                if pd.api.types.is_datetime64_any_dtype(frame["日期"]):
                    frame["日期"] = frame["日期"].dt.strftime("%Y%m%d")
                else:
                    frame["日期"] = (
                        frame["日期"].astype(str).str.replace("-", "")
                    )
            self.record_success("sina")
            return frame
        except Exception as error:
            if not isinstance(error, CircuitBreakerError):
                self.record_failure("sina")
            log_provider_error(
                self.logger,
                error,
                provider="sina_akshare",
                operation="historical_prices",
                retryable=not isinstance(error, CircuitBreakerError),
                degraded_allowed=True,
                symbol=symbol,
                effective_date=end_date,
            )
            raise



    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(CircuitBreakerError),
    )
    def fetch_yfinance(self, symbol, start_date, end_date, adjust=""):
        import yfinance as yf

        self.ensure_source_available("yfinance")
        provider_symbol = to_yfinance_symbol(symbol)
        start_fmt = (
            f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        )
        end_dt = datetime.datetime.strptime(
            end_date, "%Y%m%d"
        ) + datetime.timedelta(days=1)
        try:
            frame = yf.Ticker(provider_symbol).history(
                start=start_fmt,
                end=end_dt.strftime("%Y-%m-%d"),
                auto_adjust=adjust == "hfq",
            )
            if frame.empty:
                raise ValueError(
                    f"YFinance returned empty dataframe for {provider_symbol}"
                )
            frame = frame.reset_index()
            if "Date" in frame.columns:
                frame = frame.rename(
                    columns={
                        "Date": "日期",
                        "Open": "开盘",
                        "Close": "收盘",
                        "High": "最高",
                        "Low": "最低",
                        "Volume": "成交量",
                    }
                )
                frame["日期"] = frame["日期"].dt.strftime("%Y%m%d")
            self.record_success("yfinance")
            return frame
        except Exception as error:
            if not isinstance(error, CircuitBreakerError):
                self.record_failure("yfinance")
            log_provider_error(
                self.logger,
                error,
                provider="yfinance",
                operation="historical_prices",
                retryable=not isinstance(error, CircuitBreakerError),
                degraded_allowed=False,
                symbol=symbol,
                effective_date=end_date,
            )
            raise


class AShareQuoteAdapter:
    """Tencent/Sina exact-session quote transport without fallback policy."""

    def __init__(self, *, validate_prices):
        self.validate_prices = validate_prices

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

    def fetch_tencent_daily(
        self,
        symbol,
        start_date,
        end_date,
        adjust,
    ):
        if adjust not in {"", "hfq"}:
            raise InvalidMarketDataRequest(
                f"Unsupported Tencent adjustment mode {adjust!r}"
            )
        market_symbol = to_tencent_symbol(symbol)
        start_fmt = datetime.datetime.strptime(
            start_date,
            "%Y%m%d",
        ).strftime("%Y-%m-%d")
        end_fmt = datetime.datetime.strptime(
            end_date,
            "%Y%m%d",
        ).strftime("%Y-%m-%d")
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
                f"Tencent close endpoint rejected {symbol}: "
                f"{payload.get('msg', '')}"
            )
        data = (payload.get("data") or {}).get(market_symbol) or {}
        series_key = "hfqday" if adjust == "hfq" else "day"
        rows = data.get(series_key) or []
        if not rows:
            raise DataIntegrityError(
                f"Tencent returned no {adjust or 'raw'} daily bars "
                f"for {symbol}"
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
            if (
                row_date == end_date
                and len(row) > 6
                and isinstance(row[6], dict)
            ):
                corporate_action = dict(row[6])
        frame = pd.DataFrame(
            normalized_rows,
            columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"],
        )
        frame["日期"] = (
            frame["日期"].astype(str).str.replace("-", "", regex=False)
        )
        frame = self.validate_prices(frame, symbol)

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
            "time": (
                f"{timestamp[8:10]}:{timestamp[10:12]}:"
                f"{timestamp[12:14]}"
            ),
            "open": self._quote_number(
                quote_values[5],
                "open",
                symbol,
            ),
            "close": self._quote_number(
                quote_values[3],
                "close",
                symbol,
            ),
            "high": self._quote_number(
                quote_values[33],
                "high",
                symbol,
            ),
            "low": self._quote_number(
                quote_values[34],
                "low",
                symbol,
            ),
            "previous_close": self._quote_number(
                quote_values[4],
                "previous close",
                symbol,
            ),
            "corporate_action": corporate_action,
        }
        return frame, quote

    def fetch_sina_quote(self, symbol):
        market_symbol = to_sina_symbol(symbol)
        response = requests.get(
            SINA_A_SHARE_QUOTE_URL.format(
                market_symbol=market_symbol
            ),
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
        return {
            "symbol": str(symbol).zfill(6),
            "date": str(values[30]).replace("-", ""),
            "time": str(values[31]),
            "open": self._quote_number(values[1], "open", symbol),
            "close": self._quote_number(values[3], "close", symbol),
            "high": self._quote_number(values[4], "high", symbol),
            "low": self._quote_number(values[5], "low", symbol),
            "previous_close": self._quote_number(
                values[2],
                "previous close",
                symbol,
            ),
        }
