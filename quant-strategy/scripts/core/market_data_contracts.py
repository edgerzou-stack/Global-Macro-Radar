from typing import Protocol

import pandas as pd


class DataIntegrityError(Exception):
    """Raised when fetched data fails integrity checks."""


class InvalidMarketDataRequest(DataIntegrityError):
    """Raised for a locally invalid request before contacting a provider."""


class CircuitBreakerError(Exception):
    """Raised when a provider is disabled for the current gateway run."""


class FatalSystemError(Exception):
    """Raised when required market data has no safe provider fallback."""


class MarketDataProvider(Protocol):
    def fetch(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjust: str = "",
    ) -> pd.DataFrame:
        ...
