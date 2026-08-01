"""Resolve display names without coupling report rendering to market providers.

The compatibility ``get_stock_name`` function remains available, but cache
state is loaded once per process and persisted atomically.  Callers on a
latency- or reliability-sensitive path can set ``allow_network=False`` and get
cache-only behaviour.
"""

from __future__ import annotations

import atexit
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterable, Mapping


CACHE_FILE = Path(__file__).resolve().parent.parent / "ticker_names.json"
NameLoader = Callable[[str], str | None]
AShareLoader = Callable[[], Mapping[str, str]]


def _usable_name(symbol: object, name: object) -> str | None:
    normalized_symbol = str(symbol or "").strip()
    normalized_name = str(name or "").strip()
    if (
        not normalized_symbol
        or not normalized_name
        or normalized_name == normalized_symbol
    ):
        return None
    return normalized_name


def _default_a_share_loader() -> dict[str, str]:
    import akshare as ak

    frame = ak.stock_info_a_code_name()
    return {
        str(row["code"]).zfill(6): str(row["name"]).strip()
        for _, row in frame.iterrows()
        if str(row.get("code") or "").strip()
        and str(row.get("name") or "").strip()
    }


def _default_international_loader(symbol: str) -> str | None:
    import yfinance as yf

    info = yf.Ticker(symbol).info
    return str(info.get("shortName") or info.get("longName") or "").strip() or None


class StockNameResolver:
    """Process-local name cache with optional, injectable provider fallbacks."""

    def __init__(
        self,
        cache_file: str | os.PathLike[str] = CACHE_FILE,
        *,
        a_share_loader: AShareLoader | None = None,
        international_loader: NameLoader | None = None,
    ) -> None:
        self.cache_file = Path(cache_file)
        self._a_share_loader = a_share_loader or _default_a_share_loader
        self._international_loader = (
            international_loader or _default_international_loader
        )
        self._lock = threading.RLock()
        self._cache = self._read_cache()
        self._dirty: dict[str, str] = {}
        self._a_share_names: Mapping[str, str] | None = None

    def _read_cache(self) -> dict[str, str]:
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        result = {}
        for raw_symbol, raw_name in payload.items():
            symbol = str(raw_symbol).strip()
            name = _usable_name(symbol, raw_name)
            if symbol and name:
                result[symbol] = name
        return result

    def prime(self, names: Mapping[str, str], *, persist: bool = False) -> None:
        """Add authoritative names already present in an upstream payload."""
        with self._lock:
            for raw_symbol, raw_name in names.items():
                symbol = str(raw_symbol).strip()
                name = _usable_name(symbol, raw_name)
                if not symbol or name is None:
                    continue
                self._cache[symbol] = name
                if persist:
                    self._dirty[symbol] = name

    def resolve(self, code: object, *, allow_network: bool = True) -> str:
        symbol = str(code or "").strip()
        if not symbol:
            return ""

        with self._lock:
            cached = self._cache.get(symbol)
            if cached:
                return cached
            if not allow_network:
                return symbol

            name = _usable_name(symbol, self._resolve_uncached(symbol))
            resolved = name or symbol
            self._cache[symbol] = resolved
            if name:
                # Provider outages are cached for this process only. Persisting
                # a symbol-only fallback would suppress recovery on later runs.
                self._dirty[symbol] = resolved
            return resolved

    def resolve_many(
        self, codes: Iterable[object], *, allow_network: bool = True
    ) -> dict[str, str]:
        """Resolve unique symbols once while preserving first-seen order."""
        symbols = list(dict.fromkeys(str(code or "").strip() for code in codes))
        return {
            symbol: self.resolve(symbol, allow_network=allow_network)
            for symbol in symbols
            if symbol
        }

    def _resolve_uncached(self, symbol: str) -> str | None:
        try:
            if symbol.isdigit() and len(symbol) == 6:
                if self._a_share_names is None:
                    self._a_share_names = self._a_share_loader()
                return str(self._a_share_names.get(symbol) or "").strip() or None
            return self._international_loader(symbol)
        except Exception:
            # A display name is optional. Provider failure must not block a
            # truthful report; the canonical symbol remains the fallback.
            return None

    def flush(self) -> bool:
        """Merge pending names into the on-disk cache with one atomic replace."""
        with self._lock:
            if not self._dirty:
                return False
            merged = self._read_cache()
            merged.update(self._dirty)
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.cache_file.name}.",
                suffix=".tmp",
                dir=self.cache_file.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(
                        merged,
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.cache_file)
            finally:
                if temporary.exists():
                    temporary.unlink()
            self._cache = merged
            self._dirty.clear()
            return True


_DEFAULT_RESOLVER = StockNameResolver()


def get_stock_name(code: object, *, allow_network: bool = True) -> str:
    """Compatibility wrapper around the process-local resolver."""
    return _DEFAULT_RESOLVER.resolve(code, allow_network=allow_network)


def get_stock_names(
    codes: Iterable[object], *, allow_network: bool = True
) -> dict[str, str]:
    return _DEFAULT_RESOLVER.resolve_many(codes, allow_network=allow_network)


def prime_stock_names(
    names: Mapping[str, str], *, persist: bool = False
) -> None:
    _DEFAULT_RESOLVER.prime(names, persist=persist)


def flush_stock_name_cache() -> bool:
    return _DEFAULT_RESOLVER.flush()


atexit.register(flush_stock_name_cache)
