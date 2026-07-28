#!/usr/bin/env python3
"""Rebuild growth paper ledgers on an isolated copy without inventing fills."""

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

from core.trade_intents import TradeIntentLedger
from migrations.v008_trade_execution_evidence import apply_v008


REBUILD_ID = "growth-ledger-rebuild-20260726-v1"
STRATEGIES = ("growth_a_stock", "growth_us_stock", "growth_hk_stock")
INITIAL_CASH = 1_000_000.0
TRANCHE_AMOUNT = 33_000.0
QUARANTINE_TABLES = {
    "portfolio": ("id",),
    "trade_history": ("id",),
    "portfolio_snapshots": ("id",),
    "strategy_nav_history": ("date", "strategy_id"),
}


class GrowthLedgerRebuildError(RuntimeError):
    pass


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_database(source, output):
    if source.resolve() == output.resolve():
        raise GrowthLedgerRebuildError("source and output databases must differ")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as source_conn:
        with sqlite3.connect(output) as output_conn:
            source_conn.backup(output_conn)


def table_columns(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def row_payload(columns, row):
    return {column: row[index] for index, column in enumerate(columns)}


def non_growth_digest(connection):
    specs = {
        "portfolio": ("strategy",),
        "trade_history": ("strategy",),
        "portfolio_snapshots": ("strategy",),
        "strategy_accounts": ("strategy_id",),
        "strategy_nav_history": ("strategy_id",),
        "strategy_daily_results": ("strategy",),
        "trade_intents": ("strategy_id",),
    }
    result = {}
    for table, (strategy_column,) in specs.items():
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        columns = table_columns(connection, table)
        rows = [
            row_payload(columns, row)
            for row in connection.execute(
                f'SELECT * FROM "{table}" WHERE "{strategy_column}" '
                "NOT IN (?,?,?) ORDER BY "
                + ",".join(f'"{column}"' for column in columns),
                STRATEGIES,
            )
        ]
        result[table] = sha256_text(canonical_json(rows))
    return result


def insert_quarantine_manifest(connection, source_sha256, created_at):
    audit_payload = {
        "rebuild_id": REBUILD_ID,
        "source_sha256": source_sha256,
        "strategies": STRATEGIES,
        "policy": "exclude_unverifiable_pre_v8_growth_ledger_rows",
    }
    connection.execute(
        """
        INSERT INTO quarantine_manifests (
            manifest_id,audit_sha256,source_sha256,audit_version,release_mode,
            created_at,candidate_count
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            REBUILD_ID,
            sha256_text(canonical_json(audit_payload)),
            source_sha256,
            1,
            "dry-run",
            created_at,
            len(QUARANTINE_TABLES),
        ),
    )


def quarantine_growth_rows(connection, table, primary_keys):
    columns = table_columns(connection, table)
    strategy_column = "strategy_id" if table == "strategy_nav_history" else "strategy"
    rows = connection.execute(
        f'SELECT * FROM "{table}" WHERE "{strategy_column}" IN (?,?,?)',
        STRATEGIES,
    ).fetchall()
    candidate_id = f"superseded-growth-{table.replace('_', '-')}"
    selector = {
        "table": table,
        "strategy_column": strategy_column,
        "strategies": STRATEGIES,
    }
    connection.execute(
        """
        INSERT INTO quarantine_candidates (
            manifest_id,candidate_id,confidence,action,reason,selector_json,
            copied_row_count
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            REBUILD_ID,
            candidate_id,
            "confirmed",
            "exclude_from_active_ledger",
            "pre-v8 growth ledger execution dates/prices are not replayable",
            canonical_json(selector),
            len(rows),
        ),
    )
    for row in rows:
        payload = row_payload(columns, row)
        pk = {column: payload[column] for column in primary_keys}
        pk_json = canonical_json(pk)
        row_json = canonical_json(payload)
        connection.execute(
            """
            INSERT INTO quarantine_rows (
                manifest_id,candidate_id,source_table,source_pk_json,row_json,
                row_sha256
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                REBUILD_ID,
                candidate_id,
                table,
                pk_json,
                row_json,
                sha256_text(row_json),
            ),
        )
        values = [str(pk[column]) for column in primary_keys]
        # Portfolio has a UNIQUE(strategy, symbol) key. Keeping a superseded
        # row in that active table would block the next verified fill for the
        # same symbol. Preserve its exact row and hash in quarantine evidence,
        # then remove only that archived active row. Other ledger tables can
        # remain in place and are excluded through the normalized key index.
        if table != "portfolio":
            connection.execute(
                """
                INSERT INTO quarantine_key_index (
                    manifest_id,candidate_id,source_table,key_arity,key_1,key_2,
                    source_pk_json
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    REBUILD_ID,
                    candidate_id,
                    table,
                    len(primary_keys),
                    values[0],
                    values[1] if len(values) == 2 else "",
                    pk_json,
                ),
            )
    if table == "portfolio" and rows:
        identifiers = [row_payload(columns, row)["id"] for row in rows]
        connection.executemany(
            "DELETE FROM portfolio WHERE id=?", [(identifier,) for identifier in identifiers]
        )
    return len(rows)


def latest_targets(connection, strategy, limit=10):
    row = connection.execute(
        """
        SELECT result_date,result_json
        FROM strategy_daily_results
        WHERE strategy=?
        ORDER BY result_date DESC,id DESC
        LIMIT 1
        """,
        (strategy,),
    ).fetchone()
    if row is None:
        raise GrowthLedgerRebuildError(f"missing daily results for {strategy}")
    result_date, raw = row
    payload = json.loads(raw)
    targets = []
    for item in payload.get("results") or []:
        symbol = str(item.get("股票代码") or "").strip()
        if symbol and symbol not in targets:
            targets.append(symbol)
        if len(targets) == limit:
            break
    if not targets:
        raise GrowthLedgerRebuildError(f"empty latest target set for {strategy}")
    return result_date, targets


def rebuild_copy(connection, *, source_sha256, signal_date):
    environment = connection.execute(
        "SELECT value FROM meta_data WHERE key='database_environment'"
    ).fetchone()
    if environment is None:
        raise GrowthLedgerRebuildError("database environment is missing")
    apply_v008(connection)
    created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
    before = non_growth_digest(connection)

    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM quarantine_manifests WHERE manifest_id=?", (REBUILD_ID,)
        ).fetchone():
            raise GrowthLedgerRebuildError(f"{REBUILD_ID} already applied")
        insert_quarantine_manifest(connection, source_sha256, created_at)
        quarantined = {
            table: quarantine_growth_rows(connection, table, primary_keys)
            for table, primary_keys in QUARANTINE_TABLES.items()
        }
        connection.execute(
            """
            INSERT INTO trade_intent_supersessions (
                intent_id,rebuild_id,reason,superseded_at
            )
            SELECT intent_id,?,? ,?
            FROM trade_intents
            WHERE strategy_id IN (?,?,?)
            """,
            (
                REBUILD_ID,
                "superseded by audited growth-ledger cutover",
                created_at,
                *STRATEGIES,
            ),
        )
        for strategy in STRATEGIES:
            updated = connection.execute(
                "UPDATE strategy_accounts SET total_capital=?,available_cash=? "
                "WHERE strategy_id=?",
                (INITIAL_CASH, INITIAL_CASH, strategy),
            )
            if updated.rowcount != 1:
                raise GrowthLedgerRebuildError(f"missing account for {strategy}")
            connection.execute(
                "INSERT OR REPLACE INTO meta_data(key,value) VALUES (?,?)",
                (
                    f"cash_replay_enforced:{strategy}",
                    canonical_json(
                        {
                            "initial_cash": INITIAL_CASH,
                            "tranche_amount": TRANCHE_AMOUNT,
                            "tolerance": 0.01,
                            "rebuild_id": REBUILD_ID,
                        }
                    ),
                ),
            )

        ledger = TradeIntentLedger(connection, tranche_amount=TRANCHE_AMOUNT)
        targets = {}
        research_dates = {}
        for strategy in STRATEGIES:
            result_date, ranked = latest_targets(connection, strategy)
            research_dates[strategy] = result_date
            targets[strategy] = ranked
            ledger.plan_strategy(
                run_id=f"{REBUILD_ID}:{strategy}",
                signal_date=signal_date,
                strategy_id=strategy,
                ranked_targets=ranked,
                reason="audited growth-ledger restart; awaiting exact raw open",
                manage_transaction=False,
            )
        connection.execute(
            "INSERT OR REPLACE INTO meta_data(key,value) VALUES (?,?)",
            (
                "growth_ledger_rebuild_v1",
                canonical_json(
                    {
                        "rebuild_id": REBUILD_ID,
                        "source_sha256": source_sha256,
                        "signal_date": signal_date,
                        "research_dates": research_dates,
                        "targets": targets,
                        "quarantined_rows": quarantined,
                    }
                ),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    after = non_growth_digest(connection)
    if before != after:
        raise GrowthLedgerRebuildError("non-growth strategy rows changed")
    return {
        "rebuild_id": REBUILD_ID,
        "signal_date": signal_date,
        "research_dates": research_dates,
        "targets": targets,
        "quarantined_rows": quarantined,
        "non_growth_digests_preserved": True,
    }


def verify(connection, reconciliation):
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise GrowthLedgerRebuildError(
            f"database validation failed: integrity={integrity}, fk={foreign_keys}"
        )
    for strategy in STRATEGIES:
        account = connection.execute(
            "SELECT total_capital,available_cash FROM strategy_accounts "
            "WHERE strategy_id=?",
            (strategy,),
        ).fetchone()
        account_values = tuple(account) if account is not None else None
        if account_values != (INITIAL_CASH, INITIAL_CASH):
            raise GrowthLedgerRebuildError(
                f"unexpected rebuilt account for {strategy}: {account_values}"
            )
        effective_positions = connection.execute(
            """
            SELECT COUNT(*) FROM portfolio p
            WHERE p.strategy=?
              AND NOT EXISTS (
                  SELECT 1 FROM quarantine_key_index q
                  WHERE q.source_table='portfolio' AND q.key_arity=1
                    AND q.key_1=CAST(p.id AS TEXT) AND q.key_2=''
              )
            """,
            (strategy,),
        ).fetchone()[0]
        if effective_positions != 0:
            raise GrowthLedgerRebuildError(
                f"{strategy} retained {effective_positions} unverified positions"
            )
        active_intents = connection.execute(
            """
            SELECT symbol FROM trade_intents i
            WHERE i.strategy_id=? AND i.state='PENDING'
              AND NOT EXISTS (
                  SELECT 1 FROM trade_intent_supersessions s
                  WHERE s.intent_id=i.intent_id
              )
            ORDER BY target_rank,symbol
            """,
            (strategy,),
        ).fetchall()
        if [row[0] for row in active_intents] != reconciliation["targets"][strategy]:
            raise GrowthLedgerRebuildError(
                f"{strategy} pending targets do not match reconciliation"
            )
    return {
        "integrity_check": integrity,
        "foreign_key_violations": 0,
        "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "accounts_reset": len(STRATEGIES),
        "effective_positions": 0,
        "pending_intents": sum(len(v) for v in reconciliation["targets"].values()),
    }


def run(source, output, report, signal_date):
    source = source.resolve()
    output = output.resolve()
    report = report.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    dt.date.fromisoformat(signal_date)
    source_sha = sha256_file(source)
    copy_database(source, output)
    with sqlite3.connect(output, timeout=30.0) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "UPDATE meta_data SET value='test' WHERE key='database_environment'"
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta_data(key,value) VALUES "
            "('database_environment_source_sha256',?)",
            (source_sha,),
        )
        connection.commit()
        reconciliation = rebuild_copy(
            connection, source_sha256=source_sha, signal_date=signal_date
        )
        verification = verify(connection, reconciliation)
        mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise GrowthLedgerRebuildError(f"unable to finalize journal mode: {mode}")
    if sha256_file(source) != source_sha:
        raise GrowthLedgerRebuildError("source database changed during rebuild")
    result = {
        "status": "verified",
        "source_database": str(source),
        "source_sha256": source_sha,
        "output_database": str(output),
        "output_sha256": sha256_file(output),
        "reconciliation": reconciliation,
        "verification": verification,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--signal-date", required=True, help="YYYY-MM-DD")
    return parser.parse_args()


def main():
    args = parse_args()
    result = run(
        args.source_db, args.output_db, args.report, args.signal_date
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
