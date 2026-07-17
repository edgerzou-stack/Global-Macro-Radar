import datetime
import pytz
import pandas_market_calendars as mcal

class Market:
    def __init__(self, name: str, timezone_str: str, calendar_name: str = None):
        self.name = name
        self.tz = pytz.timezone(timezone_str)
        self.calendar = mcal.get_calendar(calendar_name) if calendar_name else None

    def get_current_time(self) -> datetime.datetime:
        """Get the current time in the market's specific timezone."""
        from core.clock import clock
        return clock.now(self.tz)

    def is_trading_day(self) -> bool:
        """Check if today is a trading day using market calendar."""
        if not self.calendar:
            now = self.get_current_time()
            return now.weekday() < 5

        now = self.get_current_time()
        date_str = now.strftime('%Y-%m-%d')
        # Check if today is in the valid trading days
        schedule = self.calendar.valid_days(start_date=date_str, end_date=date_str)
        return len(schedule) > 0

    def is_trading_time(self) -> bool:
        """Determine if it is currently trading hours. Override in subclasses."""
        raise NotImplementedError

    def get_effective_trading_date(self) -> str:
        """
        Returns the effective trading session date string (YYYY-MM-DD).
        If it's before the market open or a non-trading day, returns the previous trading day.
        If the market is open or already closed for the day, returns today.
        """
        now_local = self.get_current_time()
        start_date = (now_local - datetime.timedelta(days=30)).date()
        end_date = now_local.date()

        if not self.calendar:
            # Fallback
            current = now_local
            while current.weekday() >= 5:
                current -= datetime.timedelta(days=1)
            return current.strftime('%Y-%m-%d')

        try:
            schedule = self.calendar.schedule(start_date=start_date, end_date=end_date)
            now_utc = now_local.astimezone(pytz.utc)
            import pandas as pd

            today_ts = pd.Timestamp(now_local.date())
            if today_ts in schedule.index:
                row = schedule.loc[today_ts]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[0]
                market_open_utc = row['market_open']
                if now_utc >= market_open_utc:
                    return today_ts.strftime('%Y-%m-%d')

            valid_days = schedule[schedule['market_open'] <= pd.Timestamp(now_utc)]
            if not valid_days.empty:
                return valid_days.index[-1].strftime('%Y-%m-%d')
            return start_date.strftime('%Y-%m-%d')
        except Exception as e:
            import logging
            logging.debug(f"mcal schedule failed: {e}. Falling back to weekday logic.")
            current = now_local
            while current.weekday() >= 5:
                current -= datetime.timedelta(days=1)
            return current.strftime('%Y-%m-%d')

    def get_latest_completed_trading_date(self) -> str:
        """Return the latest session whose official close is in the past.

        This is deliberately stricter than ``get_effective_trading_date``.
        NAV uses closing bars, so an open session is not yet a valid valuation
        date even though it is already the effective execution session.
        """
        if not self.calendar:
            raise RuntimeError(f"{self.name} has no authoritative market calendar")

        now_local = self.get_current_time()
        start_date = (now_local - datetime.timedelta(days=30)).date()
        try:
            import pandas as pd

            schedule = self.calendar.schedule(
                start_date=start_date,
                end_date=now_local.date(),
            )
            now_utc = pd.Timestamp(now_local.astimezone(pytz.utc))
            completed = schedule[schedule["market_close"] <= now_utc]
            if completed.empty:
                raise RuntimeError(
                    f"{self.name} calendar has no completed session in lookback"
                )
            return completed.index[-1].strftime("%Y-%m-%d")
        except Exception as error:
            raise RuntimeError(
                f"Unable to determine latest completed {self.name} session"
            ) from error

    def get_next_trading_date(self, target_date: datetime.date) -> datetime.date:
        """
        Returns the exact next available trading day after or on the target_date.
        """
        if not self.calendar:
            # Fallback naive logic
            d = target_date
            while d.weekday() >= 5:
                d += datetime.timedelta(days=1)
            return d

        try:
            import pandas as pd
            end_date = target_date + datetime.timedelta(days=30)
            schedule = self.calendar.valid_days(start_date=target_date, end_date=end_date)
            if len(schedule) > 0:
                # The valid_days returns a DatetimeIndex
                return schedule[0].date()
            return target_date
        except Exception as e:
            import logging
            logging.error(f"Failed to calculate next trading date for {target_date}: {e}")
            return target_date

    def get_previous_trading_date(self, target_date: datetime.date) -> datetime.date:
        """
        Returns the exact previous or current trading day for the target_date.
        """
        if not self.calendar:
            d = target_date
            while d.weekday() >= 5:
                d -= datetime.timedelta(days=1)
            return d

        try:
            import pandas as pd
            start_date = target_date - datetime.timedelta(days=30)
            schedule = self.calendar.valid_days(start_date=start_date, end_date=target_date)
            if len(schedule) > 0:
                return schedule[-1].date()
            return target_date
        except Exception as e:
            import logging
            logging.error(f"Failed to calculate previous trading date for {target_date}: {e}")
            return target_date

    def get_most_recent_trading_day(self, target_date: datetime.date) -> datetime.date:
        """
        Returns the exact most recent trading day on or before the target_date.
        """
        if not self.calendar:
            d = target_date
            while d.weekday() >= 5:
                d -= datetime.timedelta(days=1)
            return d

        try:
            import pandas as pd
            start_date = target_date - datetime.timedelta(days=30)
            schedule = self.calendar.valid_days(start_date=start_date, end_date=target_date)
            if len(schedule) > 0:
                # The valid_days returns a DatetimeIndex
                return schedule[-1].date()
            return target_date
        except Exception as e:
            import logging
            logging.error(f"Failed to get most recent trading date from calendar: {e}")
            d = target_date
            while d.weekday() >= 5:
                d -= datetime.timedelta(days=1)
            return d

class AShareMarket(Market):
    def __init__(self):
        super().__init__("A-Share", "Asia/Shanghai", "XSHG")

    def is_trading_time(self) -> bool:
        if not self.is_trading_day():
            return False

        t = self.get_current_time().time()
        morning_open = datetime.time(9, 30)
        morning_close = datetime.time(11, 30)
        afternoon_open = datetime.time(13, 0)
        afternoon_close = datetime.time(15, 0)

        return (morning_open <= t <= morning_close) or (afternoon_open <= t <= afternoon_close)

class HKMarket(Market):
    def __init__(self):
        super().__init__("HK-Share", "Asia/Hong_Kong", "XHKG")

    def is_trading_time(self) -> bool:
        if not self.is_trading_day():
            return False

        t = self.get_current_time().time()
        morning_open = datetime.time(9, 30)
        morning_close = datetime.time(12, 0)
        afternoon_open = datetime.time(13, 0)
        afternoon_close = datetime.time(16, 0)

        return (morning_open <= t <= morning_close) or (afternoon_open <= t <= afternoon_close)

class USMarket(Market):
    def __init__(self):
        super().__init__("US-Share", "America/New_York", "XNYS")

    def is_trading_time(self) -> bool:
        if not self.is_trading_day():
            return False

        t = self.get_current_time().time()
        # 09:30 - 16:00 ET
        market_open = datetime.time(9, 30)
        market_close = datetime.time(16, 0)

        return market_open <= t <= market_close
