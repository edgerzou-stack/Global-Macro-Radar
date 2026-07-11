import datetime
import threading

class GlobalClock:
    """
    A unified time context manager that allows injecting a mock time for backtesting purposes.
    If no mock time is set, it defaults to the system's real time.
    """
    _instance = None
    _mock_time = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance
        
    def set_mock_time(self, mock_time: datetime.datetime):
        """Inject a specific time for backtesting."""
        self._mock_time = mock_time
        
    def clear_mock_time(self):
        """Restore real-time behavior."""
        self._mock_time = None
        
    def now(self, tz=None) -> datetime.datetime:
        """Returns the current simulated or real datetime."""
        if self._mock_time:
            if tz:
                return self._mock_time.astimezone(tz)
            return self._mock_time
        return datetime.datetime.now(tz)
        
    def today(self) -> datetime.date:
        """Returns the current simulated or real date."""
        if self._mock_time:
            return self._mock_time.date()
        return datetime.date.today()

# Global singleton instance
clock = GlobalClock()
