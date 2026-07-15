"""Safe, repeatable shadow acceptance runner.

The runner never gives a stage the production database path. Each iteration
starts from a transactionally consistent SQLite online backup and runs with
network and real-order execution disabled by default.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote

from core.quarantine import quarantined_row_ids


StageCallable = Callable[["ShadowContext"], Optional[Mapping[str, Any]]]

DEFAULT_LIVE_RSS_FEEDS = (
    "https://openai.com/news/rss.xml",
    "https://www.technologyreview.com/feed/",
)
DEFAULT_LIVE_SYMBOLS = ("600519", "AAPL")


class NetworkAccessDisabled(RuntimeError):
    """Raised when an offline shadow stage attempts a network connection."""


class RequestBudgetExceeded(RuntimeError):
    """Raised before a live probe can exceed its configured request budget."""


class LiveProbeDeadlineExceeded(BaseException):
    """Hard deadline signal that broad provider Exception handlers cannot swallow."""


@contextlib.contextmanager
def network_policy(allow_live_api: bool):
    """Block process-level socket access unless live use was explicitly enabled."""
    if allow_live_api:
        yield
        return

    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def blocked(*_args, **_kwargs):
        raise NetworkAccessDisabled(
            "Live API access is disabled in shadow mode; pass --allow-live-api "
            "only for an explicitly approved live probe"
        )

    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    try:
        yield
    finally:
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex


@contextlib.contextmanager
def live_probe_environment():
    """Disable LLMs and real execution without reading or logging secret values."""
    flags = {
        "LIVE_API_PROBE": "1",
        "DISABLE_LLM": "1",
        "DISABLE_REAL_ORDERS": "1",
        "EXECUTION_MODE": "shadow_probe",
    }
    previous_flags = {name: os.environ.get(name) for name in flags}
    os.environ.update(flags)
    try:
        yield
    finally:
        for name, value in previous_flags.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def read_only_http_policy():
    """Reject mutating HTTP verbs inside the narrowly scoped live stages."""
    try:
        import requests
    except ImportError:
        yield
        return

    original_request = requests.sessions.Session.request

    def guarded_request(session, method, url, *args, **kwargs):
        normalized = str(method).upper()
        if normalized not in {"GET", "HEAD"}:
            raise NetworkAccessDisabled(
                f"HTTP {normalized} is forbidden in read-only live probes"
            )
        return original_request(session, normalized, url, *args, **kwargs)

    requests.sessions.Session.request = guarded_request
    try:
        yield
    finally:
        requests.sessions.Session.request = original_request


@contextlib.contextmanager
def production_database_write_guard(production_db: Path):
    """Refuse non-read-only SQLite opens of the production database."""
    production = Path(production_db).resolve()
    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        text = str(database)
        is_read_only_uri = bool(kwargs.get("uri")) and "mode=ro" in text
        if not text.startswith("file:"):
            try:
                target = Path(text).expanduser().resolve()
            except (OSError, ValueError):
                target = None
            if target == production and not is_read_only_uri:
                raise sqlite3.OperationalError(
                    "Production database writes are forbidden in live shadow probes"
                )
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = guarded_connect
    try:
        yield
    finally:
        sqlite3.connect = original_connect


@contextlib.contextmanager
def live_probe_deadline(seconds: float):
    """Enforce a hard wall-clock deadline on POSIX main-thread probe stages."""
    if seconds <= 0:
        raise LiveProbeDeadlineExceeded("Live probe iteration deadline exceeded")
    started = time.monotonic()
    can_signal = (
        hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not can_signal:
        yield
        if time.monotonic() - started > seconds:
            raise LiveProbeDeadlineExceeded("Live probe iteration deadline exceeded")
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def deadline_handler(_signum, _frame):
        raise LiveProbeDeadlineExceeded("Live probe iteration deadline exceeded")

    signal.signal(signal.SIGALRM, deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


@contextlib.contextmanager
def shadow_environment(database_path: Path):
    """Expose only the isolated database and explicit no-order flags to stages."""
    values = {
        "SQLITE_DB_PATH": str(database_path),
        "QUANT_DB_ENV": "test",
        "SHADOW_MODE": "1",
        "EXECUTION_MODE": "shadow",
        "DISABLE_REAL_ORDERS": "1",
        "QUANT_RUN_BACKUP_COMPLETED": "1",
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


@dataclasses.dataclass(frozen=True)
class ShadowConfig:
    production_db: Path
    output_dir: Path
    market_cache_db: Optional[Path] = None
    iterations: int = 20
    allow_live_api: bool = False
    freshness_days: int = 5
    live_rss_feeds: tuple[str, ...] = DEFAULT_LIVE_RSS_FEEDS
    live_symbols: tuple[str, ...] = DEFAULT_LIVE_SYMBOLS
    live_request_limit: int = 8
    live_timeout_seconds: float = 45.0
    live_lookback_days: int = 10

    def __post_init__(self):
        object.__setattr__(self, "production_db", Path(self.production_db).expanduser().resolve())
        object.__setattr__(self, "output_dir", Path(self.output_dir).expanduser().resolve())
        if self.market_cache_db is not None:
            object.__setattr__(
                self,
                "market_cache_db",
                Path(self.market_cache_db).expanduser().resolve(),
            )
        if not 1 <= self.iterations <= 100:
            raise ValueError("iterations must be between 1 and 100")
        if self.freshness_days < 0:
            raise ValueError("freshness_days must be non-negative")
        object.__setattr__(self, "live_rss_feeds", tuple(self.live_rss_feeds))
        object.__setattr__(self, "live_symbols", tuple(self.live_symbols))
        if len(self.live_rss_feeds) > 10:
            raise ValueError("live_rss_feeds must contain at most 10 feeds")
        if len(self.live_symbols) > 10:
            raise ValueError("live_symbols must contain at most 10 symbols")
        if any(not str(url).startswith("https://") for url in self.live_rss_feeds):
            raise ValueError("live_rss_feeds must use HTTPS URLs")
        symbol_pattern = re.compile(r"^[A-Za-z0-9._-]{1,20}$")
        if any(not symbol_pattern.fullmatch(str(symbol)) for symbol in self.live_symbols):
            raise ValueError("live_symbols contains an invalid symbol")
        if not 1 <= self.live_request_limit <= 50:
            raise ValueError("live_request_limit must be between 1 and 50")
        if not 1 <= self.live_timeout_seconds <= 300:
            raise ValueError("live_timeout_seconds must be between 1 and 300")
        if not 1 <= self.live_lookback_days <= 30:
            raise ValueError("live_lookback_days must be between 1 and 30")


@dataclasses.dataclass
class IterationMetrics:
    request_count: int = 0
    external_requests: int = 0
    cache_lookups: int = 0
    cache_hits: int = 0
    sources: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)

    def snapshot(self) -> dict[str, int]:
        return {
            "request_count": self.request_count,
            "external_requests": self.external_requests,
            "cache_lookups": self.cache_lookups,
            "cache_hits": self.cache_hits,
        }

    def delta(self, before: Mapping[str, int]) -> dict[str, int]:
        current = self.snapshot()
        return {key: current[key] - before[key] for key in current}


@dataclasses.dataclass
class ShadowContext:
    iteration: int
    database_path: Path
    market_cache_path: Optional[Path]
    metrics: IterationMetrics
    observed_at: dt.datetime
    freshness_days: int
    max_external_requests: Optional[int] = None
    live_deadline: Optional[float] = None
    live_rss_feeds: tuple[str, ...] = ()
    live_symbols: tuple[str, ...] = ()
    live_lookback_days: int = 10

    def consume_external_request(self, source: str) -> None:
        if (
            self.max_external_requests is not None
            and self.metrics.external_requests >= self.max_external_requests
        ):
            raise RequestBudgetExceeded(
                f"Live request limit {self.max_external_requests} exhausted "
                f"before source {source}"
            )
        self.metrics.request_count += 1
        self.metrics.external_requests += 1

    def remaining_live_seconds(self) -> float:
        if self.live_deadline is None:
            return float("inf")
        remaining = self.live_deadline - time.monotonic()
        if remaining <= 0:
            raise LiveProbeDeadlineExceeded("Live probe iteration deadline exceeded")
        return remaining

    def record_request(
        self,
        source: str,
        *,
        cache_hit: bool,
        source_date: Optional[str] = None,
        external: bool = True,
    ) -> None:
        if external:
            self.consume_external_request(source)
        else:
            self.metrics.request_count += 1
        self.metrics.cache_lookups += 1
        if cache_hit:
            self.metrics.cache_hits += 1
        self.record_source_freshness(source, source_date)

    def record_cache_lookup(
        self, source: str, *, cache_hit: bool, source_date: Optional[str] = None
    ) -> None:
        self.metrics.request_count += 1
        self.metrics.cache_lookups += 1
        if cache_hit:
            self.metrics.cache_hits += 1
        self.record_source_freshness(source, source_date)

    def record_source_freshness(
        self,
        source: str,
        source_date: Optional[str],
        *,
        status: Optional[str] = None,
    ) -> None:
        normalized = _parse_source_date(source_date)
        if normalized is None:
            entry = {
                "latest_date": None,
                "age_days": None,
                "status": status or "unknown",
            }
        else:
            age_days = (self.observed_at.date() - normalized).days
            if status is None:
                if age_days < 0:
                    status = "future"
                elif age_days <= self.freshness_days:
                    status = "fresh"
                else:
                    status = "stale"
            entry = {
                "latest_date": normalized.isoformat(),
                "age_days": age_days,
                "status": status,
            }
        self.metrics.sources[source] = entry

    def record_live_probe(
        self,
        source: str,
        *,
        source_date: Optional[str],
        success: bool,
        latency_ms: float,
        probe_status: str,
        error: Optional[str] = None,
    ) -> None:
        self.record_source_freshness(source, source_date)
        self.metrics.sources[source].update(
            {
                "success": bool(success),
                "latency_ms": round(max(0.0, float(latency_ms)), 3),
                "probe_status": str(probe_status),
                "error": str(error) if error else None,
            }
        )


def _parse_source_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    text = str(value).strip().replace("-", "")[:8]
    try:
        return dt.datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _sqlite_read_only_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        _sqlite_read_only_uri(path), uri=True, timeout=30.0
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def online_backup(source_path: Path, destination_path: Path) -> dict[str, Any]:
    """Create and verify a consistent backup while opening source read-only."""
    source = Path(source_path).expanduser().resolve()
    destination = Path(destination_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    if source == destination:
        raise ValueError("Shadow backup destination must differ from its source")
    if destination.exists():
        raise FileExistsError(f"Shadow backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with read_only_connection(source) as source_connection:
        with sqlite3.connect(destination, timeout=30.0) as destination_connection:
            source_connection.backup(destination_connection)
            # A backup of a WAL-mode source retains WAL journal mode in the
            # database header. Convert only the isolated destination so it can
            # subsequently be opened query-only without creating sidecars.
            destination_connection.execute("PRAGMA journal_mode=DELETE")

    with read_only_connection(destination) as verification:
        integrity = verification.execute("PRAGMA integrity_check").fetchone()
        page_count = verification.execute("PRAGMA page_count").fetchone()[0]
        user_version = verification.execute("PRAGMA user_version").fetchone()[0]
    if not integrity or integrity[0] != "ok":
        raise sqlite3.DatabaseError(f"Shadow backup integrity failed: {integrity!r}")
    return {
        "integrity": integrity[0],
        "page_count": page_count,
        "user_version": user_version,
        "source_opened_read_only": True,
    }


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def database_integrity_stage(context: ShadowContext) -> Mapping[str, Any]:
    with read_only_connection(context.database_path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = sorted(_table_names(connection))
    status = "ok" if integrity == "ok" and not foreign_key_errors else "failed"
    return {
        "status": status,
        "integrity": integrity,
        "foreign_key_errors": len(foreign_key_errors),
        "user_version": user_version,
        "table_count": len(tables),
    }


def ledger_invariants_stage(context: ShadowContext) -> Mapping[str, Any]:
    issues: dict[str, int] = {}
    observations: dict[str, int] = {}
    with read_only_connection(context.database_path) as connection:
        tables = _table_names(connection)
        if "strategy_accounts" in tables:
            issues["negative_cash"] = connection.execute(
                "SELECT COUNT(*) FROM strategy_accounts WHERE available_cash < 0"
            ).fetchone()[0]
            issues["non_positive_capital"] = connection.execute(
                "SELECT COUNT(*) FROM strategy_accounts WHERE total_capital <= 0"
            ).fetchone()[0]
        if "portfolio" in tables:
            quarantined_portfolio_ids = quarantined_row_ids(connection, "portfolio")
            observations["quarantined_portfolio_rows"] = len(
                quarantined_portfolio_ids
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(portfolio)")
            }
            if "entry_price" in columns:
                pending_ids = {
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM portfolio "
                        "WHERE entry_price IS NULL OR entry_price <= 0"
                    ).fetchall()
                }
                observations["pending_or_unknown_entry_price"] = len(pending_ids)
                observations["active_pending_or_unknown_entry_price"] = len(
                    pending_ids - quarantined_portfolio_ids
                )
            if "shares" in columns:
                invalid_quantity_ids = {
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM portfolio WHERE shares IS NULL OR shares <= 0"
                    ).fetchall()
                }
                observations["raw_invalid_position_quantity"] = len(
                    invalid_quantity_ids
                )
                issues["invalid_position_quantity"] = len(
                    invalid_quantity_ids - quarantined_portfolio_ids
                )
        if {"journal_transactions", "journal_entries"}.issubset(tables):
            issues["unbalanced_journal_currency_groups"] = len(
                connection.execute(
                    """
                    SELECT jt.transaction_id, je.currency
                    FROM journal_transactions jt
                    LEFT JOIN journal_entries je
                        ON je.transaction_id = jt.transaction_id
                    GROUP BY jt.transaction_id, je.currency
                    HAVING COUNT(je.entry_id) = 0
                        OR SUM(je.debit_minor) != SUM(je.credit_minor)
                    """
                ).fetchall()
            )
    issue_count = sum(issues.values())
    return {
        "status": "ok" if issue_count == 0 else "failed",
        "issue_count": issue_count,
        "issues": issues,
        "observations": observations,
    }


def execution_snapshot_stage(context: ShadowContext) -> Mapping[str, Any]:
    """Read order/fill state only; this stage has no execution adapter."""
    with read_only_connection(context.database_path) as connection:
        tables = _table_names(connection)
        order_count = 0
        fill_count = 0
        states: dict[str, int] = {}
        if "orders" in tables:
            order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            states = {
                state: count
                for state, count in connection.execute(
                    "SELECT state, COUNT(*) FROM orders GROUP BY state"
                ).fetchall()
            }
        if "fills" in tables:
            fill_count = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    return {
        "status": "ok",
        "orders_observed": order_count,
        "fills_observed": fill_count,
        "order_states": states,
        "execution_adapter": "disabled",
    }


def market_cache_health_stage(context: ShadowContext) -> Mapping[str, Any]:
    source = "market_cache"
    context.metrics.request_count += 1
    context.metrics.cache_lookups += 1
    if context.market_cache_path is None or not context.market_cache_path.is_file():
        context.record_source_freshness(source, None, status="missing")
        return {
            "status": "degraded",
            "cache_hit": False,
            "rows": 0,
            "symbols": 0,
            "latest_date": None,
        }

    with read_only_connection(context.market_cache_path) as connection:
        tables = _table_names(connection)
        if "daily_prices" not in tables:
            context.record_source_freshness(source, None, status="invalid_schema")
            return {
                "status": "degraded",
                "cache_hit": False,
                "rows": 0,
                "symbols": 0,
                "latest_date": None,
            }
        rows, symbols, latest_date = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM daily_prices"
        ).fetchone()

    cache_hit = rows > 0
    if cache_hit:
        context.metrics.cache_hits += 1
    context.record_source_freshness(source, latest_date)
    freshness = context.metrics.sources[source]
    return {
        "status": "ok" if cache_hit and freshness["status"] == "fresh" else "degraded",
        "cache_hit": cache_hit,
        "rows": rows,
        "symbols": symbols,
        "latest_date": freshness["latest_date"],
        "age_days": freshness["age_days"],
        "freshness_status": freshness["status"],
    }


def _load_production_ingest():
    radar_root = Path(__file__).resolve().parents[2] / "industry-radar"
    ingest_path = radar_root / "ingest.py"
    spec = importlib.util.spec_from_file_location("shadow_production_ingest", ingest_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load production RSS ingest contract: {ingest_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_data_gateway(cache_path: Path):
    from core.data_gateway import DataGateway

    return DataGateway(db_path=str(cache_path))


def rss_live_health_stage(context: ShadowContext) -> Mapping[str, Any]:
    feeds = context.live_rss_feeds
    if not feeds:
        return {"status": "ok", "configured_feeds": 0, "sources": []}

    for feed in feeds:
        context.consume_external_request(f"rss:{feed}")
    remaining = context.remaining_live_seconds()
    ingest = _load_production_ingest()
    _articles, health_results = ingest.fetch_rss_feeds(
        list(feeds),
        hours_back=context.freshness_days * 24,
        now=context.observed_at,
        return_health=True,
        request_timeout=max(1.0, min(15.0, remaining)),
        max_workers=min(5, len(feeds)),
    )

    source_results = []
    successes = 0
    for health in health_results:
        feed = str(health.get("url", "unknown"))
        status = str(health.get("status", "failed"))
        success = status != "failed"
        successes += int(success)
        latency_ms = float(health.get("latency_ms") or 0.0)
        context.record_live_probe(
            f"rss:{feed}",
            source_date=health.get("newest_published_at"),
            success=success,
            latency_ms=latency_ms,
            probe_status=status,
            error=health.get("error"),
        )
        source_results.append(
            {
                "url": feed,
                "status": status,
                "success": success,
                "latency_ms": round(latency_ms, 3),
                "fresh_entries": int(health.get("fresh_entries") or 0),
                "total_entries": int(health.get("total_entries") or 0),
                "quarantined_entries": int(
                    health.get("quarantined_entries") or 0
                ),
                "newest_published_at": health.get("newest_published_at"),
            }
        )

    if successes == 0:
        stage_status = "failed"
    elif any(item["status"] != "healthy" for item in source_results):
        stage_status = "degraded"
    else:
        stage_status = "ok"
    return {
        "status": stage_status,
        "configured_feeds": len(feeds),
        "successful_feeds": successes,
        "sources": source_results,
        "contract": "industry-radar/ingest.py",
    }


def _call_gateway_source_once(gateway, method_name: str, *args):
    """Call one DataGateway adapter attempt, bypassing its retry decorator."""
    bound_method = getattr(gateway, method_name)
    unwrapped = getattr(bound_method, "__wrapped__", None)
    if unwrapped is not None:
        return unwrapped(gateway, *args)
    return bound_method(*args)


def _market_observation(frame) -> tuple[str, float]:
    if frame is None or getattr(frame, "empty", True):
        raise ValueError("Market source returned no rows")
    if "日期" not in frame.columns or "收盘" not in frame.columns:
        raise ValueError("Market source omitted 日期/收盘")
    valid = frame.copy()
    valid["收盘"] = valid["收盘"].map(float)
    valid = valid[valid["收盘"].map(lambda value: math.isfinite(value) and value > 0)]
    if valid.empty:
        raise ValueError("Market source returned no finite positive close")
    valid["日期"] = valid["日期"].astype(str).str.replace("-", "", regex=False).str[:8]
    valid = valid[valid["日期"].str.fullmatch(r"\d{8}")]
    if valid.empty:
        raise ValueError("Market source returned no valid date")
    latest = valid.sort_values("日期").iloc[-1]
    return str(latest["日期"]), float(latest["收盘"])


def market_live_health_stage(context: ShadowContext) -> Mapping[str, Any]:
    symbols = context.live_symbols
    if not symbols:
        return {"status": "ok", "configured_symbols": 0, "sources": []}

    cache_path = context.database_path.parent / "live_probe_market_cache.db"
    gateway = _create_data_gateway(cache_path)
    end_date = context.observed_at.date().strftime("%Y%m%d")
    start_date = (
        context.observed_at.date() - dt.timedelta(days=context.live_lookback_days)
    ).strftime("%Y%m%d")
    source_results = []
    observations: dict[str, dict[str, tuple[str, float]]] = {}

    for symbol in symbols:
        symbol = str(symbol)
        is_a_share = len(symbol) == 6 and symbol.isdigit()
        adapters = (
            (("baostock", "_fetch_from_baostock"), ("sina", "_fetch_from_sina"))
            if is_a_share
            else (("yfinance", "_fetch_from_yfinance"),)
        )
        for provider, method_name in adapters:
            source = f"market:{provider}:{symbol}"
            context.consume_external_request(source)
            context.remaining_live_seconds()
            started = time.perf_counter()
            try:
                frame = _call_gateway_source_once(
                    gateway, method_name, symbol, start_date, end_date, ""
                )
                source_date, close = _market_observation(frame)
                latency_ms = _elapsed_ms(started)
                context.record_live_probe(
                    source,
                    source_date=source_date,
                    success=True,
                    latency_ms=latency_ms,
                    probe_status="healthy",
                )
                observations.setdefault(symbol, {})[provider] = (source_date, close)
                source_results.append(
                    {
                        "source": provider,
                        "symbol": symbol,
                        "status": "healthy",
                        "success": True,
                        "latest_date": _parse_source_date(source_date).isoformat(),
                        "latency_ms": latency_ms,
                    }
                )
            except (RequestBudgetExceeded, LiveProbeDeadlineExceeded):
                raise
            except Exception as error:
                latency_ms = _elapsed_ms(started)
                context.record_live_probe(
                    source,
                    source_date=None,
                    success=False,
                    latency_ms=latency_ms,
                    probe_status="failed",
                    error=f"{type(error).__name__}: {error}",
                )
                source_results.append(
                    {
                        "source": provider,
                        "symbol": symbol,
                        "status": "failed",
                        "success": False,
                        "latest_date": None,
                        "latency_ms": latency_ms,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )

    cross_checks = []
    for symbol, provider_values in observations.items():
        if {"baostock", "sina"}.issubset(provider_values):
            bs_date, bs_close = provider_values["baostock"]
            sina_date, sina_close = provider_values["sina"]
            relative_gap = abs(bs_close - sina_close) / max(bs_close, sina_close)
            passed = bs_date == sina_date and relative_gap <= 0.01
            cross_checks.append(
                {
                    "symbol": symbol,
                    "passed": passed,
                    "date_match": bs_date == sina_date,
                    "relative_close_gap": round(relative_gap, 6),
                }
            )

    successful = sum(item["success"] for item in source_results)
    if not source_results or successful == 0:
        stage_status = "failed"
    elif successful != len(source_results) or any(
        not check["passed"] for check in cross_checks
    ):
        stage_status = "degraded"
    else:
        stage_status = "ok"
    return {
        "status": stage_status,
        "configured_symbols": len(symbols),
        "successful_sources": successful,
        "sources": source_results,
        "cross_checks": cross_checks,
        "contract": "core.data_gateway.DataGateway",
        "cache_path": "isolated_iteration_cache",
    }


DEFAULT_STAGES: tuple[tuple[str, StageCallable], ...] = (
    ("database_integrity", database_integrity_stage),
    ("ledger_invariants", ledger_invariants_stage),
    ("execution_snapshot", execution_snapshot_stage),
    ("market_cache_health", market_cache_health_stage),
)

LIVE_API_STAGES: tuple[tuple[str, StageCallable], ...] = (
    ("rss_live_health", rss_live_health_stage),
    ("market_live_health", market_live_health_stage),
)


class ShadowRunner:
    def __init__(
        self,
        config: ShadowConfig,
        *,
        extra_stages: Optional[Iterable[tuple[str, StageCallable]]] = None,
        now: Optional[Callable[[], dt.datetime]] = None,
    ):
        self.config = config
        self.extra_stages = tuple(extra_stages or ())
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        configured_stages = DEFAULT_STAGES + self.extra_stages
        if self.config.allow_live_api:
            configured_stages += LIVE_API_STAGES
        names = [name for name, _stage in configured_stages]
        if len(names) != len(set(names)):
            raise ValueError("Shadow stage names must be unique")

    def run(self) -> dict[str, Any]:
        if not self.config.production_db.is_file():
            raise FileNotFoundError(
                f"Production database does not exist: {self.config.production_db}"
            )
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        started_at = _ensure_utc(self.now())
        production_hash_before = _file_sha256(self.config.production_db)
        iterations: list[dict[str, Any]] = []

        for iteration_number in range(1, self.config.iterations + 1):
            iterations.append(self._run_iteration(iteration_number))

        production_hash_after = _file_sha256(self.config.production_db)
        production_unchanged = production_hash_before == production_hash_after
        finished_at = _ensure_utc(self.now())
        summary = _summarize_iterations(iterations)
        summary["duration_ms"] = _duration_ms(started_at, finished_at)
        summary["acceptance_passed"] = (
            summary["iterations_succeeded"] == self.config.iterations
            and summary["iterations_degraded"] == 0
            and production_unchanged
        )

        timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        json_name = f"shadow_acceptance_{timestamp}.json"
        markdown_name = f"shadow_acceptance_{timestamp}.md"
        report = {
            "schema_version": 1,
            "mode": "shadow",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "configuration": {
                "iterations": self.config.iterations,
                "freshness_days": self.config.freshness_days,
                "live_request_limit": self.config.live_request_limit,
                "live_timeout_seconds": self.config.live_timeout_seconds,
                "live_rss_feeds": list(self.config.live_rss_feeds),
                "live_symbols": list(self.config.live_symbols),
                "production_db": str(self.config.production_db),
                "market_cache_db": (
                    str(self.config.market_cache_db)
                    if self.config.market_cache_db is not None
                    else None
                ),
            },
            "safety": {
                "production_database_opened_read_only": True,
                "production_database_unchanged": production_unchanged,
                "real_orders_enabled": False,
                "live_api_enabled": self.config.allow_live_api,
                "live_api_scope": "health_probes_only",
                "http_methods_allowed": ["GET", "HEAD"],
                "llm_enabled": False,
                "isolated_database_per_iteration": True,
            },
            "summary": summary,
            "iterations": iterations,
            "artifacts": {"json": json_name, "markdown": markdown_name},
        }
        _atomic_write_json(self.config.output_dir / json_name, report)
        _atomic_write_text(
            self.config.output_dir / markdown_name, _render_markdown(report)
        )
        return report

    def _run_iteration(self, iteration_number: int) -> dict[str, Any]:
        observed_at = _ensure_utc(self.now())
        metrics = IterationMetrics()
        stage_results: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory(prefix=f"shadow-{iteration_number:02d}-") as temp:
            temp_dir = Path(temp)
            database_copy = temp_dir / "quant_system.shadow.db"
            cache_copy = temp_dir / "market_data_cache.shadow.db"

            before = metrics.snapshot()
            started = time.perf_counter()
            try:
                database_backup = online_backup(
                    self.config.production_db, database_copy
                )
                cache_backup = None
                if (
                    self.config.market_cache_db is not None
                    and self.config.market_cache_db.is_file()
                ):
                    cache_backup = online_backup(
                        self.config.market_cache_db, cache_copy
                    )
                stage_results.append(
                    {
                        "name": "snapshot",
                        "status": "ok",
                        "duration_ms": _elapsed_ms(started),
                        "metrics": metrics.delta(before),
                        "details": {
                            "database": database_backup,
                            "market_cache": cache_backup,
                        },
                    }
                )
            except Exception as error:
                stage_results.append(
                    {
                        "name": "snapshot",
                        "status": "failed",
                        "duration_ms": _elapsed_ms(started),
                        "metrics": metrics.delta(before),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                return _iteration_result(iteration_number, stage_results, metrics)

            context = ShadowContext(
                iteration=iteration_number,
                database_path=database_copy,
                market_cache_path=cache_copy if cache_copy.is_file() else None,
                metrics=metrics,
                observed_at=observed_at,
                freshness_days=self.config.freshness_days,
                max_external_requests=(
                    self.config.live_request_limit
                    if self.config.allow_live_api
                    else None
                ),
                live_deadline=(
                    time.monotonic() + self.config.live_timeout_seconds
                    if self.config.allow_live_api
                    else None
                ),
                live_rss_feeds=self.config.live_rss_feeds,
                live_symbols=self.config.live_symbols,
                live_lookback_days=self.config.live_lookback_days,
            )
            with shadow_environment(database_copy), network_policy(False):
                for name, stage in DEFAULT_STAGES + self.extra_stages:
                    stage_results.append(self._execute_stage(name, stage, context))

            if self.config.allow_live_api:
                with shadow_environment(database_copy), \
                    live_probe_environment(), \
                    production_database_write_guard(self.config.production_db), \
                    read_only_http_policy(), \
                    network_policy(True):
                    for name, stage in LIVE_API_STAGES:
                        try:
                            with live_probe_deadline(context.remaining_live_seconds()):
                                result = self._execute_stage(name, stage, context)
                        except LiveProbeDeadlineExceeded as error:
                            result = {
                                "name": name,
                                "status": "failed",
                                "duration_ms": 0.0,
                                "metrics": metrics.delta(metrics.snapshot()),
                                "error": f"LiveProbeDeadlineExceeded: {error}",
                            }
                        stage_results.append(result)

        return _iteration_result(iteration_number, stage_results, metrics)

    @staticmethod
    def _execute_stage(
        name: str, stage: StageCallable, context: ShadowContext
    ) -> dict[str, Any]:
        before = context.metrics.snapshot()
        started = time.perf_counter()
        try:
            details = dict(stage(context) or {})
            status = str(details.pop("status", "ok"))
            if status not in {"ok", "degraded", "failed"}:
                raise ValueError(f"Invalid stage status from {name}: {status!r}")
            return {
                "name": name,
                "status": status,
                "duration_ms": _elapsed_ms(started),
                "metrics": context.metrics.delta(before),
                "details": details,
            }
        except LiveProbeDeadlineExceeded as error:
            return {
                "name": name,
                "status": "failed",
                "duration_ms": _elapsed_ms(started),
                "metrics": context.metrics.delta(before),
                "error": f"LiveProbeDeadlineExceeded: {error}",
            }
        except Exception as error:
            return {
                "name": name,
                "status": "failed",
                "duration_ms": _elapsed_ms(started),
                "metrics": context.metrics.delta(before),
                "error": f"{type(error).__name__}: {error}",
            }


def _iteration_result(
    iteration: int,
    stages: list[dict[str, Any]],
    metrics: IterationMetrics,
) -> dict[str, Any]:
    failed = any(stage["status"] == "failed" for stage in stages)
    degraded = any(stage["status"] == "degraded" for stage in stages)
    cache_hit_rate = (
        metrics.cache_hits / metrics.cache_lookups if metrics.cache_lookups else None
    )
    return {
        "iteration": iteration,
        "status": "failed" if failed else ("degraded" if degraded else "ok"),
        "duration_ms": round(sum(stage["duration_ms"] for stage in stages), 3),
        "metrics": {
            **metrics.snapshot(),
            "cache_hit_rate": (
                round(cache_hit_rate, 6) if cache_hit_rate is not None else None
            ),
            "source_freshness": metrics.sources,
        },
        "stages": stages,
    }


def _summarize_iterations(iterations: list[dict[str, Any]]) -> dict[str, Any]:
    external_requests = sum(
        item["metrics"]["external_requests"] for item in iterations
    )
    request_count = sum(item["metrics"]["request_count"] for item in iterations)
    cache_lookups = sum(item["metrics"]["cache_lookups"] for item in iterations)
    cache_hits = sum(item["metrics"]["cache_hits"] for item in iterations)
    source_entries: dict[str, list[dict[str, Any]]] = {}
    for item in iterations:
        for source, freshness in item["metrics"]["source_freshness"].items():
            source_entries.setdefault(source, []).append(freshness)
    live_samples = [
        entry
        for entries in source_entries.values()
        for entry in entries
        if "success" in entry
    ]
    return {
        "iterations_completed": len(iterations),
        "iterations_succeeded": sum(
            item["status"] != "failed" for item in iterations
        ),
        "iterations_degraded": sum(
            item["status"] == "degraded" for item in iterations
        ),
        "request_count": request_count,
        "external_requests": external_requests,
        "cache_lookups": cache_lookups,
        "cache_hits": cache_hits,
        "cache_hit_rate": (
            round(cache_hits / cache_lookups, 6) if cache_lookups else None
        ),
        "live_probe_success_rate": (
            round(sum(bool(entry["success"]) for entry in live_samples) / len(live_samples), 6)
            if live_samples
            else None
        ),
        "source_freshness": {
            source: _aggregate_freshness(entries)
            for source, entries in sorted(source_entries.items())
        },
    }


def _aggregate_freshness(entries: list[dict[str, Any]]) -> dict[str, Any]:
    precedence = {
        "future": 5,
        "missing": 4,
        "invalid_schema": 4,
        "stale": 3,
        "unknown": 2,
        "fresh": 1,
    }
    worst = max(entries, key=lambda entry: precedence.get(entry["status"], 6))
    ages = [entry["age_days"] for entry in entries if entry["age_days"] is not None]
    latest_dates = [
        entry["latest_date"] for entry in entries if entry["latest_date"] is not None
    ]
    latencies = [
        float(entry["latency_ms"])
        for entry in entries
        if entry.get("latency_ms") is not None
    ]
    successes = [bool(entry["success"]) for entry in entries if "success" in entry]
    return {
        "status": worst["status"],
        "latest_date": max(latest_dates) if latest_dates else None,
        "age_days": max(ages) if ages else None,
        "samples": len(entries),
        "success_rate": (
            round(sum(successes) / len(successes), 6) if successes else None
        ),
        "avg_latency_ms": (
            round(sum(latencies) / len(latencies), 3) if latencies else None
        ),
    }


def _ensure_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _duration_ms(start: dt.datetime, end: dt.datetime) -> float:
    return round(max(0.0, (end - start).total_seconds() * 1000.0), 3)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    safety = report["safety"]
    lines = [
        f"# {report['configuration']['iterations']} 次 Shadow 验收",
        "",
        f"- 开始时间：`{report['started_at']}`",
        f"- 验收通过：`{summary['acceptance_passed']}`",
        f"- 成功轮次：`{summary['iterations_succeeded']}/{summary['iterations_completed']}`",
        f"- 外部请求数：`{summary['external_requests']}`",
        f"- 缓存命中：`{summary['cache_hits']}/{summary['cache_lookups']}`",
        f"- 生产数据库保持不变：`{safety['production_database_unchanged']}`",
        f"- 真实下单启用：`{safety['real_orders_enabled']}`",
        f"- Live API 启用：`{safety['live_api_enabled']}`",
        "",
        "## 数据源新鲜度",
        "",
        "| 数据源 | 状态 | 最新日期 | 最差年龄（天） | 成功率 | 平均延迟 ms | 样本数 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for source, freshness in summary["source_freshness"].items():
        lines.append(
            f"| {source} | {freshness['status']} | "
            f"{freshness['latest_date'] or '-'} | "
            f"{freshness['age_days'] if freshness['age_days'] is not None else '-'} | "
            f"{freshness['success_rate'] if freshness['success_rate'] is not None else '-'} | "
            f"{freshness['avg_latency_ms'] if freshness['avg_latency_ms'] is not None else '-'} | "
            f"{freshness['samples']} |"
        )
    lines.extend(
        [
            "",
            "## 阶段明细",
            "",
            "| 轮次 | 阶段 | 状态 | 耗时 ms | 请求数 | 外部请求 | 缓存命中/查询 |",
            "|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for iteration in report["iterations"]:
        for stage in iteration["stages"]:
            metrics = stage["metrics"]
            lines.append(
                f"| {iteration['iteration']} | {stage['name']} | {stage['status']} | "
                f"{stage['duration_ms']:.3f} | {metrics['request_count']} | "
                f"{metrics['external_requests']} | "
                f"{metrics['cache_hits']}/{metrics['cache_lookups']} |"
            )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    scripts_dir = Path(__file__).resolve().parent
    quant_root = scripts_dir.parent
    repository_root = quant_root.parent
    parser = argparse.ArgumentParser(
        description="Run isolated shadow acceptance checks (20 iterations by default)."
    )
    parser.add_argument(
        "--production-db",
        type=Path,
        default=quant_root / "quant_system.db",
        help="Production SQLite database opened read-only for online backup.",
    )
    parser.add_argument(
        "--market-cache-db",
        type=Path,
        default=scripts_dir / ".cache" / "market_data_cache.db",
        help="Optional market cache SQLite database used for offline freshness checks.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=repository_root / "reports" / "shadow",
    )
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--freshness-days", type=int, default=5)
    parser.add_argument(
        "--allow-live-api",
        action="store_true",
        help="Run bounded read-only RSS and market-data health probes.",
    )
    parser.add_argument(
        "--live-rss-feed",
        action="append",
        default=None,
        help="HTTPS RSS feed to probe; repeat to provide a small custom sample.",
    )
    parser.add_argument(
        "--live-symbol",
        action="append",
        default=None,
        help="Market symbol to probe; repeat to provide a small custom sample.",
    )
    parser.add_argument(
        "--skip-live-rss", action="store_true", help="Disable the RSS live probe."
    )
    parser.add_argument(
        "--skip-live-market", action="store_true", help="Disable the market live probe."
    )
    parser.add_argument("--live-request-limit", type=int, default=8)
    parser.add_argument("--live-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--live-lookback-days", type=int, default=10)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    live_rss_feeds = tuple(args.live_rss_feed or DEFAULT_LIVE_RSS_FEEDS)
    live_symbols = tuple(args.live_symbol or DEFAULT_LIVE_SYMBOLS)
    if args.skip_live_rss:
        live_rss_feeds = ()
    if args.skip_live_market:
        live_symbols = ()
    runner = ShadowRunner(
        ShadowConfig(
            production_db=args.production_db,
            market_cache_db=args.market_cache_db,
            output_dir=args.output_dir,
            iterations=args.iterations,
            allow_live_api=args.allow_live_api,
            freshness_days=args.freshness_days,
            live_rss_feeds=live_rss_feeds,
            live_symbols=live_symbols,
            live_request_limit=args.live_request_limit,
            live_timeout_seconds=args.live_timeout_seconds,
            live_lookback_days=args.live_lookback_days,
        )
    )
    report = runner.run()
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"JSON: {args.output_dir / report['artifacts']['json']}")
    print(f"Markdown: {args.output_dir / report['artifacts']['markdown']}")
    return 0 if report["summary"]["acceptance_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
