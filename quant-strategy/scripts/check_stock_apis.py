import sys
import os
import logging
import datetime
import math
import pandas as pd
from core.data_gateway import DataGateway

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("StockAPIHealthCheck")


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

def main():
    logger.info("Starting Pre-flight Health Check for Stock APIs...")
    dg = DataGateway()

    # Check A-share API (Cross-validation between Baostock and Sina)
    try:
        from core.clock import clock
        # Use a recent trading date. We just fetch the last 7 days and take the most recent overlapping day.
        end_dt = clock.today()
        start_dt = end_dt - datetime.timedelta(days=7)
        start_str = start_dt.strftime("%Y%m%d")
        end_str = end_dt.strftime("%Y%m%d")

        # Test benchmark 600519 (Moutai)
        df_baostock = dg._fetch_from_baostock("600519", start_str, end_str, adjust="")
        df_sina = dg._fetch_from_sina("600519", start_str, end_str, adjust="")

        if df_baostock.empty or df_sina.empty:
            logger.error(f"A-share fetch returned empty. Baostock: {not df_baostock.empty}, Sina: {not df_sina.empty}")
            sys.exit(1)

        validate_price_frame_fresh(df_baostock, end_dt)
        validate_price_frame_fresh(df_sina, end_dt)

        # Get the latest overlapping date
        bs_date = df_baostock.iloc[-1]['日期']
        sina_date = df_sina.iloc[-1]['日期']

        bs_price = float(df_baostock.iloc[-1]['收盘'])
        sina_price = float(df_sina.iloc[-1]['收盘'])

        # If dates match, cross-check the price discrepancy
        if bs_date == sina_date:
            diff_pct = abs(bs_price - sina_price) / max(bs_price, sina_price)
            if diff_pct > 0.005:  # 0.5% threshold
                logger.error(f"CROSS-CHECK FAILED! Baostock and Sina report divergent prices for 600519 on {bs_date}. Baostock: {bs_price}, Sina: {sina_price}. Diff: {diff_pct:.2%}")
                sys.exit(1)
            else:
                logger.info(f"A-share Cross-Check passed: Baostock ({bs_price}) vs Sina ({sina_price}) on {bs_date}.")
        else:
            logger.warning(f"A-share Cross-Check Warning: Latest dates mismatch. Baostock: {bs_date}, Sina: {sina_date}. Proceeding anyway.")

        logger.info("A-share API chain is healthy.")
    except Exception as e:
        logger.error(f"A-share API chain failed: {e}", exc_info=True)
        sys.exit(1)

    # Check US/HK API (YFinance)
    try:
        end_dt = clock.today()
        start_dt = end_dt - datetime.timedelta(days=10)
        df_us = dg.get_historical_prices(
            "AAPL", start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")
        )
        validate_price_frame_fresh(df_us, end_dt)
        logger.info("US-share API is healthy.")
    except Exception as e:
        logger.error(f"US-share API failed: {e}")
        sys.exit(1)

    logger.info("All Stock APIs are healthy. Ready for daily run.")
    sys.exit(0)

if __name__ == "__main__":
    main()
