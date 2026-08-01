import math

import pandas as pd

from core.market_data_contracts import DataIntegrityError


def validate_prices(frame, symbol):
    if frame is None or frame.empty:
        raise DataIntegrityError(f"Empty market data for {symbol}")
    required = ["日期", "开盘", "收盘", "最高", "最低", "成交量"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise DataIntegrityError(
            f"Market data for {symbol} is missing columns: {', '.join(missing)}"
        )

    result = frame.copy()
    result["日期"] = (
        result["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
    )
    if result["日期"].eq("").any() or result["日期"].duplicated().any():
        raise DataIntegrityError(
            f"Invalid or duplicate dates in market data for {symbol}"
        )
    if not result["日期"].is_monotonic_increasing:
        raise DataIntegrityError(
            f"Market data dates are not monotonic for {symbol}"
        )

    for column in ("开盘", "收盘", "最高", "最低", "成交量"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
        if not result[column].map(
            lambda value: math.isfinite(float(value))
        ).all():
            raise DataIntegrityError(
                f"Non-finite {column} value in market data for {symbol}"
            )
    if (result[["开盘", "收盘", "最高", "最低"]] <= 0).any().any():
        raise DataIntegrityError(
            f"Non-positive OHLC value in market data for {symbol}"
        )
    if (result["成交量"] < 0).any():
        raise DataIntegrityError(
            f"Negative volume in market data for {symbol}"
        )
    if (
        (result["最低"] > result["开盘"])
        | (result["最低"] > result["收盘"])
        | (result["最高"] < result["开盘"])
        | (result["最高"] < result["收盘"])
        | (result["最低"] > result["最高"])
    ).any():
        raise DataIntegrityError(
            f"Inconsistent OHLC values in market data for {symbol}"
        )
    return result


def validate_closing_prices(frame, symbol):
    if frame is None or frame.empty:
        raise DataIntegrityError(f"Empty closing-price data for {symbol}")
    required = ["日期", "收盘", "最高", "最低"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise DataIntegrityError(
            f"Closing-price data for {symbol} is missing columns: "
            + ", ".join(missing)
        )
    result = frame.copy()
    result["日期"] = (
        result["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
    )
    if result["日期"].eq("").any() or result["日期"].duplicated().any():
        raise DataIntegrityError(
            f"Invalid or duplicate closing dates for {symbol}"
        )
    if not result["日期"].is_monotonic_increasing:
        raise DataIntegrityError(
            f"Closing-price dates are not monotonic for {symbol}"
        )
    for column in ("收盘", "最高", "最低"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
        if not result[column].map(
            lambda value: math.isfinite(float(value))
        ).all():
            raise DataIntegrityError(f"Non-finite {column} value for {symbol}")
        if (result[column] <= 0).any():
            raise DataIntegrityError(
                f"Non-positive {column} value for {symbol}"
            )
    if (
        (result["最低"] > result["收盘"])
        | (result["最高"] < result["收盘"])
        | (result["最低"] > result["最高"])
    ).any():
        raise DataIntegrityError(
            f"Inconsistent close/high/low values for {symbol}"
        )
    return result[["日期", "收盘"]]


def require_exact_close_range(frame, symbol, start_date, end_date):
    if (
        frame is None
        or frame.empty
        or not {"日期", "收盘"}.issubset(frame.columns)
    ):
        raise DataIntegrityError(
            f"Closing-price range is empty or incomplete for {symbol}"
        )
    closes = frame[["日期", "收盘"]].copy()
    closes["日期"] = (
        closes["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
    )
    available = set(closes["日期"])
    missing = [
        value
        for value in dict.fromkeys((start_date, end_date))
        if value not in available
    ]
    if missing:
        raise DataIntegrityError(
            f"Closing-price range for {symbol} is missing exact endpoint(s): "
            + ", ".join(missing)
        )
    return closes
