import datetime
import json
import math
import os

import pandas as pd

import db_utils
from core.cash_manager import CashManager
from core.clock import clock
from core.data_gateway import DataGateway
from core.quarantine import quarantine_filter, quarantined_primary_keys


data_gateway = DataGateway()


class ValuationUnavailableError(RuntimeError):
    """Raised when a production NAV cannot be supported by exact market data."""


class LedgerIntegrityError(RuntimeError):
    """Raised when persisted ledger values are internally invalid."""


NAV_RUN_STATUS_PREFIX = "nav_run_status:"


def _run_id(today):
    return (
        os.environ.get("PIPELINE_RUN_ID")
        or os.environ.get("RUN_ID")
        or f"manual-{today}"
    )


def _generated_at():
    instant = clock.now(datetime.timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=datetime.timezone.utc)
    return instant.astimezone(datetime.timezone.utc).isoformat()


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


def _nonnegative_finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


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


def _adjustment_factor_drift(frames, start_date, end_date):
    adjusted = frames.get("hfq", pd.DataFrame())
    raw = frames.get("", pd.DataFrame())
    first_adjusted = _close_on_date(adjusted, start_date)
    first_raw = _close_on_date(raw, start_date)
    last_adjusted = _close_on_date(adjusted, end_date)
    last_raw = _close_on_date(raw, end_date)
    if None in (first_adjusted, first_raw, last_adjusted, last_raw):
        return None
    factor_entry = first_adjusted / first_raw
    factor_exit = last_adjusted / last_raw
    return abs(factor_exit - factor_entry) / factor_entry


def _prepare_market_data(portfolio, today, valuation_errors=None):
    """Prepare exact-session prices and explicitly track active-session entries."""
    requirements = {}
    position_dates = {}
    unsettled_positions = set()
    for strategy, positions in portfolio.items():
        market = _market_for_strategy(strategy)
        try:
            end_raw = datetime.datetime.strptime(
                market.get_latest_completed_trading_date(), "%Y-%m-%d"
            ).date()
        except Exception as error:
            if valuation_errors is None:
                raise
            for symbol in positions:
                valuation_errors[(strategy, symbol)] = (
                    f"{strategy} valuation calendar is unavailable: {error}"
                )
            continue
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
            if entry_date > end_date:
                try:
                    effective_session = market.get_effective_trading_date().replace(
                        "-", ""
                    )
                except (AttributeError, RuntimeError, ValueError):
                    effective_session = None
                if (
                    os.environ.get("PIPELINE_ENFORCE_SESSION_IDENTITY") == "1"
                    and entry_date == effective_session
                ):
                    # A position executed in the currently active session has
                    # no official close yet.  Until that session completes,
                    # value only that exact position at authoritative cost.
                    unsettled_positions.add((strategy, symbol))
                    continue
                message = (
                    f"{strategy}/{symbol} entry session {entry_date} is later "
                    f"than latest completed valuation session {end_date} and "
                    f"does not match active market session "
                    f"{effective_session or 'unknown'}"
                )
                if valuation_errors is None:
                    raise ValuationUnavailableError(message)
                valuation_errors[(strategy, symbol)] = message
                continue
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

        factor_drift = _adjustment_factor_drift(
            frames, bounds["start"], bounds["end"]
        )
        if factor_drift is not None and (
            not math.isfinite(factor_drift) or factor_drift > 0.2
        ):
            reason = (
                "adjusted/raw factor drift requires a clean paired refresh: "
                f"{factor_drift:.6f}"
            )
            try:
                frames = data_gateway.refresh_valuation_closes(
                    symbol, bounds["start"], bounds["end"], reason
                )
            except Exception as error:
                print(f"Failed to refresh mismatched valuation series for {symbol}: {error}")
        market_data[symbol] = frames

    return position_dates, market_data, unsettled_positions


def _position_multiplier(
    strategy,
    symbol,
    position,
    position_dates,
    market_data,
    unsettled_positions,
    valuation_errors=None,
):
    entry_price = _positive_finite(position.get("entry_price"))
    if entry_price is None:
        raise ValuationUnavailableError(
            f"{strategy}/{symbol} has no authoritative positive entry price"
        )

    position_key = (strategy, symbol)
    if valuation_errors and position_key in valuation_errors:
        raise ValuationUnavailableError(valuation_errors[position_key])
    if position_key in unsettled_positions:
        return 1.0

    valuation_dates = position_dates.get(position_key)
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


def _latest_certified_snapshot(
    cursor,
    strategy,
    today,
    nav_filter,
    nav_parameters,
):
    cursor.execute(
        "SELECT date,nav,cash,holdings_value FROM strategy_nav_history "
        "WHERE strategy_id=? AND date<=?"
        + nav_filter
        + " ORDER BY date DESC LIMIT 1",
        (strategy, str(today)) + nav_parameters,
    )
    row = cursor.fetchone()
    if row is None:
        return None

    snapshot_date, nav, cash, holdings_value = row
    nav = _nonnegative_finite(nav)
    cash = _nonnegative_finite(cash)
    holdings_value = _nonnegative_finite(holdings_value)
    if None in (nav, cash, holdings_value):
        raise LedgerIntegrityError(
            f"{strategy} latest non-quarantined NAV snapshot contains invalid values"
        )
    tolerance = max(0.01, abs(nav) * 1e-8)
    if abs(nav - (cash + holdings_value)) > tolerance:
        raise LedgerIntegrityError(
            f"{strategy} latest non-quarantined NAV snapshot violates "
            "nav = cash + holdings_value"
        )
    return {
        "snapshot_date": str(snapshot_date),
        "nav": nav,
        "cash": cash,
        "holdings_value": holdings_value,
    }


def _validate_ledger_inputs(strategy, cash, positions):
    normalized_cash = _nonnegative_finite(cash)
    if normalized_cash is None:
        raise LedgerIntegrityError(f"{strategy} has invalid available cash")
    for symbol, position in positions.items():
        if _positive_finite(position.get("entry_price")) is None:
            raise LedgerIntegrityError(
                f"{strategy}/{symbol} has no authoritative positive entry price"
            )
        if _positive_finite(position.get("shares")) is None:
            raise LedgerIntegrityError(
                f"{strategy}/{symbol} has invalid tranche quantity"
            )
    return normalized_cash


def _valuation_session(strategy):
    return _market_for_strategy(strategy).get_latest_completed_trading_date()


def calc_nav():
    old_portfolio, _ = db_utils.load_portfolio_and_trades()
    today = clock.today()
    cash_manager = CashManager()
    run_id = _run_id(today)

    conn = db_utils.get_connection()
    try:
        cursor = conn.cursor()
        account_filter, account_parameters, _ = quarantine_filter(
            conn, "strategy_accounts"
        )
        test_filter, test_parameters = db_utils.test_strategy_filter("strategy_id")
        cursor.execute(
            "SELECT strategy_id, available_cash, total_capital "
            "FROM strategy_accounts WHERE 1=1" + account_filter + test_filter,
            account_parameters + test_parameters,
        )
        accounts = cursor.fetchall()
        quarantined_nav_keys = quarantined_primary_keys(
            conn, "strategy_nav_history"
        )
        valuations = []
        strategy_statuses = {}
        valuation_errors = {}
        position_dates, market_data, unsettled_positions = _prepare_market_data(
            old_portfolio,
            today,
            valuation_errors=valuation_errors,
        )
        nav_filter, nav_parameters, _ = quarantine_filter(
            conn, "strategy_nav_history"
        )
        for strategy, cash, _total_capital in accounts:
            if (str(today), strategy) in quarantined_nav_keys:
                raise LedgerIntegrityError(
                    f"{strategy} NAV row for {today} is quarantined"
                )
            positions = old_portfolio.get(strategy, {})
            cash = _validate_ledger_inputs(strategy, cash, positions)
            certified_snapshot = _latest_certified_snapshot(
                cursor,
                strategy,
                today,
                nav_filter,
                nav_parameters,
            )
            try:
                valuation_session = _valuation_session(strategy)
                holding_values = []
                for symbol, position in positions.items():
                    tranches = float(position["shares"])
                    invested_capital = (
                        cash_manager.get_tranche_size(strategy) * tranches
                    )
                    multiplier = _position_multiplier(
                        strategy,
                        symbol,
                        position,
                        position_dates,
                        market_data,
                        unsettled_positions,
                        valuation_errors,
                    )
                    holding_values.append(invested_capital * multiplier)

                holdings_value = math.fsum(holding_values)
                total_nav = cash + holdings_value
                if (
                    not math.isfinite(holdings_value)
                    or holdings_value < 0
                    or not math.isfinite(total_nav)
                    or total_nav < 0
                ):
                    raise LedgerIntegrityError(
                        f"{strategy} produced a non-finite or negative NAV"
                    )
                valuations.append((strategy, cash, holdings_value, total_nav))
                strategy_statuses[strategy] = {
                    "status": "fresh",
                    "nav": total_nav,
                    "snapshot_date": str(today),
                    "valuation_session": str(valuation_session),
                    "snapshot_cash": cash,
                    "snapshot_holdings_value": holdings_value,
                    "current_available_cash": cash,
                    "failure_reason": None,
                }
            except ValuationUnavailableError as error:
                if certified_snapshot is None:
                    strategy_statuses[strategy] = {
                        "status": "unavailable",
                        "nav": None,
                        "snapshot_date": None,
                        "valuation_session": None,
                        "snapshot_cash": None,
                        "snapshot_holdings_value": None,
                        "current_available_cash": cash,
                        "failure_reason": str(error),
                    }
                else:
                    strategy_statuses[strategy] = {
                        "status": "certified_carry_forward",
                        "nav": certified_snapshot["nav"],
                        "snapshot_date": certified_snapshot["snapshot_date"],
                        "valuation_session": None,
                        "snapshot_cash": certified_snapshot["cash"],
                        "snapshot_holdings_value": certified_snapshot[
                            "holdings_value"
                        ],
                        "current_available_cash": cash,
                        "failure_reason": str(error),
                    }

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

        status_payload = {
            "schema_version": 1,
            "run_id": run_id,
            "report_date": str(today),
            "generated_at": _generated_at(),
            "strategies": strategy_statuses,
        }
        cursor.execute(
            "REPLACE INTO meta_data (key,value) VALUES (?,?)",
            (
                NAV_RUN_STATUS_PREFIX + run_id,
                json.dumps(status_payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
        counts = {
            status: sum(
                item["status"] == status for item in strategy_statuses.values()
            )
            for status in ("fresh", "certified_carry_forward", "unavailable")
        }
        for strategy, item in strategy_statuses.items():
            if item["status"] != "fresh":
                print(
                    f"[NAV Tracker] {strategy} - {item['status']}: "
                    f"{item['failure_reason']}"
                )
        return {
            "date": str(today),
            "run_id": run_id,
            "strategies": len(strategy_statuses),
            "positions": sum(len(positions) for positions in old_portfolio.values()),
            "valuation_coverage": (
                counts["fresh"] / len(strategy_statuses)
                if strategy_statuses
                else 1.0
            ),
            "status_counts": counts,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    calc_nav()
