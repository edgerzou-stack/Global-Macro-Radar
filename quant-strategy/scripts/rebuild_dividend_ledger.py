#!/usr/bin/env python3
"""Rebuild the legacy A-share dividend simulation ledger in a new DB copy.

The tool is intentionally unable to modify its source database.  It copies the
source with SQLite's backup API, validates a declarative event manifest, rewrites
only ``dividend_a_stock`` legacy state in the copy, and emits a reconciliation
report.  Daily research results remain untouched as audit evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any

from migrations.v006_execution_ledger import apply_v006


getcontext().prec = 28
CENT = Decimal("0.01")
STRATEGY = "dividend_a_stock"
AUDIT_META_KEY = "dividend_ledger_rebuild_v1"


class RebuildError(RuntimeError):
    """Raised when an audit or reconstruction invariant fails."""


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Position:
    symbol: str
    entry_date: str
    entry_price: Decimal
    tranches: int
    source_event_id: str


@dataclass(frozen=True)
class ClosedTrade:
    event_id: str
    symbol: str
    entry_date: str
    entry_price: Decimal
    exit_date: str
    exit_price: Decimal
    pnl_rate: Decimal
    tranches: int
    reason: str


@dataclass
class ReplayState:
    cash: Decimal
    realized_pnl: Decimal
    positions: dict[str, Position]
    closed_trades: list[ClosedTrade]


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RebuildError("unsupported rebuild manifest schema_version")
    if payload.get("strategy_id") != STRATEGY:
        raise RebuildError("manifest is not for dividend_a_stock")
    if not payload.get("rebuild_id"):
        raise RebuildError("manifest rebuild_id is required")
    return payload


def validate_manifest(manifest: dict[str, Any]) -> None:
    event_ids: set[str] = set()
    previous_timestamp = ""
    for event in manifest["events"]:
        event_id = event["event_id"]
        if event_id in event_ids:
            raise RebuildError(f"duplicate event_id: {event_id}")
        event_ids.add(event_id)
        timestamp = event["executed_at"]
        if previous_timestamp and timestamp < previous_timestamp:
            raise RebuildError("events are not ordered by executed_at")
        previous_timestamp = timestamp
        if event["side"] not in {"BUY", "SELL"}:
            raise RebuildError(f"invalid side for {event_id}")
        if int(event["tranches"]) <= 0:
            raise RebuildError(f"invalid tranches for {event_id}")
        price = dec(event["price"])
        if price <= 0:
            raise RebuildError(f"non-positive price for {event_id}")
        for provider in ("baostock", "sina"):
            low = dec(event[f"{provider}_low"])
            high = dec(event[f"{provider}_high"])
            if low <= 0 or high < low or not low <= price <= high:
                raise RebuildError(
                    f"{event_id} price {price} is outside {provider} OHLC "
                    f"range [{low}, {high}]"
                )

    legacy_ids = [row["event_id"] for row in manifest["legacy_closed_trades"]]
    if len(legacy_ids) != len(set(legacy_ids)):
        raise RebuildError("duplicate legacy closed-trade event_id")
    if event_ids.intersection(legacy_ids):
        raise RebuildError("event_id reused across legacy and reconstructed events")


def _initial_state(manifest: dict[str, Any]) -> ReplayState:
    notional = dec(manifest["tranche_notional"])
    state = ReplayState(
        cash=dec(manifest["initial_capital"]),
        realized_pnl=Decimal("0"),
        positions={},
        closed_trades=[],
    )
    for row in manifest["legacy_closed_trades"]:
        pnl_rate = dec(row["pnl_rate"])
        tranches = int(row["tranches"])
        state.realized_pnl += notional * tranches * pnl_rate
        state.cash += notional * tranches * pnl_rate
        state.closed_trades.append(
            ClosedTrade(
                event_id=row["event_id"],
                symbol=row["symbol"],
                entry_date=row["entry_date"],
                entry_price=dec(row["entry_price"]),
                exit_date=row["exit_date"],
                exit_price=dec(row["exit_price"]),
                pnl_rate=pnl_rate,
                tranches=tranches,
                reason=row["reason"],
            )
        )
    return state


def _apply_event(
    state: ReplayState, event: dict[str, Any], notional: Decimal
) -> None:
    symbol = event["symbol"]
    event_id = event["event_id"]
    tranches = int(event["tranches"])
    price = dec(event["price"])
    execution_date = event["executed_at"][:10]
    if event["side"] == "BUY":
        if symbol in state.positions:
            raise RebuildError(f"duplicate BUY without SELL: {event_id}")
        required = notional * tranches
        if state.cash < required:
            raise RebuildError(f"negative cash would result from {event_id}")
        state.cash -= required
        state.positions[symbol] = Position(
            symbol=symbol,
            entry_date=execution_date,
            entry_price=price,
            tranches=tranches,
            source_event_id=event_id,
        )
        return

    position = state.positions.get(symbol)
    if position is None:
        raise RebuildError(f"SELL without an open position: {event_id}")
    if position.tranches != tranches:
        raise RebuildError(
            f"tranche mismatch for {event_id}: open={position.tranches}, "
            f"sell={tranches}"
        )
    fee_rate = dec(event["fee_rate"])
    pnl_rate = (price / position.entry_price) - Decimal("1") - fee_rate
    realized = notional * tranches * pnl_rate
    state.cash += notional * tranches + realized
    state.realized_pnl += realized
    state.closed_trades.append(
        ClosedTrade(
            event_id=event_id,
            symbol=symbol,
            entry_date=position.entry_date,
            entry_price=position.entry_price,
            exit_date=execution_date,
            exit_price=price,
            pnl_rate=pnl_rate,
            tranches=tranches,
            reason=event["reason"],
        )
    )
    del state.positions[symbol]


def replay_until(
    manifest: dict[str, Any], cutoff_date: str | None = None
) -> ReplayState:
    state = _initial_state(manifest)
    notional = dec(manifest["tranche_notional"])
    for event in manifest["events"]:
        if cutoff_date is not None and event["executed_at"][:10] > cutoff_date:
            break
        _apply_event(state, event, notional)
    return state


def _snapshot_row(
    manifest: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    state = replay_until(manifest, snapshot["date"])
    closes = {symbol: dec(value) for symbol, value in snapshot["closes"].items()}
    if set(closes) != set(state.positions):
        raise RebuildError(
            f"snapshot {snapshot['date']} close symbols do not match positions: "
            f"closes={sorted(closes)}, positions={sorted(state.positions)}"
        )
    notional = dec(manifest["tranche_notional"])
    holdings = sum(
        (
            notional
            * position.tranches
            * closes[symbol]
            / position.entry_price
            for symbol, position in state.positions.items()
        ),
        Decimal("0"),
    )
    return {
        "date": snapshot["date"],
        "market_date": snapshot["market_date"],
        "cash": money(state.cash),
        "holdings_value": money(holdings),
        "nav": money(state.cash + holdings),
        "positions": {
            symbol: {
                "entry_date": position.entry_date,
                "entry_price": str(position.entry_price),
                "tranches": position.tranches,
                "close": str(closes[symbol]),
            }
            for symbol, position in sorted(state.positions.items())
        },
    }


def build_reconciliation(manifest: dict[str, Any]) -> dict[str, Any]:
    validate_manifest(manifest)
    final = replay_until(manifest)
    snapshots = [_snapshot_row(manifest, row) for row in manifest["snapshots"]]
    expected = manifest["expected"]
    expected_positions = expected["open_positions"]
    actual_positions = {
        symbol: {
            "entry_date": position.entry_date,
            "entry_price": str(position.entry_price),
            "tranches": position.tranches,
        }
        for symbol, position in sorted(final.positions.items())
    }
    if actual_positions != expected_positions:
        raise RebuildError(
            f"final positions differ from manifest expectation: {actual_positions}"
        )
    if len(final.closed_trades) != int(expected["closed_trade_count"]):
        raise RebuildError("closed trade count does not match expectation")
    final_snapshot = snapshots[-1]
    if final_snapshot["date"] != manifest["as_of_date"]:
        raise RebuildError("last snapshot is not the manifest as_of_date")
    if final_snapshot["cash"] != dec(expected["available_cash"]):
        raise RebuildError(
            f"cash mismatch: {final_snapshot['cash']} != "
            f"{expected['available_cash']}"
        )
    if final_snapshot["nav"] != dec(expected["nav_as_of"]):
        raise RebuildError(
            f"NAV mismatch: {final_snapshot['nav']} != {expected['nav_as_of']}"
        )
    return {
        "rebuild_id": manifest["rebuild_id"],
        "strategy_id": STRATEGY,
        "as_of_date": manifest["as_of_date"],
        "available_cash": str(final_snapshot["cash"]),
        "holdings_value": str(final_snapshot["holdings_value"]),
        "nav": str(final_snapshot["nav"]),
        "realized_pnl": str(money(final.realized_pnl)),
        "closed_trade_count": len(final.closed_trades),
        "open_positions": actual_positions,
        "snapshots": [
            {
                **row,
                "cash": str(row["cash"]),
                "holdings_value": str(row["holdings_value"]),
                "nav": str(row["nav"]),
            }
            for row in snapshots
        ],
        "closed_trades": [
            {
                "event_id": row.event_id,
                "symbol": row.symbol,
                "entry_date": row.entry_date,
                "entry_price": str(row.entry_price),
                "exit_date": row.exit_date,
                "exit_price": str(row.exit_price),
                "pnl_rate": str(row.pnl_rate),
                "tranches": row.tranches,
                "reason": row.reason,
            }
            for row in final.closed_trades
        ],
        "voided_legacy_events": manifest["voided_legacy_events"],
    }


PRESERVED_TABLE_FILTERS = {
    "trade_history": ("strategy",),
    "portfolio": ("strategy",),
    "portfolio_snapshots": ("strategy",),
    "strategy_accounts": ("strategy_id",),
    "strategy_nav_history": ("strategy_id",),
}

# These tables are historical evidence for the reconstruction.  They are read
# to identify bad legacy actions, but the rebuild must never rewrite them.
FULLY_PRESERVED_TABLES = ("strategy_daily_results",)


def _preserved_digest(conn: sqlite3.Connection, table: str, column: str) -> str:
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE {column} != ? ORDER BY rowid", (STRATEGY,)
    ).fetchall()
    return sha256_json([list(row) for row in rows])


def preserved_digests(conn: sqlite3.Connection) -> dict[str, str]:
    digests = {
        table: _preserved_digest(conn, table, columns[0])
        for table, columns in PRESERVED_TABLE_FILTERS.items()
    }
    for table in FULLY_PRESERVED_TABLES:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        digests[f"{table}:all"] = sha256_json([list(row) for row in rows])
    return digests


def _validate_voided_sources(
    conn: sqlite3.Connection, manifest: dict[str, Any]
) -> None:
    for row in manifest["voided_legacy_events"]:
        exists = conn.execute(
            "SELECT 1 FROM strategy_daily_results "
            "WHERE id=? AND strategy=?",
            (row["daily_result_id"], STRATEGY),
        ).fetchone()
        if exists is None:
            raise RebuildError(
                f"voided source result {row['daily_result_id']} is missing"
            )


def rewrite_dividend_state(
    conn: sqlite3.Connection,
    manifest: dict[str, Any],
    reconciliation: dict[str, Any],
    *,
    source_sha256: str,
    manifest_sha256: str,
) -> None:
    """Rewrite only dividend legacy state in an already isolated DB copy."""

    _validate_voided_sources(conn, manifest)
    current_trade_ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM trade_history WHERE strategy=? ORDER BY id", (STRATEGY,)
        )
    ]
    closed = reconciliation["closed_trades"]
    if len(current_trade_ids) not in {16, len(closed)}:
        raise RebuildError(
            "unexpected dividend trade_history cardinality; refusing rewrite: "
            f"{len(current_trade_ids)}"
        )
    non_dividend_max = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM trade_history WHERE strategy != ?",
        (STRATEGY,),
    ).fetchone()[0]
    while len(current_trade_ids) < len(closed):
        non_dividend_max += 1
        current_trade_ids.append(non_dividend_max)

    final_positions = replay_until(manifest).positions
    existing_portfolio = conn.execute(
        "SELECT id, name_or_code FROM portfolio WHERE strategy=? ORDER BY id",
        (STRATEGY,),
    ).fetchall()
    if len(existing_portfolio) != len(final_positions):
        raise RebuildError(
            "unexpected dividend portfolio cardinality; refusing rewrite"
        )

    audit_payload = {
        "rebuild_id": manifest["rebuild_id"],
        "manifest_sha256": manifest_sha256,
        "source_sha256": source_sha256,
        "as_of_date": manifest["as_of_date"],
        "available_cash": reconciliation["available_cash"],
        "holdings_value": reconciliation["holdings_value"],
        "nav": reconciliation["nav"],
        "voided_legacy_events": manifest["voided_legacy_events"],
    }

    conn.execute("BEGIN IMMEDIATE")
    try:
        for trade_id, row in zip(current_trade_ids, closed):
            reason = f"[REBUILT:{row['event_id']}] {row['reason']}"
            existing = conn.execute(
                "SELECT 1 FROM trade_history WHERE id=?", (trade_id,)
            ).fetchone()
            values = (
                STRATEGY,
                row["symbol"],
                row["entry_date"],
                float(dec(row["entry_price"])),
                row["exit_date"],
                float(dec(row["exit_price"])),
                float(dec(row["pnl_rate"])),
                reason,
                0.0,
                int(row["tranches"]),
            )
            if existing:
                conn.execute(
                    "UPDATE trade_history SET strategy=?, name_or_code=?, "
                    "entry_date=?, entry_price=?, exit_date=?, exit_price=?, "
                    "pnl=?, reason=?, weight=?, shares=? WHERE id=?",
                    (*values, trade_id),
                )
            else:
                conn.execute(
                    "INSERT INTO trade_history "
                    "(id,strategy,name_or_code,entry_date,entry_price,exit_date,"
                    "exit_price,pnl,reason,weight,shares) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (trade_id, *values),
                )

        existing_symbols = {row[1] for row in existing_portfolio}
        if existing_symbols != set(final_positions):
            raise RebuildError(
                "current dividend symbols differ from the independently "
                "reconstructed final positions"
            )
        for symbol, position in sorted(final_positions.items()):
            conn.execute(
                "UPDATE portfolio SET entry_date=?, entry_price=?, "
                "weight=0.0, shares=? WHERE strategy=? AND name_or_code=?",
                (
                    position.entry_date,
                    float(position.entry_price),
                    position.tranches,
                    STRATEGY,
                    symbol,
                ),
            )

        account_update = conn.execute(
            "UPDATE strategy_accounts SET total_capital=?, available_cash=? "
            "WHERE strategy_id=?",
            (
                float(dec(reconciliation["nav"])),
                float(dec(reconciliation["available_cash"])),
                STRATEGY,
            ),
        )
        if account_update.rowcount != 1:
            raise RebuildError("dividend strategy account is missing")

        conn.execute(
            "DELETE FROM strategy_nav_history WHERE strategy_id=?", (STRATEGY,)
        )
        conn.executemany(
            "INSERT INTO strategy_nav_history "
            "(date,strategy_id,nav,cash,holdings_value) VALUES (?,?,?,?,?)",
            [
                (
                    row["date"],
                    STRATEGY,
                    float(dec(row["nav"])),
                    float(dec(row["cash"])),
                    float(dec(row["holdings_value"])),
                )
                for row in reconciliation["snapshots"]
            ],
        )

        conn.execute(
            "DELETE FROM portfolio_snapshots WHERE strategy=?", (STRATEGY,)
        )
        snapshot_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM portfolio_snapshots"
        ).fetchone()[0]
        for row in reconciliation["snapshots"]:
            for symbol in sorted(row["positions"]):
                snapshot_id += 1
                conn.execute(
                    "INSERT INTO portfolio_snapshots "
                    "(id,snapshot_date,strategy,name_or_code,weight) "
                    "VALUES (?,?,?,?,0.0)",
                    (snapshot_id, row["date"], STRATEGY, symbol),
                )

        conn.execute(
            "INSERT OR REPLACE INTO meta_data (key,value) VALUES (?,?)",
            (
                AUDIT_META_KEY,
                json.dumps(
                    audit_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def verify_database(
    conn: sqlite3.Connection,
    manifest: dict[str, Any],
    reconciliation: dict[str, Any],
    expected_preserved: dict[str, str],
) -> dict[str, Any]:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RebuildError(f"SQLite integrity_check failed: {integrity}")
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RebuildError(f"foreign_key_check failed: {foreign_keys}")
    actual_preserved = preserved_digests(conn)
    if actual_preserved != expected_preserved:
        raise RebuildError("non-dividend strategy rows changed during rebuild")

    account = conn.execute(
        "SELECT total_capital,available_cash FROM strategy_accounts "
        "WHERE strategy_id=?",
        (STRATEGY,),
    ).fetchone()
    if account is None:
        raise RebuildError("rebuilt strategy account is missing")
    if money(dec(account[0])) != dec(reconciliation["nav"]):
        raise RebuildError("database total_capital does not match reconciliation")
    if money(dec(account[1])) != dec(reconciliation["available_cash"]):
        raise RebuildError("database available_cash does not match reconciliation")

    positions = {
        row[0]: {
            "entry_date": row[1],
            "entry_price": str(dec(row[2])),
            "tranches": row[3],
        }
        for row in conn.execute(
            "SELECT name_or_code,entry_date,entry_price,shares FROM portfolio "
            "WHERE strategy=? ORDER BY name_or_code",
            (STRATEGY,),
        )
    }
    if positions != reconciliation["open_positions"]:
        raise RebuildError(f"database positions mismatch: {positions}")
    trade_count = conn.execute(
        "SELECT COUNT(*) FROM trade_history WHERE strategy=?", (STRATEGY,)
    ).fetchone()[0]
    if trade_count != reconciliation["closed_trade_count"]:
        raise RebuildError("database closed trade count mismatch")

    return {
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "non_dividend_digests_preserved": True,
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "trade_count": trade_count,
        "position_count": len(positions),
    }


def copy_database(source: Path, output: Path) -> None:
    if source.resolve() == output.resolve():
        raise RebuildError("source and output database must differ")
    if output.exists():
        raise RebuildError(f"output database already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        with sqlite3.connect(output) as output_conn:
            source_conn.backup(output_conn)


def run_rebuild(
    *, source: Path, output: Path, manifest_path: Path, report_path: Path
) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    manifest_path = manifest_path.resolve()
    report_path = report_path.resolve()
    if not source.is_file():
        raise RebuildError(f"source database does not exist: {source}")
    source_sha = sha256_file(source)
    manifest = load_manifest(manifest_path)
    manifest_sha = sha256_json(manifest)
    reconciliation = build_reconciliation(manifest)

    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        expected_preserved = preserved_digests(source_conn)
        _validate_voided_sources(source_conn, manifest)

    copy_database(source, output)
    try:
        with sqlite3.connect(output) as conn:
            apply_v006(conn)
            rewrite_dividend_state(
                conn,
                manifest,
                reconciliation,
                source_sha256=source_sha,
                manifest_sha256=manifest_sha,
            )
            verification = verify_database(
                conn, manifest, reconciliation, expected_preserved
            )
            # Produce one self-contained, hash-stable release artifact.  A WAL
            # sidecar must never be required when this copy is later promoted.
            journal_mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(journal_mode).lower() != "delete":
                raise RebuildError(
                    f"unable to finalize output journal mode: {journal_mode}"
                )
        # The output path is owned by this command.  Once DELETE mode is
        # confirmed and the connection is closed, no WAL/SHM sidecar may be
        # needed by (or shipped with) the release artifact.
        wal_path = Path(f"{output}-wal")
        shm_path = Path(f"{output}-shm")
        if wal_path.exists() and wal_path.stat().st_size:
            raise RebuildError(f"non-empty WAL remains after finalization: {wal_path}")
        wal_path.unlink(missing_ok=True)
        shm_path.unlink(missing_ok=True)
        if sha256_file(source) != source_sha:
            raise RebuildError("source database changed during isolated rebuild")
        result = {
            "status": "verified",
            "source_database": str(source),
            "source_sha256": source_sha,
            "output_database": str(output),
            "output_sha256": sha256_file(output),
            "manifest": str(manifest_path),
            "manifest_sha256": manifest_sha,
            "reconciliation": reconciliation,
            "verification": verification,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return result
    except Exception:
        # A failed output is evidence of an incomplete attempt.  Keep it for
        # inspection instead of silently deleting it.
        raise


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "config" / "dividend_ledger_rebuild_20260719.json",
    )
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_rebuild(
        source=args.source_db,
        output=args.output_db,
        manifest_path=args.manifest,
        report_path=args.report,
    )
    summary = {
        "status": result["status"],
        "output_database": result["output_database"],
        "output_sha256": result["output_sha256"],
        "available_cash": result["reconciliation"]["available_cash"],
        "nav": result["reconciliation"]["nav"],
        "open_positions": sorted(result["reconciliation"]["open_positions"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
