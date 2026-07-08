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
