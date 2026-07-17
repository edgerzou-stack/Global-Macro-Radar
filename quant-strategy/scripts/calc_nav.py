import datetime
import math

import pandas as pd

import db_utils
from core.cash_manager import CashManager
from core.clock import clock
from core.data_gateway import DataGateway
from core.quarantine import quarantine_filter, quarantined_primary_keys


data_gateway = DataGateway()


class ValuationUnavailableError(RuntimeError):
    """Raised when a production NAV cannot be supported by exact market data."""


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


def _close_on_date(frame, date):
    """Return the exact session close; never substitute another row."""
    if (
        frame is None
        or frame.empty
        or "日期" not in frame.columns
        or "收盘" not in frame.columns
    ):
        return None
    dates = frame["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
    exact = frame[dates == date]
    if exact.empty:
        return None
    return _positive_finite(exact.iloc[-1].get("收盘"))


def _prepare_market_data(portfolio, today):
    """Fetch each symbol/adjust pair once for the complete valuation range."""
    requirements = {}
    position_dates = {}

    for strategy, positions in portfolio.items():
        market = _market_for_strategy(strategy)
        end_raw = datetime.datetime.strptime(
            market.get_latest_completed_trading_date(), "%Y-%m-%d"
        ).date()
        end_date = end_raw.strftime("%Y%m%d")
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
                frames[adjust] = data_gateway.get_historical_closes(
                    symbol, bounds["start"], bounds["end"], adjust=adjust
                )
            except Exception as error:
                print(f"Failed to fetch {adjust or 'raw'} valuation range for {symbol}: {error}")
                frames[adjust] = pd.DataFrame()
        market_data[symbol] = frames

    return position_dates, market_data


def _position_multiplier(strategy, symbol, position, position_dates, market_data):
    entry_price = _positive_finite(position.get("entry_price"))
    if entry_price is None:
        raise ValuationUnavailableError(
            f"{strategy}/{symbol} has no authoritative positive entry price"
        )

    valuation_dates = position_dates.get((strategy, symbol))
    if valuation_dates is None:
        raise ValuationUnavailableError(
            f"{strategy}/{symbol} has no validated valuation date range"
        )

    fetch_symbol, entry_date, end_date = valuation_dates
    frames = market_data.get(fetch_symbol, {})
    adjusted = frames.get("hfq", pd.DataFrame())
    raw = frames.get("", pd.DataFrame())
    first_adjusted = _close_on_date(adjusted, entry_date)
    first_raw = _close_on_date(raw, entry_date)
    last_adjusted = _close_on_date(adjusted, end_date)
    last_raw = _close_on_date(raw, end_date)
    required = {
        "entry_adjusted": first_adjusted,
        "entry_raw": first_raw,
        "valuation_adjusted": last_adjusted,
        "valuation_raw": last_raw,
    }
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise ValuationUnavailableError(
            f"{strategy}/{symbol} is missing exact-session valuation fields: "
            + ", ".join(missing)
        )

    factor_entry = first_adjusted / first_raw
    factor_exit = last_adjusted / last_raw
    factor_drift = abs(factor_exit - factor_entry) / factor_entry
    if not math.isfinite(factor_drift) or factor_drift > 0.2:
        raise ValuationUnavailableError(
            f"{strategy}/{symbol} adjusted/raw factor drift is unsafe: "
            f"{factor_drift:.6f}"
        )

    multiplier = (last_raw * factor_exit) / (entry_price * factor_entry)
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise ValuationUnavailableError(
            f"{strategy}/{symbol} produced an invalid NAV multiplier"
        )
    return multiplier


def calc_nav():
    old_portfolio, _ = db_utils.load_portfolio_and_trades()
    today = clock.today()
    position_dates, market_data = _prepare_market_data(old_portfolio, today)
    cash_manager = CashManager()

    conn = db_utils.get_connection()
    try:
        cursor = conn.cursor()
        account_filter, account_parameters, _ = quarantine_filter(
            conn, "strategy_accounts"
        )
        cursor.execute(
            "SELECT strategy_id, available_cash, total_capital "
            "FROM strategy_accounts WHERE 1=1" + account_filter,
            account_parameters,
        )
        accounts = cursor.fetchall()
        quarantined_nav_keys = quarantined_primary_keys(
            conn, "strategy_nav_history"
        )
        valuations = []
        for strategy, cash, _total_capital in accounts:
            if (str(today), strategy) in quarantined_nav_keys:
                raise ValuationUnavailableError(
                    f"{strategy} NAV row for {today} is quarantined"
                )
            cash = _positive_finite(cash) if cash != 0 else 0.0
            if cash is None:
                raise ValuationUnavailableError(
                    f"{strategy} has invalid available cash"
                )
            positions = old_portfolio.get(strategy, {})
            holding_values = []
            for symbol, position in positions.items():
                tranches = _positive_finite(position.get("shares"))
                if tranches is None:
                    raise ValuationUnavailableError(
                        f"{strategy}/{symbol} has invalid tranche quantity"
                    )
                invested_capital = cash_manager.get_tranche_size(strategy) * tranches
                multiplier = _position_multiplier(
                    strategy,
                    symbol,
                    position,
                    position_dates,
                    market_data,
                )
                holding_values.append(invested_capital * multiplier)

            holdings_value = math.fsum(holding_values)
            total_nav = cash + holdings_value
            if not math.isfinite(total_nav):
                raise ValueError(f"Non-finite NAV for {strategy}")
            valuations.append((strategy, cash, holdings_value, total_nav))

        cursor.execute("BEGIN TRANSACTION")
        for strategy, cash, holdings_value, total_nav in valuations:
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
        return {
            "date": str(today),
            "strategies": len(valuations),
            "positions": sum(len(positions) for positions in old_portfolio.values()),
            "valuation_coverage": 1.0,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    calc_nav()
