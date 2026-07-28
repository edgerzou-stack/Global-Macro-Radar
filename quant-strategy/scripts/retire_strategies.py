#!/usr/bin/env python3
"""Retire approved empty strategies on an isolated, auditable database copy."""

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

from core.strategy_registry import RETIRED_STRATEGIES
from migrations.quarantine_manifest import (
    QUARANTINE_PRIMARY_KEYS,
    apply_quarantine_schema,
    install_quarantine_write_guards,
)


RETIREMENT_ID = "retire-unused-us-hk-dividend-v1"
STRATEGIES = tuple(sorted(RETIRED_STRATEGIES))
STRATEGY_COLUMNS = {
    "portfolio": "strategy",
    "trade_history": "strategy",
    "portfolio_snapshots": "strategy",
    "strategy_daily_results": "strategy",
    "strategy_accounts": "strategy_id",
    "strategy_nav_history": "strategy_id",
}


class StrategyRetirementError(RuntimeError):
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
        raise StrategyRetirementError("source and output databases must differ")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    output_conn = sqlite3.connect(output)
    try:
        source_conn.backup(output_conn)
    finally:
        output_conn.close()
        source_conn.close()


def table_columns(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def row_payload(columns, row):
    return {column: row[index] for index, column in enumerate(columns)}


def table_digest(connection, table):
    columns = table_columns(connection, table)
    rows = [
        row_payload(columns, row)
        for row in connection.execute(
            f'SELECT * FROM "{table}" ORDER BY '
            + ",".join(f'"{column}"' for column in columns)
        )
    ]
    return sha256_text(canonical_json(rows))


def business_digests(connection):
    excluded = {
        "meta_data",
        "quarantine_manifests",
        "quarantine_candidates",
        "quarantine_rows",
        "quarantine_key_index",
        "sqlite_sequence",
    }
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if row[0] not in excluded
    ]
    return {table: table_digest(connection, table) for table in tables}


def active_predicate(table, alias="s"):
    primary_keys = QUARANTINE_PRIMARY_KEYS[table]
    key_1 = f"CAST({alias}.\"{primary_keys[0]}\" AS TEXT)"
    key_2 = (
        f"CAST({alias}.\"{primary_keys[1]}\" AS TEXT)"
        if len(primary_keys) == 2
        else "''"
    )
    return (
        "NOT EXISTS (SELECT 1 FROM quarantine_key_index q "
        f"WHERE q.source_table='{table}' AND q.key_arity={len(primary_keys)} "
        f"AND q.key_1={key_1} AND q.key_2={key_2})"
    )


def validate_retirable(connection):
    placeholders = ",".join("?" for _ in STRATEGIES)
    active_positions = connection.execute(
        f"SELECT strategy,name_or_code FROM portfolio "
        f"WHERE strategy IN ({placeholders}) AND {active_predicate('portfolio', 'portfolio')}",
        STRATEGIES,
    ).fetchall()
    if active_positions:
        raise StrategyRetirementError(
            f"retired strategies still have active positions: {active_positions}"
        )

    pending_intents = connection.execute(
        f"SELECT intent_id,strategy_id,symbol,state FROM trade_intents "
        f"WHERE strategy_id IN ({placeholders}) AND state='PENDING'",
        STRATEGIES,
    ).fetchall()
    if pending_intents:
        raise StrategyRetirementError(
            f"retired strategies still have pending intents: {pending_intents}"
        )

    open_orders = connection.execute(
        f"SELECT order_id,strategy_id,symbol,state FROM orders "
        f"WHERE strategy_id IN ({placeholders}) "
        "AND state IN ('PENDING','OPEN','PARTIALLY_FILLED')",
        STRATEGIES,
    ).fetchall()
    if open_orders:
        raise StrategyRetirementError(
            f"retired strategies still have open orders: {open_orders}"
        )


def insert_manifest(connection, source_sha256, created_at):
    audit_payload = {
        "retirement_id": RETIREMENT_ID,
        "source_sha256": source_sha256,
        "strategies": STRATEGIES,
        "policy": "retire disabled empty strategies without deleting audit evidence",
    }
    connection.execute(
        """
        INSERT INTO quarantine_manifests (
            manifest_id,audit_sha256,source_sha256,audit_version,release_mode,
            created_at,candidate_count
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            RETIREMENT_ID,
            sha256_text(canonical_json(audit_payload)),
            source_sha256,
            1,
            "dry-run",
            created_at,
            len(STRATEGY_COLUMNS) + 1,
        ),
    )


def quarantine_strategy_rows(connection, table, strategy_column):
    columns = table_columns(connection, table)
    primary_keys = QUARANTINE_PRIMARY_KEYS[table]
    placeholders = ",".join("?" for _ in STRATEGIES)
    rows = connection.execute(
        f'SELECT * FROM "{table}" WHERE "{strategy_column}" IN ({placeholders})',
        STRATEGIES,
    ).fetchall()
    candidate_id = f"retired-{table.replace('_', '-')}"
    connection.execute(
        """
        INSERT INTO quarantine_candidates (
            manifest_id,candidate_id,confidence,action,reason,selector_json,
            copied_row_count
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            RETIREMENT_ID,
            candidate_id,
            "confirmed",
            "exclude_from_active_ledger",
            "strategy retired by policy; raw rows retained as immutable evidence",
            canonical_json(
                {
                    "table": table,
                    "strategy_column": strategy_column,
                    "strategy_ids": STRATEGIES,
                }
            ),
            len(rows),
        ),
    )
    for row in rows:
        payload = row_payload(columns, row)
        primary_key = {column: payload[column] for column in primary_keys}
        primary_key_json = canonical_json(primary_key)
        row_json = canonical_json(payload)
        connection.execute(
            """
            INSERT INTO quarantine_rows (
                manifest_id,candidate_id,source_table,source_pk_json,row_json,
                row_sha256
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                RETIREMENT_ID,
                candidate_id,
                table,
                primary_key_json,
                row_json,
                sha256_text(row_json),
            ),
        )
        values = [str(primary_key[column]) for column in primary_keys]
        connection.execute(
            """
            INSERT INTO quarantine_key_index (
                manifest_id,candidate_id,source_table,key_arity,key_1,key_2,
                source_pk_json
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                RETIREMENT_ID,
                candidate_id,
                table,
                len(primary_keys),
                values[0],
                values[1] if len(values) == 2 else "",
                primary_key_json,
            ),
        )
    return len(rows)


def quarantine_superseded_daily_results(connection):
    rows = connection.execute(
        """
        WITH active AS (
            SELECT d.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.result_date,d.strategy ORDER BY d.id DESC
                   ) AS version_rank
            FROM strategy_daily_results d
            WHERE NOT EXISTS (
                SELECT 1 FROM quarantine_key_index q
                WHERE q.source_table='strategy_daily_results'
                  AND q.key_arity=1
                  AND q.key_1=CAST(d.id AS TEXT)
                  AND q.key_2=''
            )
        )
        SELECT id,result_date,strategy,result_json
        FROM active WHERE version_rank>1 ORDER BY id
        """
    ).fetchall()
    candidate_id = "superseded-strategy-daily-results"
    connection.execute(
        """
        INSERT INTO quarantine_candidates (
            manifest_id,candidate_id,confidence,action,reason,selector_json,
            copied_row_count
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            RETIREMENT_ID,
            candidate_id,
            "confirmed",
            "exclude_from_active_ledger",
            "older same-day strategy result version; highest id remains authoritative",
            canonical_json(
                {
                    "table": "strategy_daily_results",
                    "partition_by": ["result_date", "strategy"],
                    "keep": "highest id",
                }
            ),
            len(rows),
        ),
    )
    for row in rows:
        payload = {
            "id": row[0],
            "result_date": row[1],
            "strategy": row[2],
            "result_json": row[3],
        }
        primary_key = {"id": row[0]}
        primary_key_json = canonical_json(primary_key)
        row_json = canonical_json(payload)
        connection.execute(
            """
            INSERT INTO quarantine_rows (
                manifest_id,candidate_id,source_table,source_pk_json,row_json,
                row_sha256
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                RETIREMENT_ID,
                candidate_id,
                "strategy_daily_results",
                primary_key_json,
                row_json,
                sha256_text(row_json),
            ),
        )
        connection.execute(
            """
            INSERT INTO quarantine_key_index (
                manifest_id,candidate_id,source_table,key_arity,key_1,key_2,
                source_pk_json
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                RETIREMENT_ID,
                candidate_id,
                "strategy_daily_results",
                1,
                str(row[0]),
                "",
                primary_key_json,
            ),
        )
    return len(rows)


def install_daily_result_uniqueness_guard(connection):
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS prevent_active_daily_result_duplicate
        BEFORE INSERT ON strategy_daily_results
        WHEN EXISTS (
            SELECT 1
            FROM strategy_daily_results d
            WHERE d.result_date=NEW.result_date
              AND d.strategy=NEW.strategy
              AND NOT EXISTS (
                  SELECT 1 FROM quarantine_key_index q
                  WHERE q.source_table='strategy_daily_results'
                    AND q.key_arity=1
                    AND q.key_1=CAST(d.id AS TEXT)
                    AND q.key_2=''
              )
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'active strategy_daily_results version already exists'
            );
        END
        """
    )


def retire_copy(connection, source_sha256):
    apply_quarantine_schema(connection)
    if connection.execute(
        "SELECT 1 FROM quarantine_manifests WHERE manifest_id=?",
        (RETIREMENT_ID,),
    ).fetchone():
        raise StrategyRetirementError(f"{RETIREMENT_ID} already applied")
    validate_retirable(connection)
    before = business_digests(connection)
    created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")

    connection.execute("BEGIN IMMEDIATE")
    try:
        insert_manifest(connection, source_sha256, created_at)
        counts = {
            table: quarantine_strategy_rows(connection, table, strategy_column)
            for table, strategy_column in STRATEGY_COLUMNS.items()
        }
        counts["strategy_daily_results_duplicates"] = (
            quarantine_superseded_daily_results(connection)
        )
        install_quarantine_write_guards(connection)
        install_daily_result_uniqueness_guard(connection)
        connection.execute(
            "INSERT OR REPLACE INTO meta_data(key,value) VALUES (?,?)",
            (
                "strategy_retirement_v1",
                canonical_json(
                    {
                        "retirement_id": RETIREMENT_ID,
                        "strategies": STRATEGIES,
                        "source_sha256": source_sha256,
                        "created_at": created_at,
                        "quarantined_rows": counts,
                    }
                ),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    after = business_digests(connection)
    if before != after:
        raise StrategyRetirementError("raw business ledger rows changed")
    return {
        "retirement_id": RETIREMENT_ID,
        "strategies": STRATEGIES,
        "source_sha256": source_sha256,
        "created_at": created_at,
        "quarantined_rows": counts,
        "raw_business_rows_preserved": True,
    }


def verify(connection):
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise StrategyRetirementError(
            f"database validation failed: integrity={integrity}, fk={foreign_keys}"
        )
    placeholders = ",".join("?" for _ in STRATEGIES)
    remaining = {}
    for table, strategy_column in STRATEGY_COLUMNS.items():
        count = connection.execute(
            f'SELECT COUNT(*) FROM "{table}" s '
            f'WHERE s."{strategy_column}" IN ({placeholders}) '
            f"AND {active_predicate(table)}",
            STRATEGIES,
        ).fetchone()[0]
        remaining[table] = count
    if any(remaining.values()):
        raise StrategyRetirementError(
            f"retired strategy rows remain active: {remaining}"
        )
    duplicate_groups = connection.execute(
        """
        SELECT result_date,strategy,COUNT(*)
        FROM strategy_daily_results d
        WHERE NOT EXISTS (
            SELECT 1 FROM quarantine_key_index q
            WHERE q.source_table='strategy_daily_results'
              AND q.key_arity=1
              AND q.key_1=CAST(d.id AS TEXT)
              AND q.key_2=''
        )
        GROUP BY result_date,strategy HAVING COUNT(*)>1
        """
    ).fetchall()
    if duplicate_groups:
        raise StrategyRetirementError(
            f"active daily-result duplicates remain: {duplicate_groups}"
        )
    return {
        "integrity_check": integrity,
        "foreign_key_check": foreign_keys,
        "active_retired_rows": remaining,
        "active_daily_result_duplicate_groups": duplicate_groups,
    }


def write_json(path, payload):
    target = Path(path).resolve()
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--output-db", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)

    source = Path(args.source_db).resolve()
    output = Path(args.output_db).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    source_sha256 = sha256_file(source)
    copy_database(source, output)
    try:
        connection = sqlite3.connect(output)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            retirement = retire_copy(connection, source_sha256)
            verification = verify(connection)
        finally:
            connection.close()
        payload = {
            **retirement,
            "output_db": str(output),
            "output_sha256": sha256_file(output),
            "verification": verification,
        }
        write_json(args.report, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    except Exception:
        output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
