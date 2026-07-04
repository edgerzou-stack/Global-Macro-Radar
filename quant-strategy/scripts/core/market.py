import datetime
import pytz

class Market:
    def __init__(self, name: str, timezone_str: str):
        self.name = name
        self.tz = pytz.timezone(timezone_str)
        
    def get_current_time(self) -> datetime.datetime:
        """Get the current time in the market's specific timezone."""
        from core.clock import clock
        return clock.now(self.tz)
        
    def is_trading_time(self) -> bool:
        """Determine if it is currently trading hours. Override in subclasses."""
        raise NotImplementedError

class AShareMarket(Market):
    def __init__(self):
        super().__init__("A-Share", "Asia/Shanghai")
        
    def is_trading_time(self) -> bool:
        now = self.get_current_time()
        if now.weekday() >= 5: # Saturday or Sunday
            return False
            
        t = now.time()
        # 09:30 - 11:30 and 13:00 - 15:00
        morning_open = datetime.time(9, 30)
        morning_close = datetime.time(11, 30)
        afternoon_open = datetime.time(13, 0)
        afternoon_close = datetime.time(15, 0)
        
        return (morning_open <= t <= morning_close) or (afternoon_open <= t <= afternoon_close)

class HKMarket(Market):
    def __init__(self):
        super().__init__("HK-Share", "Asia/Hong_Kong")
        
    def is_trading_time(self) -> bool:
        now = self.get_current_time()
        if now.weekday() >= 5:
            return False
            
        t = now.time()
        # 09:30 - 12:00 and 13:00 - 16:00
        morning_open = datetime.time(9, 30)
        morning_close = datetime.time(12, 0)
        afternoon_open = datetime.time(13, 0)
        afternoon_close = datetime.time(16, 0)
        
        return (morning_open <= t <= morning_close) or (afternoon_open <= t <= afternoon_close)

class USMarket(Market):
    def __init__(self):
        # US Eastern Time
        super().__init__("US-Share", "America/New_York")
        
    def is_trading_time(self) -> bool:
        now = self.get_current_time()
        if now.weekday() >= 5:
            return False
            
        t = now.time()
        # 09:30 - 16:00 ET
        market_open = datetime.time(9, 30)
        market_close = datetime.time(16, 0)
        
        return market_open <= t <= market_close
