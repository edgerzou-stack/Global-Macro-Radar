import datetime
import math

import pandas as pd

import db_utils
from core.cash_manager import CashManager
from core.clock import clock
from core.data_gateway import DataGateway


data_gateway = DataGateway()


def _market_for_strategy(strategy):
    from core.market import AShareMarket, HKMarket, USMarket

    if "_us_" in strategy:
        return USMarket()
    if "_hk_" in strategy:
        return HKMarket()
    return AShareMarket()


def _positive_finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _close_on_date(frame, date, fallback):
    if frame is None or frame.empty or "收盘" not in frame.columns:
        return None
    selected = frame
    if "日期" in frame.columns:
        dates = frame["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
        exact = frame[dates == date]
        if not exact.empty:
            selected = exact
    row = selected.iloc[0] if fallback == "first" else selected.iloc[-1]
    return _positive_finite(row.get("收盘"))


def _prepare_market_data(portfolio, today):
    """Fetch each symbol/adjust pair once for the complete valuation range."""
    requirements = {}
    position_dates = {}

    for strategy, positions in portfolio.items():
        market = _market_for_strategy(strategy)
        end_raw = datetime.datetime.strptime(
            market.get_effective_trading_date(), "%Y-%m-%d"
        ).date()
        end_date = market.get_most_recent_trading_day(end_raw).strftime("%Y%m%d")
        for symbol, position in positions.items():
            if _positive_finite(position.get("entry_price")) is None:
                continue
            try:
                entry_raw = datetime.datetime.strptime(
                    str(position.get("entry_date", today))[:10], "%Y-%m-%d"
                ).date()
            except (TypeError, ValueError):
                entry_raw = today
            entry_date = market.get_most_recent_trading_day(entry_raw).strftime("%Y%m%d")
            fetch_symbol = (
                f"{symbol}.HK"
                if "_hk_" in strategy and not symbol.upper().endswith(".HK")
                else symbol
            )
            position_dates[(strategy, symbol)] = (fetch_symbol, entry_date, end_date)
            requirement = requirements.setdefault(
                fetch_symbol, {"start": entry_date, "end": end_date}
            )
            requirement["start"] = min(requirement["start"], entry_date)
            requirement["end"] = max(requirement["end"], end_date)

    market_data = {}
    for symbol, bounds in requirements.items():
        frames = {}
        for adjust in ("hfq", ""):
            try:
                frames[adjust] = data_gateway.get_historical_prices(
                    symbol, bounds["start"], bounds["end"], adjust=adjust
                )
            except Exception as error:
                print(f"Failed to fetch {adjust or 'raw'} valuation range for {symbol}: {error}")
                frames[adjust] = pd.DataFrame()
        market_data[symbol] = frames

    return position_dates, market_data


def calc_nav():
    old_portfolio, _ = db_utils.load_portfolio_and_trades()
    today = clock.today()
    position_dates, market_data = _prepare_market_data(old_portfolio, today)
    cash_manager = CashManager()
    current_price_fallback = {}

    conn = db_utils.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT strategy_id, available_cash, total_capital FROM strategy_accounts")
        accounts = cursor.fetchall()

        for strategy, cash, _total_capital in accounts:
            holdings_value = 0.0
            positions = old_portfolio.get(strategy, {})

            for symbol, position in positions.items():
                entry_price = _positive_finite(position.get("entry_price"))
                tranches = _positive_finite(position.get("shares", 1)) or 1.0
                invested_capital = cash_manager.get_tranche_size(strategy) * tranches
                multiplier = 1.0

                valuation_dates = position_dates.get((strategy, symbol))
                if entry_price is not None and valuation_dates:
                    fetch_symbol, entry_date, end_date = valuation_dates
                    frames = market_data.get(fetch_symbol, {})
                    adjusted = frames.get("hfq", pd.DataFrame())
                    raw = frames.get("", pd.DataFrame())

                    current_price = _close_on_date(raw, end_date, "last")
                    if current_price is None:
                        if fetch_symbol not in current_price_fallback:
                            try:
                                current_price_fallback[fetch_symbol] = _positive_finite(
                                    data_gateway.get_current_price(fetch_symbol)
                                )
                            except Exception as error:
                                print(
                                    f"Failed to fetch current price for {symbol} in "
                                    f"{strategy} during NAV calculation: {error}"
                                )
                                current_price_fallback[fetch_symbol] = None
                        current_price = current_price_fallback[fetch_symbol]

                    if current_price is not None:
                        first_adjusted = _close_on_date(adjusted, entry_date, "first")
                        first_raw = _close_on_date(raw, entry_date, "first")
                        last_adjusted = _close_on_date(adjusted, end_date, "last")
                        last_raw = _close_on_date(raw, end_date, "last")

                        if all(
                            value is not None
                            for value in (first_adjusted, first_raw, last_adjusted, last_raw)
                        ):
                            factor_entry = first_adjusted / first_raw
                            factor_exit = last_adjusted / last_raw
                            if abs(factor_exit - factor_entry) / factor_entry > 0.2:
                                print(
                                    f"WARNING: Data source mix detected for {symbol}. "
                                    "Holding NAV at cost until data reconciles."
                                )
                            else:
                                adjusted_entry = entry_price * factor_entry
                                adjusted_current = current_price * factor_exit
                                multiplier = adjusted_current / adjusted_entry
                        else:
                            multiplier = current_price / entry_price

                if not math.isfinite(multiplier) or multiplier <= 0:
                    multiplier = 1.0
                holdings_value += invested_capital * multiplier

            total_nav = cash + holdings_value
            if not math.isfinite(total_nav):
                raise ValueError(f"Non-finite NAV for {strategy}")
            print(
                f"[NAV Tracker] {strategy} - NAV: {total_nav:,.2f} | "
                f"Cash: {cash:,.2f} | Holdings: {holdings_value:,.2f}"
            )
            cursor.execute(
                """
                INSERT OR REPLACE INTO strategy_nav_history
                    (date, strategy_id, nav, cash, holdings_value)
                VALUES (?, ?, ?, ?, ?)
                """,
                (today, strategy, total_nav, cash, holdings_value),
            )
            cursor.execute(
                "UPDATE strategy_accounts SET total_capital = ? WHERE strategy_id = ?",
                (total_nav, strategy),
            )

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    calc_nav()
