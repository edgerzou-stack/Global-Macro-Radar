#!/usr/bin/env python3
"""Rebuild the legacy US hot-spot ledger in a new SQLite database copy.

The source database is always opened read-only.  Conflicting intraday results
remain untouched as research evidence; the final persisted result for each day
is the canonical signal and executes at the next XNYS session open.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any

from core.market import USMarket
from core.quarantine import quarantine_filter
from migrations.quarantine_manifest import (
    apply_quarantine_schema,
    install_quarantine_write_guards,
)
from migrations.v006_execution_ledger import apply_v006


getcontext().prec = 28
CENT = Decimal("0.01")
STRATEGY = "hot_spot_us_stock"
MAX_HOLDINGS = 10
AUDIT_META_KEY = "hotspot_us_ledger_rebuild_v1"


class RebuildError(RuntimeError):
    pass


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RebuildError(f"unable to load rebuild manifest: {error}") from error
    return payload


def _next_us_session(signal_date: str) -> str:
    try:
        signal = dt.date.fromisoformat(signal_date)
    except (TypeError, ValueError) as error:
        raise RebuildError(f"invalid signal date: {signal_date!r}") from error
    market = USMarket()
    return market.get_next_trading_date(signal + dt.timedelta(days=1)).isoformat()


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise RebuildError("unsupported manifest schema_version")
    if manifest.get("strategy_id") != STRATEGY:
        raise RebuildError(f"manifest strategy must be {STRATEGY}")
    if not manifest.get("rebuild_id"):
        raise RebuildError("rebuild_id is required")
    if dec(manifest.get("initial_capital", 0)) <= 0:
        raise RebuildError("initial_capital must be positive")
    if dec(manifest.get("tranche_notional", 0)) <= 0:
        raise RebuildError("tranche_notional must be positive")
    if dec(manifest.get("fee_rate", 0)) < 0:
        raise RebuildError("fee_rate cannot be negative")

    previous_date = ""
    seen_result_ids = set()
    for signal in manifest.get("canonical_signals", []):
        result_id = signal.get("daily_result_id")
        if isinstance(result_id, bool) or not isinstance(result_id, int):
            raise RebuildError("daily_result_id must be an integer")
        if result_id in seen_result_ids:
            raise RebuildError(f"duplicate daily_result_id: {result_id}")
        seen_result_ids.add(result_id)
        signal_date = signal.get("signal_date")
        if previous_date and signal_date <= previous_date:
            raise RebuildError("canonical signals must be strictly date ordered")
        previous_date = signal_date
        expected_execution = _next_us_session(signal_date)
        if signal.get("execution_date") != expected_execution:
            raise RebuildError(
                f"result {result_id} must execute at next XNYS open "
                f"{expected_execution}"
            )
        targets = signal.get("targets")
        if not isinstance(targets, list) or any(
            not isinstance(symbol, str) or not symbol for symbol in targets
        ):
            raise RebuildError(f"invalid targets for result {result_id}")
        if len(targets) != len(set(targets)):
            raise RebuildError(f"duplicate targets for result {result_id}")
        if len(targets) > MAX_HOLDINGS:
            raise RebuildError(
                f"result {result_id} contains more than {MAX_HOLDINGS} targets"
            )

    snapshots = manifest.get("snapshots", [])
    if not snapshots or snapshots[-1].get("date") != manifest.get("as_of_date"):
        raise RebuildError("final snapshot must match as_of_date")
    quarantine = manifest.get("quarantine")
    if not isinstance(quarantine, dict):
        raise RebuildError("quarantine selectors are required")


def validate_canonical_signals(
    connection: sqlite3.Connection, manifest: dict[str, Any]
) -> None:
    for signal in manifest["canonical_signals"]:
        result_id = signal["daily_result_id"]
        signal_date = signal["signal_date"]
        final_row = connection.execute(
            "SELECT id,result_json FROM strategy_daily_results "
            "WHERE strategy=? AND result_date=? ORDER BY id DESC LIMIT 1",
            (STRATEGY, signal_date),
        ).fetchone()
        if final_row is None or final_row[0] != result_id:
            raise RebuildError(
                f"result {result_id} is not the final persisted result for "
                f"{signal_date}"
            )
        try:
            payload = json.loads(final_row[1])
            symbols = [row["股票代码"] for row in payload["results"]]
        except (TypeError, KeyError, json.JSONDecodeError) as error:
            raise RebuildError(
                f"invalid stored result payload for {result_id}"
            ) from error
        if symbols != signal["targets"]:
            raise RebuildError(
                f"manifest targets differ from stored result {result_id}: "
                f"{symbols}"
            )


def _price_bar(manifest: dict[str, Any], date: str, symbol: str) -> dict[str, Any]:
    try:
        bar = manifest["price_bars"][date][symbol]
    except KeyError as error:
        raise RebuildError(f"missing raw price bar for {symbol} on {date}") from error
    if bar.get("adjustment") != "raw":
        raise RebuildError(f"{symbol}/{date} is not explicitly raw/unadjusted")
    if bar.get("source") != "yahoo_chart":
        raise RebuildError(f"unsupported price source for {symbol}/{date}")
    values = {key: dec(bar[key]) for key in ("open", "high", "low", "close")}
    if any(value <= 0 for value in values.values()):
        raise RebuildError(f"non-positive price in {symbol}/{date}")
    if not values["low"] <= values["open"] <= values["high"]:
        raise RebuildError(f"open is outside OHLC range for {symbol}/{date}")
    if not values["low"] <= values["close"] <= values["high"]:
        raise RebuildError(f"close is outside OHLC range for {symbol}/{date}")
    return bar


def derive_events(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    validate_manifest(manifest)
    current: list[str] = []
    events = []
    for signal in manifest["canonical_signals"]:
        targets = signal["targets"]
        execution_date = signal["execution_date"]
        removed = [symbol for symbol in current if symbol not in targets]
        added = [symbol for symbol in targets if symbol not in current]
        for side, symbols in (("SELL", removed), ("BUY", added)):
            for symbol in symbols:
                bar = _price_bar(manifest, execution_date, symbol)
                events.append(
                    {
                        "event_id": (
                            f"result-{signal['daily_result_id']}:"
                            f"{side.lower()}:{symbol}"
                        ),
                        "signal_result_id": signal["daily_result_id"],
                        "signal_date": signal["signal_date"],
                        "execution_date": execution_date,
                        "side": side,
                        "symbol": symbol,
                        "price": str(bar["open"]),
                        "source": bar["source"],
                        "adjustment": bar["adjustment"],
                    }
                )
        current = list(targets)
        if len(current) > MAX_HOLDINGS:
            raise RebuildError("derived portfolio exceeds holding limit")
    return events


@dataclass(frozen=True)
class Position:
    symbol: str
    entry_date: str
    entry_price: Decimal
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


@dataclass
class ReplayState:
    cash: Decimal
    realized_pnl: Decimal
    positions: dict[str, Position]
    closed_trades: list[ClosedTrade]


def replay(
    manifest: dict[str, Any], events: list[dict[str, Any]], cutoff_date: str | None = None
) -> ReplayState:
    notional = dec(manifest["tranche_notional"])
    fee = dec(manifest["fee_rate"])
    state = ReplayState(
        cash=dec(manifest["initial_capital"]),
        realized_pnl=Decimal("0"),
        positions={},
        closed_trades=[],
    )
    for event in events:
        if cutoff_date and event["execution_date"] > cutoff_date:
            break
        symbol = event["symbol"]
        price = dec(event["price"])
        if event["side"] == "BUY":
            if symbol in state.positions:
                raise RebuildError(f"duplicate BUY without SELL: {event['event_id']}")
            if len(state.positions) >= MAX_HOLDINGS:
                raise RebuildError(f"holding limit exceeded by {event['event_id']}")
            if state.cash < notional:
                raise RebuildError(f"negative cash from {event['event_id']}")
            state.cash -= notional
            state.positions[symbol] = Position(
                symbol, event["execution_date"], price, event["event_id"]
            )
            continue
        position = state.positions.get(symbol)
        if position is None:
            raise RebuildError(f"SELL without position: {event['event_id']}")
        pnl_rate = price / position.entry_price - Decimal("1") - fee
        realized = notional * pnl_rate
        state.cash += notional + realized
        state.realized_pnl += realized
        state.closed_trades.append(
            ClosedTrade(
                event["event_id"],
                symbol,
                position.entry_date,
                position.entry_price,
                event["execution_date"],
                price,
                pnl_rate,
            )
        )
        del state.positions[symbol]
    return state


def _snapshot(
    manifest: dict[str, Any], events: list[dict[str, Any]], row: dict[str, Any]
) -> dict[str, Any]:
    state = replay(manifest, events, row["date"])
    closes = {symbol: dec(value) for symbol, value in row["closes"].items()}
    if set(closes) != set(state.positions):
        raise RebuildError(
            f"snapshot {row['date']} symbols differ from replayed positions"
        )
    notional = dec(manifest["tranche_notional"])
    holdings = sum(
        (
            notional * closes[symbol] / position.entry_price
            for symbol, position in state.positions.items()
        ),
        Decimal("0"),
    )
    return {
        "date": row["date"],
        "cash": money(state.cash),
        "holdings_value": money(holdings),
        "nav": money(state.cash + holdings),
        "positions": {
            symbol: {
                "entry_date": position.entry_date,
                "entry_price": str(position.entry_price),
                "tranches": 1,
                "close": str(closes[symbol]),
            }
            for symbol, position in state.positions.items()
        },
    }


def build_reconciliation(manifest: dict[str, Any]) -> dict[str, Any]:
    events = derive_events(manifest)
    final = replay(manifest, events)
    snapshots = [_snapshot(manifest, events, row) for row in manifest["snapshots"]]
    final_snapshot = snapshots[-1]
    open_positions = {
        symbol: {
            "entry_date": position.entry_date,
            "entry_price": str(position.entry_price),
            "tranches": 1,
        }
        for symbol, position in final.positions.items()
    }
    expected = manifest["expected"]
    comparisons = {
        "available_cash": str(final_snapshot["cash"]),
        "holdings_value": str(final_snapshot["holdings_value"]),
        "nav_as_of": str(final_snapshot["nav"]),
        "realized_pnl": str(money(final.realized_pnl)),
        "closed_trade_count": len(final.closed_trades),
        "open_positions": open_positions,
    }
    for key, actual in comparisons.items():
        if actual != expected[key]:
            raise RebuildError(
                f"expected {key} mismatch: actual={actual!r}, "
                f"expected={expected[key]!r}"
            )
    return {
        "rebuild_id": manifest["rebuild_id"],
        "strategy_id": STRATEGY,
        "as_of_date": manifest["as_of_date"],
        "available_cash": str(final_snapshot["cash"]),
        "holdings_value": str(final_snapshot["holdings_value"]),
        "nav": str(final_snapshot["nav"]),
        "realized_pnl": str(money(final.realized_pnl)),
        "open_positions": open_positions,
        "closed_trade_count": len(final.closed_trades),
        "closed_trades": [
            {
                "event_id": trade.event_id,
                "symbol": trade.symbol,
                "entry_date": trade.entry_date,
                "entry_price": str(trade.entry_price),
                "exit_date": trade.exit_date,
                "exit_price": str(trade.exit_price),
                "pnl_rate": str(trade.pnl_rate),
                "tranches": 1,
            }
            for trade in final.closed_trades
        ],
        "events": events,
        "snapshots": [
            {
                **row,
                "cash": str(row["cash"]),
                "holdings_value": str(row["holdings_value"]),
                "nav": str(row["nav"]),
            }
            for row in snapshots
        ],
    }


PRESERVED_TABLE_FILTERS = {
    "trade_history": "strategy",
    "portfolio": "strategy",
    "portfolio_snapshots": "strategy",
    "strategy_accounts": "strategy_id",
    "strategy_nav_history": "strategy_id",
}


def preserved_digests(connection: sqlite3.Connection) -> dict[str, str]:
    result = {}
    for table, column in PRESERVED_TABLE_FILTERS.items():
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE {column} != ? ORDER BY rowid", (STRATEGY,)
        ).fetchall()
        result[table] = sha256_json([list(row) for row in rows])
    rows = connection.execute(
        "SELECT * FROM strategy_daily_results ORDER BY rowid"
    ).fetchall()
    result["strategy_daily_results:all"] = sha256_json(
        [list(row) for row in rows]
    )
    return result


def _row_dict(connection: sqlite3.Connection, table: str, condition: str, values):
    cursor = connection.execute(f"SELECT * FROM {table} WHERE {condition}", values)
    columns = [item[0] for item in cursor.description]
    row = cursor.fetchone()
    if row is None:
        raise RebuildError(f"quarantine source row missing: {table}/{values}")
    return dict(zip(columns, row))


def _insert_quarantine_candidate(
    connection: sqlite3.Connection,
    *,
    manifest_id: str,
    candidate_id: str,
    reason: str,
    table: str,
    rows: list[dict[str, Any]],
) -> None:
    connection.execute(
        "INSERT INTO quarantine_candidates "
        "(manifest_id,candidate_id,confidence,action,reason,selector_json,"
        "copied_row_count) VALUES (?,?,?,?,?,?,?)",
        (
            manifest_id,
            candidate_id,
            "high",
            "quarantine_only",
            reason,
            json.dumps({"table": table}, sort_keys=True),
            len(rows),
        ),
    )
    for row in rows:
        if "id" not in row:
            raise RebuildError(f"unsupported quarantine primary key for {table}")
        primary_key = {"id": row["id"]}
        pk_json = json.dumps(primary_key, sort_keys=True, separators=(",", ":"))
        row_json = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        connection.execute(
            "INSERT INTO quarantine_rows "
            "(manifest_id,candidate_id,source_table,source_pk_json,row_json,row_sha256) "
            "VALUES (?,?,?,?,?,?)",
            (
                manifest_id,
                candidate_id,
                table,
                pk_json,
                row_json,
                hashlib.sha256(row_json.encode("utf-8")).hexdigest(),
            ),
        )
        connection.execute(
            "INSERT INTO quarantine_key_index "
            "(manifest_id,candidate_id,source_table,key_arity,key_1,key_2,"
            "source_pk_json) VALUES (?,?,?,?,?,?,?)",
            (manifest_id, candidate_id, table, 1, str(row["id"]), "", pk_json),
        )


def rewrite_state(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
    reconciliation: dict[str, Any],
    *,
    source_sha256: str,
    manifest_sha256: str,
) -> None:
    validate_canonical_signals(connection, manifest)
    apply_v006(connection)
    apply_quarantine_schema(connection)

    final_positions = reconciliation["open_positions"]
    portfolio_rows = connection.execute(
        "SELECT id,name_or_code FROM portfolio WHERE strategy=? ORDER BY id",
        (STRATEGY,),
    ).fetchall()
    existing_symbols = {row[1] for row in portfolio_rows}
    quarantine_portfolio = set(manifest["quarantine"]["portfolio_symbols"])
    if existing_symbols != set(final_positions) | quarantine_portfolio:
        raise RebuildError(
            "legacy portfolio symbols do not match rebuilt plus quarantined set"
        )

    trade_rows = connection.execute(
        "SELECT id,name_or_code FROM trade_history WHERE strategy=? ORDER BY id",
        (STRATEGY,),
    ).fetchall()
    quarantine_trades = set(manifest["quarantine"]["trade_symbols"])
    if not quarantine_trades.issubset({row[1] for row in trade_rows}):
        raise RebuildError("legacy quarantine trade symbols are missing")

    audit_payload = {
        "rebuild_id": manifest["rebuild_id"],
        "manifest_sha256": manifest_sha256,
        "source_sha256": source_sha256,
        "as_of_date": manifest["as_of_date"],
        "canonical_daily_result_ids": [
            row["daily_result_id"] for row in manifest["canonical_signals"]
        ],
        "available_cash": reconciliation["available_cash"],
        "nav": reconciliation["nav"],
    }

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO quarantine_manifests "
            "(manifest_id,audit_sha256,source_sha256,audit_version,release_mode,"
            "created_at,candidate_count) VALUES (?,?,?,?,?,?,?)",
            (
                manifest["rebuild_id"],
                manifest_sha256,
                source_sha256,
                1,
                "dry-run",
                manifest["created_at"],
                2,
            ),
        )
        portfolio_evidence = [
            _row_dict(
                connection,
                "portfolio",
                "strategy=? AND name_or_code=?",
                (STRATEGY, symbol),
            )
            for symbol in sorted(quarantine_portfolio)
        ]
        _insert_quarantine_candidate(
            connection,
            manifest_id=manifest["rebuild_id"],
            candidate_id="superseded-overlimit-positions",
            reason=(
                "Legacy weekend/intraday state exceeded the ten-position limit; "
                "rows are preserved as immutable evidence but excluded from active state."
            ),
            table="portfolio",
            rows=portfolio_evidence,
        )
        trade_evidence = [
            _row_dict(
                connection,
                "trade_history",
                "strategy=? AND name_or_code=?",
                (STRATEGY, symbol),
            )
            for symbol in sorted(quarantine_trades)
        ]
        _insert_quarantine_candidate(
            connection,
            manifest_id=manifest["rebuild_id"],
            candidate_id="superseded-intraday-trades",
            reason=(
                "The trade came from a non-final same-day screening result and "
                "is not part of the canonical next-session-open replay."
            ),
            table="trade_history",
            rows=trade_evidence,
        )

        for symbol, position in final_positions.items():
            updated = connection.execute(
                "UPDATE portfolio SET entry_date=?,entry_price=?,weight=0.0,shares=1 "
                "WHERE strategy=? AND name_or_code=?",
                (
                    position["entry_date"],
                    float(dec(position["entry_price"])),
                    STRATEGY,
                    symbol,
                ),
            )
            if updated.rowcount != 1:
                raise RebuildError(f"missing final portfolio row: {symbol}")

        existing_trade_ids = {
            symbol: row_id
            for row_id, symbol in trade_rows
            if symbol not in quarantine_trades
        }
        for trade in reconciliation["closed_trades"]:
            reason = (
                f"[REBUILT:{trade['event_id']}] final daily signal; "
                "executed at next XNYS session open"
            )
            values = (
                STRATEGY,
                trade["symbol"],
                trade["entry_date"],
                float(dec(trade["entry_price"])),
                trade["exit_date"],
                float(dec(trade["exit_price"])),
                float(dec(trade["pnl_rate"])),
                reason,
                0.0,
                1,
            )
            trade_id = existing_trade_ids.get(trade["symbol"])
            if trade_id is None:
                connection.execute(
                    "INSERT INTO trade_history "
                    "(strategy,name_or_code,entry_date,entry_price,exit_date,"
                    "exit_price,pnl,reason,weight,shares) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            else:
                connection.execute(
                    "UPDATE trade_history SET strategy=?,name_or_code=?,entry_date=?,"
                    "entry_price=?,exit_date=?,exit_price=?,pnl=?,reason=?,weight=?,"
                    "shares=? WHERE id=?",
                    (*values, trade_id),
                )

        account = connection.execute(
            "UPDATE strategy_accounts SET total_capital=?,available_cash=? "
            "WHERE strategy_id=?",
            (
                float(dec(reconciliation["nav"])),
                float(dec(reconciliation["available_cash"])),
                STRATEGY,
            ),
        )
        if account.rowcount != 1:
            raise RebuildError("strategy account is missing")

        connection.execute(
            "DELETE FROM strategy_nav_history WHERE strategy_id=?", (STRATEGY,)
        )
        connection.executemany(
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
        connection.execute(
            "DELETE FROM portfolio_snapshots WHERE strategy=?", (STRATEGY,)
        )
        snapshot_id = connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM portfolio_snapshots"
        ).fetchone()[0]
        for row in reconciliation["snapshots"]:
            for symbol in row["positions"]:
                snapshot_id += 1
                connection.execute(
                    "INSERT INTO portfolio_snapshots "
                    "(id,snapshot_date,strategy,name_or_code,weight) "
                    "VALUES (?,?,?,?,0.0)",
                    (snapshot_id, row["date"], STRATEGY, symbol),
                )
        connection.execute(
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
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    install_quarantine_write_guards(connection)


def verify_database(
    connection: sqlite3.Connection,
    reconciliation: dict[str, Any],
    expected_preserved: dict[str, str],
) -> dict[str, Any]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RebuildError(f"integrity_check failed: {integrity}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RebuildError(f"foreign_key_check failed: {foreign_keys}")
    if preserved_digests(connection) != expected_preserved:
        raise RebuildError("non-hotspot strategy or daily-result evidence changed")

    suffix, parameters, _keys = quarantine_filter(connection, "portfolio")
    rows = connection.execute(
        "SELECT name_or_code,entry_date,entry_price,shares FROM portfolio "
        "WHERE strategy=?" + suffix + " ORDER BY name_or_code",
        (STRATEGY, *parameters),
    ).fetchall()
    active_positions = {
        row[0]: {
            "entry_date": row[1],
            "entry_price": str(dec(row[2])),
            "tranches": row[3],
        }
        for row in rows
    }
    expected_positions = {
        key: reconciliation["open_positions"][key]
        for key in sorted(reconciliation["open_positions"])
    }
    if active_positions != expected_positions:
        raise RebuildError(f"active portfolio mismatch: {active_positions}")
    if len(active_positions) > MAX_HOLDINGS:
        raise RebuildError("rebuilt active portfolio exceeds holding limit")

    suffix, parameters, _keys = quarantine_filter(connection, "trade_history")
    active_trade_count = connection.execute(
        "SELECT COUNT(*) FROM trade_history WHERE strategy=?" + suffix,
        (STRATEGY, *parameters),
    ).fetchone()[0]
    if active_trade_count != reconciliation["closed_trade_count"]:
        raise RebuildError("active trade count mismatch")

    account = connection.execute(
        "SELECT total_capital,available_cash FROM strategy_accounts "
        "WHERE strategy_id=?",
        (STRATEGY,),
    ).fetchone()
    if money(dec(account[0])) != dec(reconciliation["nav"]):
        raise RebuildError("account NAV mismatch")
    if money(dec(account[1])) != dec(reconciliation["available_cash"]):
        raise RebuildError("account cash mismatch")
    return {
        "integrity_check": integrity,
        "foreign_key_violations": 0,
        "preserved_digests": True,
        "active_position_count": len(active_positions),
        "active_trade_count": active_trade_count,
        "quarantine_row_count": connection.execute(
            "SELECT COUNT(*) FROM quarantine_rows WHERE manifest_id=?",
            (reconciliation["rebuild_id"],),
        ).fetchone()[0],
    }


def copy_database(source: Path, output: Path) -> None:
    if source.resolve() == output.resolve():
        raise RebuildError("source and output database must differ")
    if output.exists():
        raise RebuildError(f"output database already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(
        f"file:{source.resolve()}?mode=ro", uri=True
    ) as source_connection:
        with sqlite3.connect(output) as output_connection:
            source_connection.backup(output_connection)


def run_rebuild(
    *, source: Path, output: Path, manifest_path: Path, report_path: Path
) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    manifest_path = manifest_path.resolve()
    report_path = report_path.resolve()
    if not source.is_file():
        raise RebuildError(f"source database does not exist: {source}")
    manifest = load_manifest(manifest_path)
    validate_manifest(manifest)
    reconciliation = build_reconciliation(manifest)
    source_sha = sha256_file(source)
    manifest_sha = sha256_json(manifest)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_connection:
        validate_canonical_signals(source_connection, manifest)
        expected_preserved = preserved_digests(source_connection)

    copy_database(source, output)
    try:
        with sqlite3.connect(output) as connection:
            rewrite_state(
                connection,
                manifest,
                reconciliation,
                source_sha256=source_sha,
                manifest_sha256=manifest_sha,
            )
            verification = verify_database(
                connection, reconciliation, expected_preserved
            )
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise RebuildError(f"unable to finalize journal mode: {mode}")
        Path(f"{output}-wal").unlink(missing_ok=True)
        Path(f"{output}-shm").unlink(missing_ok=True)
        if sha256_file(source) != source_sha:
            raise RebuildError("source database changed during rebuild")
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
        temporary = report_path.with_name(report_path.name + ".tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, report_path)
        return result
    except Exception:
        output.unlink(missing_ok=True)
        Path(f"{output}-wal").unlink(missing_ok=True)
        Path(f"{output}-shm").unlink(missing_ok=True)
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--output-db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--report", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    result = run_rebuild(
        source=Path(args.source_db),
        output=Path(args.output_db),
        manifest_path=Path(args.manifest),
        report_path=Path(args.report),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
