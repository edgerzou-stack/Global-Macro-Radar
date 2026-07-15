"""Offline point-in-time backtest CLI.

The command refuses to derive historical inputs from today's universe or live
APIs. A caller must provide an explicit, versioned JSON dataset.
"""

import argparse
import json
import logging
import os
import tempfile
from pathlib import Path

import pandas as pd

from core.backtest import BacktestDataError, PointInTimeBacktest
import db_utils


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


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
    if not payload.get("dataset_version"):
        raise BacktestDataError("backtest dataset requires dataset_version")
    if not payload.get("as_of"):
        raise BacktestDataError("backtest dataset requires an as_of timestamp")
    if not isinstance(payload.get("prices"), list):
        raise BacktestDataError("backtest dataset requires a prices list")
    if not isinstance(payload.get("signals"), list):
        raise BacktestDataError("backtest dataset requires a signals list")
    return payload


def run_dataset(payload):
    config = payload.get("config") or {}
    engine = PointInTimeBacktest(
        pd.DataFrame(payload["prices"]),
        initial_cash=config.get("initial_cash", 1_000_000.0),
        commission_rate=config.get("commission_rate", 0.0),
        slippage_bps=config.get("slippage_bps", 0.0),
    )
    return engine.run(payload["signals"])


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
        },
    )
    logger.info("Backtest completed: %s", result.manifest_hash)
    return result


if __name__ == "__main__":
    main()
