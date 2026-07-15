"""Deterministic, point-in-time portfolio backtesting primitives.

This module deliberately has no network, current-universe, database, or wall-clock
dependencies. Callers must supply a versioned historical dataset explicitly.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List

import pandas as pd


class BacktestDataError(ValueError):
    """Raised when a backtest input cannot be used without inventing data."""


@dataclass(frozen=True)
class BacktestResult:
    nav: pd.DataFrame
    fills: pd.DataFrame
    manifest_hash: str
    pending_signals: int


class PointInTimeBacktest:
    """Long-only target-weight engine with next-session-open execution."""

    PRICE_COLUMNS = ("date", "symbol", "open", "close")

    def __init__(
        self,
        prices: pd.DataFrame,
        initial_cash: float = 1_000_000.0,
        commission_rate: float = 0.0,
        slippage_bps: float = 0.0,
    ):
        self.initial_cash = self._finite_non_negative(initial_cash, "initial_cash")
        if self.initial_cash <= 0:
            raise BacktestDataError("initial_cash must be positive")
        self.commission_rate = self._finite_non_negative(
            commission_rate, "commission_rate"
        )
        self.slippage_bps = self._finite_non_negative(slippage_bps, "slippage_bps")
        self.prices = self._validate_prices(prices)
        self._dates = self.prices["date"].drop_duplicates().tolist()
        self._prices_by_date = {
            date: frame.set_index("symbol")
            for date, frame in self.prices.groupby("date", sort=True)
        }

    @staticmethod
    def _finite_non_negative(value: Any, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise BacktestDataError(f"{name} must be numeric") from error
        if not math.isfinite(number) or number < 0:
            raise BacktestDataError(f"{name} must be finite and non-negative")
        return number

    @classmethod
    def _validate_prices(cls, prices: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(prices, pd.DataFrame) or prices.empty:
            raise BacktestDataError("prices must be a non-empty DataFrame")
        missing = [column for column in cls.PRICE_COLUMNS if column not in prices.columns]
        if missing:
            raise BacktestDataError(f"prices missing columns: {', '.join(missing)}")

        result = prices.loc[:, cls.PRICE_COLUMNS].copy()
        parsed_dates = pd.to_datetime(result["date"], errors="coerce")
        if parsed_dates.isna().any():
            raise BacktestDataError("prices contain invalid dates")
        result["date"] = parsed_dates.dt.strftime("%Y-%m-%d")
        result["symbol"] = result["symbol"].astype(str).str.strip()
        if result["symbol"].eq("").any():
            raise BacktestDataError("prices contain an empty symbol")
        if result.duplicated(["date", "symbol"]).any():
            raise BacktestDataError("prices contain duplicate date/symbol rows")

        for column in ("open", "close"):
            result[column] = pd.to_numeric(result[column], errors="coerce")
            valid = result[column].map(
                lambda value: math.isfinite(float(value)) and float(value) > 0
            )
            if not valid.all():
                raise BacktestDataError(f"prices contain invalid {column} values")

        return result.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)

    @staticmethod
    def _validate_signals(signals: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for sequence, signal in enumerate(signals):
            if not isinstance(signal, dict) or "date" not in signal or "weights" not in signal:
                raise BacktestDataError("each signal requires date and weights")
            parsed_date = pd.to_datetime(signal["date"], errors="coerce")
            if pd.isna(parsed_date):
                raise BacktestDataError("signal contains an invalid date")
            weights = signal["weights"]
            if not isinstance(weights, dict):
                raise BacktestDataError("signal weights must be a mapping")

            clean_weights = {}
            for raw_symbol, raw_weight in weights.items():
                symbol = str(raw_symbol).strip()
                if not symbol:
                    raise BacktestDataError("signal contains an empty symbol")
                try:
                    weight = float(raw_weight)
                except (TypeError, ValueError) as error:
                    raise BacktestDataError("target weights must be numeric") from error
                if not math.isfinite(weight) or weight < 0 or weight > 1:
                    raise BacktestDataError("target weights must be finite and in [0, 1]")
                if weight > 0:
                    clean_weights[symbol] = weight
            if sum(clean_weights.values()) > 1.0 + 1e-12:
                raise BacktestDataError("target weights cannot sum to more than 1")
            normalized.append(
                {
                    "date": parsed_date.strftime("%Y-%m-%d"),
                    "weights": clean_weights,
                    "sequence": sequence,
                }
            )
        return sorted(normalized, key=lambda item: (item["date"], item["sequence"]))

    def _manifest_hash(self, signals: List[Dict[str, Any]]) -> str:
        payload = {
            "engine": "point-in-time-v1",
            "initial_cash": self.initial_cash,
            "commission_rate": self.commission_rate,
            "slippage_bps": self.slippage_bps,
            "prices": self.prices.to_dict(orient="records"),
            "signals": [
                {"date": item["date"], "weights": item["weights"]}
                for item in signals
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def run(self, signals: Iterable[Dict[str, Any]]) -> BacktestResult:
        clean_signals = self._validate_signals(signals)
        cash = self.initial_cash
        quantities: Dict[str, float] = {}
        fill_rows: List[Dict[str, Any]] = []
        nav_rows: List[Dict[str, Any]] = []
        next_signal = 0

        for date in self._dates:
            eligible = []
            while (
                next_signal < len(clean_signals)
                and clean_signals[next_signal]["date"] < date
            ):
                eligible.append(clean_signals[next_signal])
                next_signal += 1

            # Multiple signals before one available session collapse to the latest
            # target; no unavailable historical fill is fabricated.
            if eligible:
                signal = eligible[-1]
                cash = self._rebalance(
                    date,
                    signal,
                    cash,
                    quantities,
                    fill_rows,
                )

            day = self._prices_by_date[date]
            missing_marks = sorted(set(quantities) - set(day.index))
            if missing_marks:
                raise BacktestDataError(
                    f"missing close marks on {date}: {', '.join(missing_marks)}"
                )
            holdings_value = sum(
                quantity * float(day.at[symbol, "close"])
                for symbol, quantity in quantities.items()
            )
            nav = cash + holdings_value
            if not math.isfinite(nav) or nav < -1e-8:
                raise BacktestDataError(f"invalid NAV on {date}: {nav}")
            nav_rows.append(
                {
                    "date": date,
                    "cash": cash,
                    "holdings_value": holdings_value,
                    "nav": nav,
                    "positions": len(quantities),
                }
            )

        return BacktestResult(
            nav=pd.DataFrame(nav_rows),
            fills=pd.DataFrame(
                fill_rows,
                columns=["date", "signal_date", "symbol", "side", "quantity", "price", "fee"],
            ),
            manifest_hash=self._manifest_hash(clean_signals),
            pending_signals=len(clean_signals) - next_signal,
        )

    def _rebalance(
        self,
        date: str,
        signal: Dict[str, Any],
        cash: float,
        quantities: Dict[str, float],
        fill_rows: List[Dict[str, Any]],
    ) -> float:
        day = self._prices_by_date[date]
        required_symbols = set(quantities) | set(signal["weights"])
        missing = sorted(required_symbols - set(day.index))
        if missing:
            raise BacktestDataError(
                f"missing execution prices on {date}: {', '.join(missing)}"
            )

        nav_open = cash + sum(
            quantity * float(day.at[symbol, "open"])
            for symbol, quantity in quantities.items()
        )
        desired = {
            symbol: nav_open * weight / float(day.at[symbol, "open"])
            for symbol, weight in signal["weights"].items()
        }

        # Sell first so the target allocation can reuse released cash.
        for symbol in sorted(set(quantities) | set(desired)):
            current_quantity = quantities.get(symbol, 0.0)
            target_quantity = desired.get(symbol, 0.0)
            if target_quantity >= current_quantity - 1e-12:
                continue
            quantity = current_quantity - target_quantity
            price = float(day.at[symbol, "open"]) * (1 - self.slippage_bps / 10_000)
            notional = quantity * price
            fee = notional * self.commission_rate
            cash += notional - fee
            self._record_fill(fill_rows, date, signal["date"], symbol, "SELL", quantity, price, fee)
            if target_quantity <= 1e-12:
                quantities.pop(symbol, None)
            else:
                quantities[symbol] = target_quantity

        for symbol in sorted(desired):
            current_quantity = quantities.get(symbol, 0.0)
            requested = desired[symbol] - current_quantity
            if requested <= 1e-12:
                continue
            price = float(day.at[symbol, "open"]) * (1 + self.slippage_bps / 10_000)
            unit_cost = price * (1 + self.commission_rate)
            quantity = min(requested, cash / unit_cost)
            if quantity <= 1e-12:
                continue
            notional = quantity * price
            fee = notional * self.commission_rate
            cash -= notional + fee
            if cash < 0 and cash > -1e-8:
                cash = 0.0
            if cash < -1e-8:
                raise BacktestDataError("execution spent more cash than available")
            quantities[symbol] = current_quantity + quantity
            self._record_fill(fill_rows, date, signal["date"], symbol, "BUY", quantity, price, fee)

        return cash

    @staticmethod
    def _record_fill(
        rows: List[Dict[str, Any]],
        date: str,
        signal_date: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        fee: float,
    ) -> None:
        rows.append(
            {
                "date": date,
                "signal_date": signal_date,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "fee": fee,
            }
        )
