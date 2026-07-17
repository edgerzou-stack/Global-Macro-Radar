"""Offline point-in-time backtest CLI.

The command refuses to derive historical inputs from today's universe or live
APIs. A caller must provide an explicit, versioned JSON dataset.
"""

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

from core.backtest import BacktestDataError, PointInTimeBacktest
import db_utils


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def prepare_backtest_database(db_path):
    """Create a fresh, explicitly marked backtest database."""
    path = db_utils.normalize_db_path(db_path)
    if path == db_utils.get_production_db_path():
        raise ValueError("Refusing to use the production database for a backtest")
    if os.path.exists(path):
        raise FileExistsError(
            f"Backtest database already exists; choose a fresh path: {path}"
        )
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = db_utils.init_db(path, environment="backtest")
    connection.close()
    return path


def load_point_in_time_dataset(dataset_path):
    path = Path(dataset_path).expanduser().resolve()
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise BacktestDataError("backtest dataset must be a JSON object")
    if payload.get("schema_version") != 1:
        raise BacktestDataError("backtest dataset requires schema_version 1")
    if not payload.get("dataset_version"):
        raise BacktestDataError("backtest dataset requires dataset_version")
    try:
        as_of = dt.datetime.fromisoformat(payload["as_of"])
    except (KeyError, TypeError, ValueError) as error:
        raise BacktestDataError("backtest dataset requires an ISO as_of timestamp") from error
    if as_of.tzinfo is None:
        raise BacktestDataError("backtest dataset as_of must be timezone-aware")
    if not isinstance(payload.get("prices"), list):
        raise BacktestDataError("backtest dataset requires a prices list")
    if not isinstance(payload.get("signals"), list):
        raise BacktestDataError("backtest dataset requires a signals list")
    calendar = payload.get("calendar")
    if not isinstance(calendar, list) or not calendar:
        raise BacktestDataError("backtest dataset requires a non-empty calendar")
    if calendar != sorted(set(calendar)):
        raise BacktestDataError("calendar must be sorted and unique")
    source_hashes = payload.get("source_hashes")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise BacktestDataError("backtest dataset requires source_hashes")
    if any(
        not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value)
        for value in source_hashes.values()
    ):
        raise BacktestDataError("source_hashes must contain lowercase SHA-256 values")

    def parse_available(record, label):
        try:
            value = dt.datetime.fromisoformat(record["available_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise BacktestDataError(f"{label} requires timezone-aware available_at") from error
        if value.tzinfo is None or value > as_of:
            raise BacktestDataError(f"{label} has invalid or future available_at")
        return value

    sessions = set(calendar)
    for row in payload["prices"]:
        if not isinstance(row, dict) or row.get("date") not in sessions:
            raise BacktestDataError("every price row must belong to the declared calendar")
        parse_available(row, "price row")
        if not row.get("currency"):
            raise BacktestDataError("price row requires currency")
        if "fx_to_base" not in row:
            raise BacktestDataError("price row requires point-in-time fx_to_base")

    memberships = payload.get("universe_membership")
    if not isinstance(memberships, list):
        raise BacktestDataError("backtest dataset requires universe_membership")
    normalized_memberships = []
    for row in memberships:
        available = parse_available(row, "universe membership")
        try:
            start = dt.date.fromisoformat(row["start_date"])
            end = dt.date.fromisoformat(row["end_date"]) if row.get("end_date") else None
        except (KeyError, TypeError, ValueError) as error:
            raise BacktestDataError("invalid universe membership dates") from error
        if end is not None and end < start:
            raise BacktestDataError("universe membership ends before it starts")
        normalized_memberships.append((row.get("symbol"), start, end, available))

    def evidence_index(label):
        raw_rows = payload.get(label, [])
        if not isinstance(raw_rows, list):
            raise BacktestDataError(f"{label} must be a list")
        indexed = {}
        for row in raw_rows:
            identifier = row.get("id") if isinstance(row, dict) else None
            if not isinstance(identifier, str) or not identifier or identifier in indexed:
                raise BacktestDataError(f"{label} IDs must be unique non-empty strings")
            parse_available(row, label)
            indexed[identifier] = row
        return indexed

    fundamentals = evidence_index("fundamentals")
    news = evidence_index("news")

    for signal in payload["signals"]:
        available = parse_available(signal, "signal")
        try:
            signal_date = dt.date.fromisoformat(signal["date"])
        except (KeyError, TypeError, ValueError) as error:
            raise BacktestDataError("signal requires an ISO date") from error
        if available.date() > signal_date:
            raise BacktestDataError("signal is dated before it became available")
        for symbol, weight in (signal.get("weights") or {}).items():
            if float(weight) <= 0:
                continue
            members = [
                item for item in normalized_memberships
                if item[0] == symbol
                and item[1] <= signal_date
                and (item[2] is None or signal_date <= item[2])
                and item[3] <= available
            ]
            if not members:
                raise BacktestDataError(
                    f"signal symbol was not an available universe member: {symbol}"
                )
        for evidence_id in signal.get("fundamental_ids", []):
            evidence = fundamentals.get(evidence_id)
            if evidence is None or parse_available(evidence, "fundamental") > available:
                raise BacktestDataError("signal references unavailable fundamental data")
        for evidence_id in signal.get("news_ids", []):
            evidence = news.get(evidence_id)
            if evidence is None or parse_available(evidence, "news") > available:
                raise BacktestDataError("signal references unavailable news")

    actions = payload.get("corporate_actions")
    if not isinstance(actions, list):
        raise BacktestDataError("backtest dataset requires corporate_actions")
    for action in actions:
        available = parse_available(action, "corporate action")
        try:
            effective_date = dt.date.fromisoformat(action["date"])
        except (KeyError, TypeError, ValueError) as error:
            raise BacktestDataError("corporate action requires an ISO date") from error
        if available.date() > effective_date:
            raise BacktestDataError(
                "corporate action became available after its effective date"
            )
    return payload


def run_dataset(payload):
    config = payload.get("config") or {}
    dataset_identity = {
        "schema_version": payload["schema_version"],
        "dataset_version": payload["dataset_version"],
        "as_of": payload["as_of"],
        "calendar": payload["calendar"],
        "source_hashes": payload["source_hashes"],
        "universe_membership": payload["universe_membership"],
        "fundamentals": payload.get("fundamentals", []),
        "news": payload.get("news", []),
    }
    engine = PointInTimeBacktest(
        pd.DataFrame(payload["prices"]),
        initial_cash=config.get("initial_cash", 1_000_000.0),
        commission_rate=config.get("commission_rate", 0.0),
        slippage_bps=config.get("slippage_bps", 0.0),
        corporate_actions=payload["corporate_actions"],
        manifest_context=dataset_identity,
    )
    return engine.run(payload["signals"])


def persist_audit_database(database_path, payload, result):
    """Persist the actual run/fill/NAV evidence in one isolated transaction."""
    run_id = result.manifest_hash
    with sqlite3.connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS backtest_runs (
                run_id TEXT PRIMARY KEY, dataset_version TEXT NOT NULL,
                dataset_as_of TEXT NOT NULL, manifest_hash TEXT NOT NULL,
                status TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS backtest_fills (
                run_id TEXT NOT NULL, sequence INTEGER NOT NULL, date TEXT NOT NULL,
                signal_date TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                quantity REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
                PRIMARY KEY (run_id, sequence)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS backtest_nav (
                run_id TEXT NOT NULL, date TEXT NOT NULL, cash REAL NOT NULL,
                holdings_value REAL NOT NULL, nav REAL NOT NULL, positions INTEGER NOT NULL,
                PRIMARY KEY (run_id, date)
            )"""
        )
        connection.execute(
            "INSERT INTO backtest_runs VALUES (?, ?, ?, ?, ?)",
            (run_id, payload["dataset_version"], payload["as_of"], result.manifest_hash, "completed"),
        )
        connection.executemany(
            "INSERT INTO backtest_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, sequence, row.date, row.signal_date, row.symbol, row.side,
                 float(row.quantity), float(row.price), float(row.fee))
                for sequence, row in enumerate(result.fills.itertuples(index=False))
            ],
        )
        connection.executemany(
            "INSERT INTO backtest_nav VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, row.date, float(row.cash), float(row.holdings_value),
                 float(row.nav), int(row.positions))
                for row in result.nav.itertuples(index=False)
            ],
        )
    return run_id


def _write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Point-in-time Global Macro backtest")
    parser.add_argument(
        "--dataset",
        required=True,
        help="Versioned JSON containing historical prices and dated target weights",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="New or existing directory for deterministic result artifacts",
    )
    parser.add_argument(
        "--db-path",
        help="Fresh audit DB path; defaults to an isolated temporary directory",
    )
    args = parser.parse_args(argv)

    database_path = args.db_path
    if database_path is None:
        database_path = os.path.join(
            tempfile.mkdtemp(prefix="global_macro_backtest_"), "backtest.db"
        )
    database_path = prepare_backtest_database(database_path)
    os.environ["SQLITE_DB_PATH"] = database_path
    os.environ["QUANT_DB_ENV"] = "backtest"

    payload = load_point_in_time_dataset(args.dataset)
    result = run_dataset(payload)
    persist_audit_database(database_path, payload, result)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result.nav.to_csv(output_dir / "nav.csv", index=False)
    result.fills.to_csv(output_dir / "fills.csv", index=False)
    _write_json_atomic(
        output_dir / "manifest.json",
        {
            "status": "VALID_POINT_IN_TIME",
            "dataset_version": payload["dataset_version"],
            "dataset_as_of": payload["as_of"],
            "manifest_hash": result.manifest_hash,
            "pending_signals": result.pending_signals,
            "audit_database": database_path,
            "input_sha256": hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        },
    )
    logger.info("Backtest completed: %s", result.manifest_hash)
    return result


if __name__ == "__main__":
    main()
