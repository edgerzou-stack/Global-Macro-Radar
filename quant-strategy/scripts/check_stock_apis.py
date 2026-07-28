import sys
import os
import json
import logging
import datetime
import math
import pandas as pd
from core.data_gateway import DataGateway
from core.market import AShareMarket, USMarket

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("StockAPIHealthCheck")
STOCK_API_HEALTH_FIXTURE_SCHEMA_VERSION = 1
EXPECTED_HEALTH_SOURCES = {
    "baostock": ("600519", ""),
    "sina": ("600519", ""),
    "yfinance": ("AAPL", ""),
}


def validate_price_frame_fresh(frame, as_of, max_age_days=5):
    if frame is None or frame.empty or "日期" not in frame.columns or "收盘" not in frame.columns:
        raise ValueError("price health frame is empty or incomplete")
    parsed_dates = pd.to_datetime(frame["日期"].astype(str), errors="coerce")
    if parsed_dates.isna().any():
        raise ValueError("price health frame contains invalid dates")
    latest_index = parsed_dates.idxmax()
    latest_date = parsed_dates.loc[latest_index].date()
    if (as_of - latest_date).days > max_age_days:
        raise ValueError(
            f"stale market data: latest={latest_date}, as_of={as_of}"
        )
    price = float(frame.loc[latest_index, "收盘"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError("latest market price must be finite positive")
    return latest_date


def load_stock_api_health_fixture(path, as_of):
    fixture_path = os.path.abspath(os.fspath(path))
    try:
        with open(fixture_path, "r", encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Cannot load stock API health fixture {fixture_path}: {error}"
        ) from error
    if not isinstance(fixture, dict):
        raise ValueError("stock API health fixture must be a JSON object")
    if set(fixture) != {"schema_version", "as_of", "sources"}:
        raise ValueError("stock API health fixture has invalid top-level fields")
    if fixture.get("schema_version") != STOCK_API_HEALTH_FIXTURE_SCHEMA_VERSION:
        raise ValueError("unsupported stock API health fixture schema_version")
    if fixture.get("as_of") != as_of.isoformat():
        raise ValueError(
            f"stock API health fixture as_of must equal {as_of.isoformat()}"
        )
    sources = fixture.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(EXPECTED_HEALTH_SOURCES):
        raise ValueError("stock API health fixture has invalid source keys")

    frames = {}
    for source, (expected_symbol, expected_adjust) in EXPECTED_HEALTH_SOURCES.items():
        entry = sources[source]
        if not isinstance(entry, dict) or set(entry) != {"symbol", "adjust", "rows"}:
            raise ValueError(f"stock API health fixture {source} has invalid fields")
        if entry.get("symbol") != expected_symbol:
            raise ValueError(
                f"stock API health fixture {source} must use {expected_symbol}"
            )
        if entry.get("adjust") != expected_adjust:
            raise ValueError(
                f"stock API health fixture {source} has invalid adjust"
            )
        rows = entry.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError(
                f"stock API health fixture {source} rows must be non-empty"
            )
        frame = DataGateway._validate_prices(pd.DataFrame(rows), expected_symbol)
        parsed_dates = pd.to_datetime(frame["日期"], format="%Y%m%d", errors="coerce")
        if parsed_dates.isna().any() or (parsed_dates.dt.date > as_of).any():
            raise ValueError(
                f"stock API health fixture {source} contains invalid/future dates"
            )
        validate_price_frame_fresh(frame, as_of)
        frames[source] = frame
    return frames


def validate_a_share_cross_check(df_baostock, df_sina, *, strict_dates=False):
    bs_date = df_baostock.iloc[-1]['日期']
    sina_date = df_sina.iloc[-1]['日期']
    bs_price = float(df_baostock.iloc[-1]['收盘'])
    sina_price = float(df_sina.iloc[-1]['收盘'])
    if bs_date != sina_date:
        if strict_dates:
            raise ValueError(
                "A-share fixture source dates mismatch: "
                f"Baostock={bs_date}, Sina={sina_date}"
            )
        logger.warning(
            f"A-share Cross-Check Warning: Latest dates mismatch. "
            f"Baostock: {bs_date}, Sina: {sina_date}. Proceeding anyway."
        )
        return
    diff_pct = abs(bs_price - sina_price) / max(bs_price, sina_price)
    if diff_pct > 0.005:
        raise ValueError(
            "A-share cross-check divergence for 600519 on "
            f"{bs_date}: Baostock={bs_price}, Sina={sina_price}, diff={diff_pct:.2%}"
        )
    logger.info(
        f"A-share Cross-Check passed: Baostock ({bs_price}) vs "
        f"Sina ({sina_price}) on {bs_date}."
    )

def main():
    logger.info("Starting Pre-flight Health Check for Stock APIs...")
    from core.clock import clock

    fixture_path = os.environ.get("STOCK_API_HEALTH_FIXTURE")
    if fixture_path:
        try:
            frames = load_stock_api_health_fixture(fixture_path, clock.today())
            validate_a_share_cross_check(
                frames["baostock"], frames["sina"], strict_dates=True
            )
            validate_price_frame_fresh(frames["yfinance"], clock.today())
        except Exception as error:
            logger.error(f"Fixture-backed Stock API health check failed: {error}")
            sys.exit(1)
        logger.info("All fixture-backed Stock APIs are healthy. Ready for daily run.")
        sys.exit(0)

    dg = DataGateway()

    # Check A-share API (Cross-validation between Baostock and Sina)
    try:
        # Closing-bar health must stop at the latest completed market session.
        # During trading hours, requesting the logical run date would bypass a
        # valid prior-session cache and needlessly hit the live provider.
        end_dt = datetime.date.fromisoformat(
            AShareMarket().get_latest_completed_trading_date()
        )
        start_dt = end_dt - datetime.timedelta(days=7)
        start_str = start_dt.strftime("%Y%m%d")
        end_str = end_dt.strftime("%Y%m%d")

        # Test benchmark 600519 (Moutai)
        frames = {}
        failures = {}
        for source, fetch in (
            ("baostock", dg._fetch_from_baostock),
            ("sina", dg._fetch_from_sina),
        ):
            try:
                frame = fetch("600519", start_str, end_str, adjust="")
                validate_price_frame_fresh(frame, end_dt)
                frames[source] = frame
            except Exception as error:
                failures[source] = error
                logger.warning("A-share health source %s unavailable: %s", source, error)

        if len(frames) == 2:
            validate_a_share_cross_check(frames["baostock"], frames["sina"])
            logger.info("A-share API chain is healthy with two-source cross-check.")
        elif len(frames) == 1:
            source = next(iter(frames))
            logger.warning(
                "A-share API chain is degraded but operational through validated "
                "%s data; unavailable sources=%s",
                source,
                sorted(failures),
            )
        else:
            # The actual gateway is cache-first.  A complete, strictly validated
            # completed-session cache is an acceptable temporary degraded mode;
            # stale or absent cache still fails closed.
            cached = dg.get_historical_prices(
                "600519", end_str, end_str, adjust=""
            )
            validate_price_frame_fresh(cached, end_dt)
            logger.warning(
                "A-share live sources are unavailable; proceeding with an exact "
                "completed-session validated cache row. failures=%s",
                sorted(failures),
            )
    except Exception as e:
        logger.error(f"A-share API chain failed: {e}", exc_info=True)
        sys.exit(1)

    # Check US/HK API (YFinance)
    try:
        end_dt = datetime.date.fromisoformat(
            USMarket().get_latest_completed_trading_date()
        )
        session = end_dt.strftime("%Y%m%d")
        # A health probe needs one authoritative completed close, not an
        # arbitrary ten-day range.  This maximizes safe cache reuse and only
        # contacts Yahoo when the completed-session row is genuinely absent.
        df_us = dg.get_historical_prices(
            "AAPL", session, session
        )
        validate_price_frame_fresh(df_us, end_dt)
        logger.info("US-share market-data path is healthy through %s.", session)
    except Exception as e:
        logger.error(f"US-share API failed: {e}")
        sys.exit(1)

    logger.info("Required stock data paths are operational. Ready for daily run.")
    sys.exit(0)

if __name__ == "__main__":
    main()
