import datetime
import threading

class GlobalClock:
    """
    A unified time context manager that allows injecting a mock time for backtesting purposes.
    If no mock time is set, it defaults to the system's real time.
    """
    _instance = None
    _singleton_lock = threading.Lock()
    
    def __new__(cls):
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._mock_time = None
                cls._instance._time_lock = threading.Lock()
                
                # Check for MOCK_DATE environment variable
                import os
                mock_env = os.environ.get("MOCK_DATE")
                if mock_env:
                    try:
                        # Parse 'YYYY-MM-DD'
                        dt = datetime.datetime.strptime(mock_env, "%Y-%m-%d")
                        cls._instance._mock_time = dt
                        print(f"[GlobalClock] MOCK_DATE env var detected. System time locked to: {dt.date()}")
                    except ValueError:
                        print(f"[GlobalClock] WARNING: Invalid MOCK_DATE format '{mock_env}'. Expected YYYY-MM-DD.")
                        
        return cls._instance
        
    def set_mock_time(self, mock_time: datetime.datetime):
        """Inject a specific time for backtesting."""
        with self._time_lock:
            self._mock_time = mock_time
        
    def clear_mock_time(self):
        """Restore real-time behavior."""
        with self._time_lock:
            self._mock_time = None
        
    def _get_mock_time(self):
        with self._time_lock:
            if self._mock_time:
                return self._mock_time
        import os
        mock_env = os.environ.get("MOCK_DATE")
        if mock_env:
            try:
                return datetime.datetime.strptime(mock_env, "%Y-%m-%d")
            except ValueError:
                pass
        return None

    def now(self, tz=None) -> datetime.datetime:
        """Returns the current simulated or real datetime."""
        mock = self._get_mock_time()
        if mock:
            if tz:
                return mock.astimezone(tz)
            return mock
        return datetime.datetime.now(tz)
        
    def today(self) -> datetime.date:
        """Returns the current simulated or real date."""
        mock = self._get_mock_time()
        if mock:
            return mock.date()
        return datetime.date.today()

# Global singleton instance
clock = GlobalClock()
