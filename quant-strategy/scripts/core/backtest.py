"""Deterministic, point-in-time portfolio backtesting primitives.

This module deliberately has no network, current-universe, database, or wall-clock
dependencies. Callers must supply a versioned historical dataset explicitly.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional

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
        corporate_actions: Optional[Iterable[Mapping[str, Any]]] = None,
        manifest_context: Optional[Mapping[str, Any]] = None,
    ):
        self.initial_cash = self._finite_non_negative(initial_cash, "initial_cash")
        if self.initial_cash <= 0:
            raise BacktestDataError("initial_cash must be positive")
        self.commission_rate = self._finite_non_negative(
            commission_rate, "commission_rate"
        )
        self.slippage_bps = self._finite_non_negative(slippage_bps, "slippage_bps")
        self.prices = self._validate_prices(prices)
        self.corporate_actions = self._validate_corporate_actions(
            corporate_actions or []
        )
        self.manifest_context = dict(manifest_context or {})
        self._dates = self.prices["date"].drop_duplicates().tolist()
        self._prices_by_date = {
            date: frame.set_index("symbol")
            for date, frame in self.prices.groupby("date", sort=True)
        }
        self._actions_by_date = {
            date: frame.to_dict(orient="records")
            for date, frame in self.corporate_actions.groupby("date", sort=True)
        } if not self.corporate_actions.empty else {}

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

        optional = [column for column in ("fx_to_base", "delisted") if column in prices]
        result = prices.loc[:, list(cls.PRICE_COLUMNS) + optional].copy()
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

        if "fx_to_base" not in result:
            result["fx_to_base"] = 1.0
        result["fx_to_base"] = pd.to_numeric(result["fx_to_base"], errors="coerce")
        valid_fx = result["fx_to_base"].map(
            lambda value: math.isfinite(float(value)) and float(value) > 0
        )
        if not valid_fx.all():
            raise BacktestDataError("prices contain invalid fx_to_base values")
        if "delisted" not in result:
            result["delisted"] = False
        if not result["delisted"].map(lambda value: type(value) is bool).all():
            raise BacktestDataError("prices.delisted must be boolean")

        return result.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)

    @classmethod
    def _validate_corporate_actions(cls, actions):
        columns = ["date", "symbol", "type", "value"]
        if not actions:
            return pd.DataFrame(columns=columns)
        frame = pd.DataFrame(list(actions))
        missing = [column for column in columns if column not in frame]
        if missing:
            raise BacktestDataError(
                f"corporate actions missing columns: {', '.join(missing)}"
            )
        frame = frame.loc[:, columns].copy()
        parsed_dates = pd.to_datetime(frame["date"], errors="coerce")
        if parsed_dates.isna().any():
            raise BacktestDataError("corporate actions contain invalid dates")
        frame["date"] = parsed_dates.dt.strftime("%Y-%m-%d")
        frame["symbol"] = frame["symbol"].astype(str).str.strip()
        if frame["symbol"].eq("").any():
            raise BacktestDataError("corporate actions contain an empty symbol")
        if not frame["type"].isin({"split", "cash_dividend"}).all():
            raise BacktestDataError("unsupported corporate action type")
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        valid = frame["value"].map(
            lambda value: math.isfinite(float(value)) and float(value) >= 0
        )
        if not valid.all() or (
            (frame["type"] == "split") & (frame["value"] <= 0)
        ).any():
            raise BacktestDataError("invalid corporate action value")
        if frame.duplicated(["date", "symbol", "type"]).any():
            raise BacktestDataError("duplicate corporate action")
        # A same-session split changes the share count on which a declared
        # per-share cash distribution is based.  Make that ordering explicit.
        frame["_type_order"] = frame["type"].map({"split": 0, "cash_dividend": 1})
        return (
            frame.sort_values(["date", "symbol", "_type_order"], kind="stable")
            .drop(columns="_type_order")
            .reset_index(drop=True)
        )

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
            "engine": "point-in-time-v2",
            "initial_cash": self.initial_cash,
            "commission_rate": self.commission_rate,
            "slippage_bps": self.slippage_bps,
            "prices": self.prices.to_dict(orient="records"),
            "corporate_actions": self.corporate_actions.to_dict(orient="records"),
            "context": self.manifest_context,
            "signals": [
                {"date": item["date"], "weights": item["weights"]}
                for item in signals
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _base_price(day, symbol, column):
        return float(day.at[symbol, column]) * float(day.at[symbol, "fx_to_base"])

    def _apply_corporate_actions(self, date, day, cash, quantities, fill_rows):
        for action in self._actions_by_date.get(date, []):
            symbol = action["symbol"]
            quantity = quantities.get(symbol, 0.0)
            if quantity <= 1e-12:
                continue
            if symbol not in day.index:
                raise BacktestDataError(
                    f"missing FX/price row for corporate action on {date}: {symbol}"
                )
            if action["type"] == "split":
                quantities[symbol] = quantity * float(action["value"])
            else:
                cash_amount = (
                    quantity
                    * float(action["value"])
                    * float(day.at[symbol, "fx_to_base"])
                )
                cash += cash_amount
                self._record_fill(
                    fill_rows,
                    date,
                    date,
                    symbol,
                    "DIVIDEND",
                    quantity,
                    float(action["value"]) * float(day.at[symbol, "fx_to_base"]),
                    0.0,
                )
        return cash

    def run(self, signals: Iterable[Dict[str, Any]]) -> BacktestResult:
        clean_signals = self._validate_signals(signals)
        cash = self.initial_cash
        quantities: Dict[str, float] = {}
        fill_rows: List[Dict[str, Any]] = []
        nav_rows: List[Dict[str, Any]] = []
        next_signal = 0

        for date in self._dates:
            day = self._prices_by_date[date]
            cash = self._apply_corporate_actions(
                date, day, cash, quantities, fill_rows
            )
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

            missing_marks = sorted(set(quantities) - set(day.index))
            if missing_marks:
                raise BacktestDataError(
                    f"missing close marks on {date}: {', '.join(missing_marks)}"
                )
            delisted_symbols = sorted(
                symbol
                for symbol in quantities
                if bool(day.at[symbol, "delisted"])
            )
            for symbol in delisted_symbols:
                quantity = quantities.pop(symbol)
                price = self._base_price(day, symbol, "close")
                notional = quantity * price
                fee = notional * self.commission_rate
                cash += notional - fee
                self._record_fill(
                    fill_rows, date, date, symbol, "DELIST", quantity, price, fee
                )
            holdings_value = sum(
                quantity * self._base_price(day, symbol, "close")
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
            quantity * self._base_price(day, symbol, "open")
            for symbol, quantity in quantities.items()
        )
        desired = {
            symbol: nav_open * weight / self._base_price(day, symbol, "open")
            for symbol, weight in signal["weights"].items()
        }

        # Sell first so the target allocation can reuse released cash.
        for symbol in sorted(set(quantities) | set(desired)):
            current_quantity = quantities.get(symbol, 0.0)
            target_quantity = desired.get(symbol, 0.0)
            if target_quantity >= current_quantity - 1e-12:
                continue
            quantity = current_quantity - target_quantity
            price = self._base_price(day, symbol, "open") * (1 - self.slippage_bps / 10_000)
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
            price = self._base_price(day, symbol, "open") * (1 + self.slippage_bps / 10_000)
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
