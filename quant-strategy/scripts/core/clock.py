import datetime
import os
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
                        
        return cls._instance
        
    def set_mock_time(self, mock_time: datetime.datetime):
        """Inject a specific time for backtesting."""
        with self._time_lock:
            self._mock_time = mock_time
        
    def clear_mock_time(self):
        """Restore real-time behavior."""
        with self._time_lock:
            self._mock_time = None
        
    def _get_explicit_mock_time(self):
        with self._time_lock:
            if self._mock_time:
                return self._mock_time
        return None

    @staticmethod
    def _get_environment_mock_date():
        mock_env = os.environ.get("MOCK_DATE")
        if mock_env:
            try:
                return datetime.date.fromisoformat(mock_env)
            except ValueError:
                raise ValueError(
                    f"Invalid MOCK_DATE {mock_env!r}; expected YYYY-MM-DD"
                )
        return None

    @staticmethod
    def _get_environment_mock_instant():
        mock_env = os.environ.get("MOCK_NOW_UTC")
        if not mock_env:
            return None
        try:
            instant = datetime.datetime.fromisoformat(mock_env.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                f"Invalid MOCK_NOW_UTC {mock_env!r}; expected timezone-aware ISO-8601"
            ) from error
        if instant.tzinfo is None:
            raise ValueError("MOCK_NOW_UTC must include an explicit timezone")
        return instant.astimezone(datetime.timezone.utc)

    @staticmethod
    def _localize_wall_time(value, tz):
        if tz is None:
            return value
        localize = getattr(tz, "localize", None)
        if callable(localize):
            return localize(value)
        return value.replace(tzinfo=tz)

    def now(self, tz=None) -> datetime.datetime:
        """Returns the current simulated or real datetime."""
        explicit = self._get_explicit_mock_time()
        if explicit is not None:
            if explicit.tzinfo is not None:
                return explicit.astimezone(tz) if tz else explicit
            return self._localize_wall_time(explicit, tz)

        instant = self._get_environment_mock_instant()
        if instant is not None:
            return instant.astimezone(tz) if tz else instant

        mock_date = self._get_environment_mock_date()
        if mock_date is not None:
            # Date-only fixtures represent a completed logical session, not a
            # physical midnight in the host timezone.  Localizing the same wall
            # date avoids silently shifting US markets to the previous day.
            wall_time = datetime.datetime.combine(mock_date, datetime.time.max)
            return self._localize_wall_time(wall_time, tz)
        return datetime.datetime.now(tz)
        
    def today(self) -> datetime.date:
        """Returns the current simulated or real date."""
        mock_date = self._get_environment_mock_date()
        if mock_date is not None:
            return mock_date
        explicit = self._get_explicit_mock_time()
        if explicit is not None:
            return explicit.date()
        instant = self._get_environment_mock_instant()
        if instant is not None:
            return instant.date()
        return datetime.date.today()

# Global singleton instance
clock = GlobalClock()
