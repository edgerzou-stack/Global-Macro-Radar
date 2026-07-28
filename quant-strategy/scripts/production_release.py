"""Fail-closed v6/quarantine release coordinator.

Default operation is a dry run against SQLite online-backup copies. Production
writes require all of: the canonical production path, an exact confirmation
token, and the fixed Asia/Shanghai maintenance window.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from migrations.quarantine_manifest import (
    QUARANTINE_PRIMARY_KEYS,
    apply_quarantine_schema,
    install_quarantine_write_guards,
)
from migrations.v006_execution_ledger import apply_v006
from migrations.v007_trade_intents import apply_v007
from migrations.v008_trade_execution_evidence import apply_v008
from core.writer_lock import writer_fence
from core.portfolio_limits import MAX_HOLDINGS_PER_STRATEGY


PRODUCTION_CONFIRM_TOKEN = "APPLY-V6-QUARANTINE-2026-07-18"
MAINTENANCE_DATE = "2026-07-18"
MAINTENANCE_START = time(14, 0)
MAINTENANCE_END = time(16, 0)
SHANGHAI = ZoneInfo("Asia/Shanghai")
V6_TABLES = ("orders", "fills", "journal_transactions", "journal_entries")
V7_TABLES = ("trade_intents",)
V8_TABLES = ("trade_execution_evidence", "trade_intent_supersessions")
QUARANTINE_TABLES = (
    "quarantine_manifests",
    "quarantine_candidates",
    "quarantine_rows",
    "quarantine_key_index",
)
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProductionReleaseError(Exception):
    pass


class ProductionAuthorizationError(ProductionReleaseError):
    pass


class AuditDriftError(ProductionReleaseError):
    pass


class SchemaFingerprintMismatch(ProductionReleaseError):
    pass


class DatabaseValidationError(ProductionReleaseError):
    pass


class CandidateSelectionError(ProductionReleaseError):
    pass


class FreshAuditError(ProductionReleaseError):
    pass


def normalize_path(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(path))))


def acquire_production_writer_fence(source_path):
    """Acquire a non-blocking advisory fence shared by production releases.

    The SQLite ``BEGIN IMMEDIATE`` transaction remains the authoritative fence
    against arbitrary database writers. This file lock additionally prevents
    two release coordinators from reaching the mutation phase concurrently.
    """

    lock_path = normalize_path(source_path) + ".release.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(descriptor)
        raise ProductionAuthorizationError(
            f"Another production release holds the writer fence: {lock_path}"
        )
    return descriptor


def release_production_writer_fence(descriptor):
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def get_canonical_production_path():
    return normalize_path(Path(__file__).resolve().parents[1] / "quant_system.db")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value):
    if isinstance(value, bytes):
        return {"__base64__": base64.b64encode(value).decode("ascii")}
    return value


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_sql(sql):
    return " ".join((sql or "").split()).strip().lower()


def _table_sql_map(conn, names):
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
        tuple(names),
    ).fetchall()
    return {row[0]: _normalize_sql(row[1]) for row in rows}


def expected_v6_table_sql():
    with sqlite3.connect(":memory:") as conn:
        apply_v006(conn)
        return _table_sql_map(conn, V6_TABLES)


def validate_v6_name_collisions(conn):
    expected = expected_v6_table_sql()
    existing = _table_sql_map(conn, V6_TABLES)
    mismatches = {
        name: {"expected": expected[name], "actual": actual}
        for name, actual in existing.items()
        if actual != expected[name]
    }
    if mismatches:
        raise SchemaFingerprintMismatch(
            f"Existing v6 table names have incompatible schemas: {sorted(mismatches)}"
        )
    return {
        "present": sorted(existing),
        "fingerprint": hashlib.sha256(
            _canonical_json(existing).encode("utf-8")
        ).hexdigest(),
    }


def v6_fingerprint(conn):
    schemas = _table_sql_map(conn, V6_TABLES)
    if set(schemas) != set(V6_TABLES):
        raise SchemaFingerprintMismatch(
            f"Missing v6 tables after migration: {sorted(set(V6_TABLES) - set(schemas))}"
        )
    expected = expected_v6_table_sql()
    if schemas != expected:
        raise SchemaFingerprintMismatch("Post-migration v6 schema differs from expected schema")
    return hashlib.sha256(_canonical_json(schemas).encode("utf-8")).hexdigest()


def expected_v7_table_sql():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA user_version=6")
        apply_v007(conn)
        return _table_sql_map(conn, V7_TABLES)


def validate_v7_name_collisions(conn):
    expected = expected_v7_table_sql()
    existing = _table_sql_map(conn, V7_TABLES)
    mismatches = {
        name: {"expected": expected[name], "actual": actual}
        for name, actual in existing.items()
        if actual != expected[name]
    }
    if mismatches:
        raise SchemaFingerprintMismatch(
            f"Existing v7 table names have incompatible schemas: {sorted(mismatches)}"
        )
    return {
        "present": sorted(existing),
        "fingerprint": hashlib.sha256(
            _canonical_json(existing).encode("utf-8")
        ).hexdigest(),
    }


def v7_fingerprint(conn):
    schemas = _table_sql_map(conn, V7_TABLES)
    if set(schemas) != set(V7_TABLES):
        raise SchemaFingerprintMismatch(
            f"Missing v7 tables after migration: {sorted(set(V7_TABLES) - set(schemas))}"
        )
    if schemas != expected_v7_table_sql():
        raise SchemaFingerprintMismatch("Post-migration v7 schema differs from expected schema")
    return hashlib.sha256(_canonical_json(schemas).encode("utf-8")).hexdigest()


def expected_v8_table_sql():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA user_version=6")
        apply_v007(conn)
        apply_v008(conn)
        return _table_sql_map(conn, V8_TABLES)


def validate_v8_name_collisions(conn):
    expected = expected_v8_table_sql()
    existing = _table_sql_map(conn, V8_TABLES)
    mismatches = {
        name: {"expected": expected[name], "actual": actual}
        for name, actual in existing.items()
        if actual != expected[name]
    }
    if mismatches:
        raise SchemaFingerprintMismatch(
            f"Existing v8 table names have incompatible schemas: {sorted(mismatches)}"
        )
    return {
        "present": sorted(existing),
        "fingerprint": hashlib.sha256(
            _canonical_json(existing).encode("utf-8")
        ).hexdigest(),
    }


def v8_fingerprint(conn):
    schemas = _table_sql_map(conn, V8_TABLES)
    if set(schemas) != set(V8_TABLES):
        raise SchemaFingerprintMismatch(
            f"Missing v8 tables after migration: {sorted(set(V8_TABLES) - set(schemas))}"
        )
    if schemas != expected_v8_table_sql():
        raise SchemaFingerprintMismatch("Post-migration v8 schema differs from expected schema")
    return hashlib.sha256(_canonical_json(schemas).encode("utf-8")).hexdigest()


def validate_database(conn):
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok":
        raise DatabaseValidationError(f"integrity_check failed: {integrity}")
    if foreign_keys:
        raise DatabaseValidationError(
            f"foreign_key_check found {len(foreign_keys)} violation(s)"
        )
    return {"integrity_check": integrity, "foreign_key_violations": 0}


def validate_against_audit(conn, source_path, audit):
    source_audit = audit.get("source") or {}
    audited_version = source_audit.get("schema_version")
    actual_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if audited_version is not None and actual_version != audited_version:
        raise AuditDriftError(
            f"Schema version drift: audited={audited_version}, current={actual_version}"
        )

    audited_counts = audit.get("table_counts") or {}
    actual_tables = _table_names(conn)
    for table, expected_count in audited_counts.items():
        if table not in actual_tables:
            raise AuditDriftError(f"Audited table is missing: {table}")
        actual_count = conn.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
        ).fetchone()[0]
        if actual_count != expected_count:
            raise AuditDriftError(
                f"Table-count drift for {table}: audited={expected_count}, current={actual_count}"
            )

    if "wal_size_bytes" in source_audit:
        wal_path = normalize_path(source_path) + "-wal"
        actual_wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        if actual_wal_size != source_audit["wal_size_bytes"]:
            raise AuditDriftError(
                f"WAL-size drift: audited={source_audit['wal_size_bytes']}, "
                f"current={actual_wal_size}"
            )
        if "wal_sha256" in source_audit:
            actual_wal_sha = sha256_file(wal_path) if actual_wal_size else None
            if actual_wal_sha != source_audit["wal_sha256"]:
                raise AuditDriftError("WAL content hash differs from the fresh audit")
    return {"schema_version": actual_version, "audited_table_counts_match": True}


def _open_read_only(path):
    return sqlite3.connect(f"file:{normalize_path(path)}?mode=ro", uri=True, timeout=30.0)


def online_backup(source_path, destination_path):
    source_path = normalize_path(source_path)
    destination_path = normalize_path(destination_path)
    if os.path.exists(destination_path):
        raise FileExistsError(destination_path)
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    temp_path = destination_path + f".{uuid.uuid4().hex}.tmp"
    try:
        with _open_read_only(source_path) as source:
            with sqlite3.connect(temp_path, timeout=30.0) as destination:
                source.backup(destination)
        with sqlite3.connect(temp_path, timeout=30.0) as verification:
            validate_database(verification)
        os.replace(temp_path, destination_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return destination_path


def _quote_identifier(identifier):
    if not IDENTIFIER.fullmatch(identifier):
        raise CandidateSelectionError(f"Unsafe SQL identifier in audit: {identifier!r}")
    return f'"{identifier}"'


def _table_names(conn):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _rows_as_dicts(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    columns = [description[0] for description in cursor.description]
    return [
        {column: _json_value(value) for column, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


def _primary_key_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    primary = sorted(
        ((row[5], row[1]) for row in rows if row[5]),
        key=lambda item: item[0],
    )
    if primary:
        return [name for _, name in primary]
    columns = {row[1] for row in rows}
    if "id" in columns:
        return ["id"]
    raise CandidateSelectionError(f"Table {table!r} has no stable primary key")


def legacy_snapshot(conn):
    excluded = (
        set(V6_TABLES) | set(V7_TABLES) | set(V8_TABLES) | set(QUARANTINE_TABLES)
    )
    snapshot = {}
    for table in sorted(_table_names(conn) - excluded):
        rows = _rows_as_dicts(conn, f"SELECT * FROM {_quote_identifier(table)}")
        canonical_rows = sorted(_canonical_json(row) for row in rows)
        snapshot[table] = {
            "row_count": len(rows),
            "sha256": hashlib.sha256(
                "\n".join(canonical_rows).encode("utf-8")
            ).hexdigest(),
        }
    return snapshot


def _select_primary_key_candidate(conn, candidate):
    table = candidate["table"]
    primary_keys = candidate.get("primary_keys") or []
    if not primary_keys:
        raise CandidateSelectionError(
            f"Candidate {candidate['candidate_id']} has no primary_keys"
        )
    columns = _primary_key_columns(conn, table)
    if columns != ["id"]:
        raise CandidateSelectionError(
            f"Candidate {candidate['candidate_id']} expected an id primary key in {table}"
        )
    placeholders = ",".join("?" for _ in primary_keys)
    rows = _rows_as_dicts(
        conn,
        f"SELECT * FROM {_quote_identifier(table)} WHERE id IN ({placeholders}) ORDER BY id",
        tuple(primary_keys),
    )
    expected = candidate.get("row_count")
    if expected is not None and len(rows) != expected:
        raise CandidateSelectionError(
            f"Candidate {candidate['candidate_id']} expected {expected} rows, found {len(rows)}"
        )
    return {table: rows}


def _select_strategy_candidate(conn, candidate):
    strategy_ids = candidate.get("strategy_ids") or []
    if not strategy_ids:
        raise CandidateSelectionError(
            f"Candidate {candidate['candidate_id']} has no strategy_ids"
        )
    placeholders = ",".join("?" for _ in strategy_ids)
    selected = {}
    expected_fields = {
        "strategy_accounts": "account_row_count",
        "strategy_nav_history": "nav_row_count",
    }
    for table in candidate.get("tables") or []:
        rows = _rows_as_dicts(
            conn,
            f"SELECT * FROM {_quote_identifier(table)} "
            f"WHERE strategy_id IN ({placeholders}) ORDER BY strategy_id",
            tuple(strategy_ids),
        )
        expected = candidate.get(expected_fields.get(table, ""))
        if expected is not None and len(rows) != expected:
            raise CandidateSelectionError(
                f"Candidate {candidate['candidate_id']} table {table} expected "
                f"{expected} rows, found {len(rows)}"
            )
        selected[table] = rows
    return selected


def _select_duplicate_candidate(conn, candidate):
    table = candidate["table"]
    if table == "portfolio_snapshots":
        keys = ("snapshot_date", "strategy", "name_or_code")
    elif table == "strategy_daily_results":
        keys = ("result_date", "strategy")
    else:
        raise CandidateSelectionError(
            f"No approved duplicate selector for table {table!r}"
        )
    quoted_keys = ", ".join(_quote_identifier(key) for key in keys)
    groups = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(row_count - 1), 0) FROM ("
        f"SELECT COUNT(*) AS row_count FROM {_quote_identifier(table)} "
        f"GROUP BY {quoted_keys} HAVING COUNT(*) > 1)"
    ).fetchone()
    if groups[0] != candidate.get("duplicate_groups"):
        raise CandidateSelectionError(
            f"Candidate {candidate['candidate_id']} duplicate group drift: {groups[0]}"
        )
    if groups[1] != candidate.get("excess_rows"):
        raise CandidateSelectionError(
            f"Candidate {candidate['candidate_id']} duplicate excess drift: {groups[1]}"
        )
    join = " AND ".join(f"t.{_quote_identifier(key)} = d.{_quote_identifier(key)}" for key in keys)
    rows = _rows_as_dicts(
        conn,
        f"SELECT t.* FROM {_quote_identifier(table)} t JOIN ("
        f"SELECT {quoted_keys} FROM {_quote_identifier(table)} "
        f"GROUP BY {quoted_keys} HAVING COUNT(*) > 1) d ON {join} ORDER BY t.id",
    )
    return {table: rows}


def select_candidate_rows(conn, candidate):
    available = _table_names(conn)
    requested_tables = set(candidate.get("tables") or [candidate.get("table")])
    if None in requested_tables or not requested_tables <= available:
        raise CandidateSelectionError(
            f"Candidate {candidate.get('candidate_id')} references missing table(s): "
            f"{sorted(requested_tables - available)}"
        )
    if candidate.get("primary_keys"):
        return _select_primary_key_candidate(conn, candidate)
    if candidate.get("strategy_ids"):
        return _select_strategy_candidate(conn, candidate)
    if "duplicate_groups" in candidate:
        return _select_duplicate_candidate(conn, candidate)
    raise CandidateSelectionError(
        f"Candidate {candidate.get('candidate_id')} has no approved selector"
    )


def _candidate_selector_fingerprint(candidates):
    return hashlib.sha256(_canonical_json(candidates).encode("utf-8")).hexdigest()


def _selected_candidate_identities(conn, candidates):
    identities = set()
    selections = {}
    for candidate in candidates:
        selected = select_candidate_rows(conn, candidate)
        selections[candidate["candidate_id"]] = {
            table: len(rows) for table, rows in selected.items()
        }
        for table, rows in selected.items():
            pk_columns = _primary_key_columns(conn, table)
            for row in rows:
                primary_key = {column: row[column] for column in pk_columns}
                identities.add((table, _canonical_json(primary_key)))
    return identities, selections


def _table_columns(conn, table):
    return {
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
    }


def _detect_uncovered_anomalies(conn, selected_identities):
    tables = _table_names(conn)
    flagged = []
    covered_identities = set(selected_identities)
    if "quarantine_key_index" in tables:
        for source_table, source_pk_json in conn.execute(
            "SELECT source_table, source_pk_json FROM quarantine_key_index"
        ):
            if source_table not in tables:
                raise FreshAuditError(
                    "Existing quarantine references a missing source table: "
                    f"{source_table!r}"
                )
            try:
                primary_key = json.loads(source_pk_json)
            except (TypeError, json.JSONDecodeError) as error:
                raise FreshAuditError(
                    "Existing quarantine contains invalid source_pk_json"
                ) from error
            expected_columns = set(_primary_key_columns(conn, source_table))
            if not isinstance(primary_key, dict) or set(primary_key) != expected_columns:
                raise FreshAuditError(
                    "Existing quarantine primary key does not match source schema: "
                    f"{source_table!r}"
                )
            covered_identities.add(
                (source_table, _canonical_json(primary_key))
            )

    def flag_query(table, condition):
        if table not in tables:
            return
        rows = _rows_as_dicts(
            conn, f"SELECT * FROM {_quote_identifier(table)} WHERE {condition}"
        )
        pk_columns = _primary_key_columns(conn, table)
        for row in rows:
            primary_key = {column: row[column] for column in pk_columns}
            identity = (table, _canonical_json(primary_key))
            if identity not in covered_identities:
                flagged.append({"table": table, "primary_key": primary_key})

    if "portfolio" in tables:
        columns = _table_columns(conn, "portfolio")
        conditions = []
        if "entry_price" in columns:
            conditions.append("entry_price IS NULL OR entry_price <= 0")
        if "shares" in columns:
            conditions.append("shares IS NULL OR shares <= 0")
        if "strategy" in columns:
            conditions.append("lower(strategy) LIKE 'test%'")
        if conditions:
            flag_query("portfolio", " OR ".join(f"({item})" for item in conditions))

        if {"strategy", "name_or_code"}.issubset(columns):
            primary_key_columns = _primary_key_columns(conn, "portfolio")
            effective_holdings = {}
            for row in _rows_as_dicts(conn, "SELECT * FROM portfolio"):
                primary_key = {
                    column: row[column] for column in primary_key_columns
                }
                identity = ("portfolio", _canonical_json(primary_key))
                if identity in covered_identities:
                    continue
                strategy = row.get("strategy")
                effective_holdings.setdefault(strategy, set()).add(
                    row.get("name_or_code")
                )
            for strategy, symbols in sorted(
                effective_holdings.items(), key=lambda item: str(item[0])
            ):
                if len(symbols) > MAX_HOLDINGS_PER_STRATEGY:
                    flagged.append(
                        {
                            "table": "portfolio",
                            "anomaly": "holding limit exceeded",
                            "strategy": strategy,
                            "holding_count": len(symbols),
                            "maximum": MAX_HOLDINGS_PER_STRATEGY,
                        }
                    )

    for table in ("strategy_accounts", "strategy_nav_history"):
        if table in tables and "strategy_id" in _table_columns(conn, table):
            conditions = ["lower(strategy_id) LIKE 'test%'"]
            columns = _table_columns(conn, table)
            for column in ("total_capital", "available_cash", "nav", "cash", "holdings_value"):
                if column in columns:
                    conditions.append(f"{_quote_identifier(column)} < 0")
            flag_query(table, " OR ".join(f"({item})" for item in conditions))

    if "trade_history" in tables and "reason" in _table_columns(conn, "trade_history"):
        flag_query("trade_history", "lower(COALESCE(reason, '')) LIKE '%corrupt%'")

    for table in ("portfolio_snapshots", "strategy_daily_results"):
        if table in tables and "strategy" in _table_columns(conn, table):
            flag_query(table, "lower(strategy) LIKE 'test%'")

    if "strategy_daily_results" in tables:
        for row in _rows_as_dicts(conn, "SELECT * FROM strategy_daily_results"):
            try:
                parsed = json.loads(row.get("result_json"))
                valid = isinstance(parsed, dict)
            except (TypeError, json.JSONDecodeError):
                valid = False
            if not valid:
                pk_columns = _primary_key_columns(conn, "strategy_daily_results")
                primary_key = {column: row[column] for column in pk_columns}
                identity = (
                    "strategy_daily_results",
                    _canonical_json(primary_key),
                )
                if identity not in covered_identities:
                    flagged.append(
                        {"table": "strategy_daily_results", "primary_key": primary_key}
                    )

    if flagged:
        raise FreshAuditError(
            f"Fresh audit found {len(flagged)} uncovered anomaly row(s): {flagged[:10]}"
        )
    return []


def refresh_audit(*, source_db, baseline_audit_path, output_path, now=None):
    """Create a fresh read-only audit while preserving approved selectors exactly."""
    source_path = normalize_path(source_db)
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    baseline, baseline_sha, baseline_path = _load_audit(baseline_audit_path)
    candidates = baseline["isolation_candidates"]
    current_time = now or datetime.now(SHANGHAI)
    if current_time.tzinfo is None:
        raise FreshAuditError("Fresh audit timestamp must be timezone-aware")
    current_time = current_time.astimezone(SHANGHAI)
    source_sha = sha256_file(source_path)
    wal_path = source_path + "-wal"
    wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0

    with _open_read_only(source_path) as conn:
        checks = validate_database(conn)
        validate_v6_name_collisions(conn)
        validate_v7_name_collisions(conn)
        validate_v8_name_collisions(conn)
        selected_identities, candidate_counts = _selected_candidate_identities(
            conn, candidates
        )
        _detect_uncovered_anomalies(conn, selected_identities)
        table_counts = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
            ).fetchone()[0]
            for table in sorted(_table_names(conn))
        }
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]

    refreshed = json.loads(json.dumps(baseline))
    refreshed["audit_timestamp"] = current_time.isoformat()
    refreshed["scope"] = source_path
    refreshed["mode"] = "fresh_strict_read_only_candidate_selectors_frozen"
    refreshed["source"].update(
        {
            "schema_version": schema_version,
            "size_bytes": os.path.getsize(source_path),
            "sha256": source_sha,
            "wal_size_bytes": wal_size,
            "wal_sha256": sha256_file(wal_path) if wal_size else None,
            "integrity_check": checks["integrity_check"],
            "foreign_key_check_violations": checks["foreign_key_violations"],
        }
    )
    refreshed["table_counts"] = table_counts
    refreshed["refresh"] = {
        "baseline_audit_path": baseline_path,
        "baseline_audit_sha256": baseline_sha,
        "candidate_selector_fingerprint": _candidate_selector_fingerprint(candidates),
        "candidate_row_counts": candidate_counts,
        "uncovered_anomaly_rows": 0,
        "source_opened_read_only": True,
    }
    target = Path(normalize_path(output_path))
    if target.exists():
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(target, refreshed)
    return refreshed


def validate_fresh_audit_for_production(audit, now):
    refresh = audit.get("refresh")
    if not refresh:
        raise FreshAuditError(
            "Production mode requires a fresh audit generated from the approved baseline"
        )
    expected_selector = _candidate_selector_fingerprint(
        audit.get("isolation_candidates") or []
    )
    if refresh.get("candidate_selector_fingerprint") != expected_selector:
        raise FreshAuditError("Candidate selector fingerprint changed after refresh")
    timestamp = datetime.fromisoformat(audit["audit_timestamp"])
    if timestamp.tzinfo is None:
        raise FreshAuditError("Fresh audit timestamp is not timezone-aware")
    age_seconds = (now.astimezone(SHANGHAI) - timestamp.astimezone(SHANGHAI)).total_seconds()
    if age_seconds < -60 or age_seconds > 30 * 60:
        raise FreshAuditError(
            "Fresh audit must be generated no more than 30 minutes before production release"
        )
    if refresh.get("uncovered_anomaly_rows") != 0:
        raise FreshAuditError("Fresh audit contains uncovered anomaly rows")


def _index_quarantine_identity(
    conn, *, manifest_id, candidate_id, source_table, primary_key
):
    expected_columns = QUARANTINE_PRIMARY_KEYS.get(source_table)
    if expected_columns is None:
        raise CandidateSelectionError(
            f"No normalized quarantine primary key is approved for {source_table!r}"
        )
    actual_columns = tuple(_primary_key_columns(conn, source_table))
    if actual_columns != expected_columns or set(primary_key) != set(expected_columns):
        raise CandidateSelectionError(
            f"Quarantine primary-key mismatch for {source_table}: "
            f"expected={expected_columns}, actual={actual_columns}"
        )
    values = tuple(primary_key[column] for column in expected_columns)
    if any(value is None or isinstance(value, (bool, dict, list)) for value in values):
        raise CandidateSelectionError(
            f"Invalid normalized quarantine key for {source_table}: {primary_key!r}"
        )
    if expected_columns == ("id",) and (
        not isinstance(values[0], int) or values[0] <= 0
    ):
        raise CandidateSelectionError(
            f"Invalid integer quarantine id for {source_table}: {values[0]!r}"
        )
    key_1 = str(values[0])
    key_2 = str(values[1]) if len(values) == 2 else ""
    source_pk_json = _canonical_json(primary_key)
    conn.execute(
        """
        INSERT OR IGNORE INTO quarantine_key_index (
            manifest_id, candidate_id, source_table, key_arity,
            key_1, key_2, source_pk_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            manifest_id,
            candidate_id,
            source_table,
            len(values),
            key_1,
            key_2,
            source_pk_json,
        ),
    )


def apply_quarantine_candidates(
    conn,
    *,
    audit,
    audit_sha256,
    source_sha256,
    release_mode,
    created_at,
):
    apply_quarantine_schema(conn)
    manifest_id = f"quarantine-{audit_sha256[:16]}-{source_sha256[:16]}"
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        existing = conn.execute(
            "SELECT manifest_id FROM quarantine_manifests WHERE manifest_id=?",
            (manifest_id,),
        ).fetchone()
        if existing:
            counts = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT candidate_id, copied_row_count "
                    "FROM quarantine_candidates WHERE manifest_id=?",
                    (manifest_id,),
                )
            }
            for candidate_id, source_table, raw_primary_key in conn.execute(
                "SELECT candidate_id, source_table, source_pk_json "
                "FROM quarantine_rows WHERE manifest_id=?",
                (manifest_id,),
            ).fetchall():
                try:
                    primary_key = json.loads(raw_primary_key)
                except (TypeError, json.JSONDecodeError) as error:
                    raise CandidateSelectionError(
                        f"Invalid quarantine primary key for {source_table}"
                    ) from error
                _index_quarantine_identity(
                    conn,
                    manifest_id=manifest_id,
                    candidate_id=candidate_id,
                    source_table=source_table,
                    primary_key=primary_key,
                )
            install_quarantine_write_guards(conn)
            if owns_transaction:
                conn.commit()
            return {
                "manifest_id": manifest_id,
                "candidate_row_counts": counts,
                "idempotent_reuse": True,
            }

        candidates = audit.get("isolation_candidates") or []
        selected = [
            (candidate, select_candidate_rows(conn, candidate))
            for candidate in candidates
        ]
        conn.execute(
            """
            INSERT INTO quarantine_manifests (
                manifest_id, audit_sha256, source_sha256, audit_version,
                release_mode, created_at, candidate_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest_id,
                audit_sha256,
                source_sha256,
                audit["audit_version"],
                release_mode,
                created_at,
                len(candidates),
            ),
        )
        counts = {}
        for candidate, table_rows in selected:
            candidate_id = candidate["candidate_id"]
            copied_count = sum(len(rows) for rows in table_rows.values())
            counts[candidate_id] = copied_count
            selector = {
                key: candidate[key]
                for key in ("table", "tables", "primary_keys", "strategy_ids", "duplicate_groups", "excess_rows")
                if key in candidate
            }
            conn.execute(
                """
                INSERT INTO quarantine_candidates (
                    manifest_id, candidate_id, confidence, action, reason,
                    selector_json, copied_row_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest_id,
                    candidate_id,
                    candidate.get("confidence", "unknown"),
                    candidate.get("action", "unspecified"),
                    candidate.get("reason", ""),
                    _canonical_json(selector),
                    copied_count,
                ),
            )
            for table, rows in table_rows.items():
                pk_columns = _primary_key_columns(conn, table)
                for row in rows:
                    primary_key = {column: row[column] for column in pk_columns}
                    row_json = _canonical_json(row)
                    conn.execute(
                        """
                        INSERT INTO quarantine_rows (
                            manifest_id, candidate_id, source_table,
                            source_pk_json, row_json, row_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            manifest_id,
                            candidate_id,
                            table,
                            _canonical_json(primary_key),
                            row_json,
                            hashlib.sha256(row_json.encode("utf-8")).hexdigest(),
                        ),
                    )
                    _index_quarantine_identity(
                        conn,
                        manifest_id=manifest_id,
                        candidate_id=candidate_id,
                        source_table=table,
                        primary_key=primary_key,
                    )
        install_quarantine_write_guards(conn)
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise
    return {
        "manifest_id": manifest_id,
        "candidate_row_counts": counts,
        "idempotent_reuse": False,
    }


def _load_audit(path):
    audit_path = normalize_path(path)
    raw = Path(audit_path).read_bytes()
    audit = json.loads(raw.decode("utf-8"))
    if audit.get("audit_version") != 1:
        raise ProductionReleaseError("Only audit_version=1 is supported")
    if not isinstance(audit.get("isolation_candidates"), list):
        raise ProductionReleaseError("Audit has no isolation_candidates list")
    return audit, hashlib.sha256(raw).hexdigest(), audit_path


def validate_production_authorization(source_path, confirm_token, now=None):
    if normalize_path(source_path) != normalize_path(get_canonical_production_path()):
        raise ProductionAuthorizationError("Production mode requires the canonical production path")
    if confirm_token != PRODUCTION_CONFIRM_TOKEN:
        raise ProductionAuthorizationError("Production confirmation token is missing or incorrect")
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        raise ProductionAuthorizationError("Maintenance-window time must be timezone-aware")
    shanghai_now = current.astimezone(SHANGHAI)
    if (
        shanghai_now.date().isoformat() != MAINTENANCE_DATE
        or not (MAINTENANCE_START <= shanghai_now.time().replace(tzinfo=None) <= MAINTENANCE_END)
    ):
        raise ProductionAuthorizationError(
            "Production writes are allowed only during 2026-07-18 14:00-16:00 Asia/Shanghai"
        )
    return shanghai_now


def _apply_release_to_database(
    path,
    audit,
    audit_sha,
    source_sha,
    mode,
    created_at,
    *,
    locked_preflight=None,
    fault_injector=None,
):
    with sqlite3.connect(path, timeout=30.0) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if locked_preflight is not None:
                locked_preflight(conn)
            pre_checks = validate_database(conn)
            validate_v6_name_collisions(conn)
            validate_v7_name_collisions(conn)
            validate_v8_name_collisions(conn)
            legacy_before = legacy_snapshot(conn)
            apply_v006(conn)
            if fault_injector:
                fault_injector("after_v006")
            first_fingerprint = v6_fingerprint(conn)
            first_counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
                for table in V6_TABLES
            }
            apply_v006(conn)
            second_fingerprint = v6_fingerprint(conn)
            second_counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
                for table in V6_TABLES
            }
            if first_fingerprint != second_fingerprint or first_counts != second_counts:
                raise SchemaFingerprintMismatch("apply_v006 is not idempotent")
            apply_v007(conn)
            if fault_injector:
                fault_injector("after_v007")
            first_v7_fingerprint = v7_fingerprint(conn)
            first_v7_counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
                for table in V7_TABLES
            }
            apply_v007(conn)
            second_v7_fingerprint = v7_fingerprint(conn)
            second_v7_counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
                for table in V7_TABLES
            }
            if (
                first_v7_fingerprint != second_v7_fingerprint
                or first_v7_counts != second_v7_counts
            ):
                raise SchemaFingerprintMismatch("apply_v007 is not idempotent")
            apply_v008(conn)
            if fault_injector:
                fault_injector("after_v008")
            first_v8_fingerprint = v8_fingerprint(conn)
            first_v8_counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
                for table in V8_TABLES
            }
            apply_v008(conn)
            second_v8_fingerprint = v8_fingerprint(conn)
            second_v8_counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
                for table in V8_TABLES
            }
            if (
                first_v8_fingerprint != second_v8_fingerprint
                or first_v8_counts != second_v8_counts
            ):
                raise SchemaFingerprintMismatch("apply_v008 is not idempotent")
            quarantine = apply_quarantine_candidates(
                conn,
                audit=audit,
                audit_sha256=audit_sha,
                source_sha256=source_sha,
                release_mode=mode,
                created_at=created_at,
            )
            # A baseline audit can predate newly introduced invariants. Recheck
            # the effective post-quarantine state on every dry run as well as a
            # production release, so an over-limit legacy portfolio cannot be
            # handed to the pipeline as an apparently valid working database.
            _detect_uncovered_anomalies(conn, set())
            if fault_injector:
                fault_injector("after_quarantine")
            post_checks = validate_database(conn)
            legacy_after = legacy_snapshot(conn)
            if legacy_before != legacy_after:
                raise DatabaseValidationError(
                    "Legacy tables changed during additive release"
                )
            if fault_injector:
                fault_injector("before_commit")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "pre_checks": pre_checks,
            "post_checks": post_checks,
            "legacy_unchanged": True,
            "v006": {
                "fingerprint": second_fingerprint,
                "applied_twice_idempotently": True,
                "table_counts": second_counts,
            },
            "v007": {
                "fingerprint": second_v7_fingerprint,
                "applied_twice_idempotently": True,
                "table_counts": second_v7_counts,
            },
            "v008": {
                "fingerprint": second_v8_fingerprint,
                "applied_twice_idempotently": True,
                "table_counts": second_v8_counts,
            },
            "quarantine": quarantine,
        }


def prepare_live_shadow_database(path, *, source_sha256):
    """Retag an isolated release copy for non-production pipeline writes.

    A SQLite online backup faithfully copies the source database's
    ``database_environment=production`` marker.  That marker must not be left
    on the writable copy passed to ``--mode live-shadow``: the child stages
    correctly request a test database and would otherwise fail closed with an
    environment mismatch.  Only the already-isolated working copy is changed;
    provenance is retained alongside the new test marker.
    """

    database_path = normalize_path(path)
    if database_path == normalize_path(get_canonical_production_path()):
        raise ProductionAuthorizationError(
            "Refusing to retag the canonical production database for live-shadow"
        )
    with sqlite3.connect(database_path, timeout=30.0) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta_data (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            row = conn.execute(
                "SELECT value FROM meta_data WHERE key = 'database_environment'"
            ).fetchone()
            original_environment = row[0] if row else "unlabeled"
            if original_environment not in {"production", "test", "unlabeled"}:
                raise DatabaseValidationError(
                    "Live-shadow copy must originate from a production, test, or "
                    "legacy unlabeled "
                    f"database, not {original_environment!r}"
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta_data (key, value) VALUES (?, ?)",
                ("database_environment_origin", original_environment),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta_data (key, value) VALUES (?, ?)",
                ("database_environment_source_sha256", str(source_sha256)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta_data (key, value) VALUES (?, ?)",
                ("database_environment", "test"),
            )
            checks = validate_database(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "environment": "test",
        "origin_environment": original_environment,
        "source_sha256": str(source_sha256),
        "checks": checks,
    }


def _write_json_atomic(path, payload):
    path = Path(path)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _write_restore_instructions(output_dir, source_path, pre_backup):
    target = Path(output_dir) / "RESTORE_INSTRUCTIONS.md"
    text = f"""# Restore instructions

Source database: `{source_path}`

Verified pre-release backup: `{pre_backup}`

1. Stop every writer and preserve the current database, WAL, and SHM files as evidence.
2. Open the pre-release backup read-only and run `PRAGMA integrity_check` and `PRAGMA foreign_key_check`.
3. Restore the backup into a new, non-production path using SQLite Online Backup API.
4. Compare schema version, legacy table counts, and hashes with `release_manifest.json`.
5. Request separate explicit approval before changing the production database path.

Never overwrite the live database in place and never drop the trade-history guard trigger.
"""
    target.write_text(text, encoding="utf-8")
    return str(target.resolve())


def _run_release_unlocked(
    *,
    source_db,
    audit_path,
    output_dir,
    apply_production=False,
    confirm_token=None,
    now=None,
):
    source_path = normalize_path(source_db)
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    authorization_time = None
    if apply_production:
        authorization_time = validate_production_authorization(
            source_path, confirm_token, now=now
        )

    audit, audit_sha, audit_path = _load_audit(audit_path)
    if apply_production:
        validate_fresh_audit_for_production(audit, authorization_time)
    source_sha = sha256_file(source_path)
    audited_sha = audit.get("source", {}).get("sha256")
    if source_sha != audited_sha:
        raise AuditDriftError(
            f"Source SHA-256 differs from audit: audited={audited_sha}, current={source_sha}"
        )

    with _open_read_only(source_path) as source:
        source_checks = validate_database(source)
        source_checks.update(validate_against_audit(source, source_path, audit))
        validate_v6_name_collisions(source)
        validate_v7_name_collisions(source)

    output_path = Path(normalize_path(output_dir))
    output_path.mkdir(parents=True, exist_ok=False)
    started_at = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).isoformat()
    pre_backup = online_backup(source_path, output_path / "pre_release_backup.db")
    working_copy = online_backup(source_path, output_path / "working_copy.db")
    restore_path = _write_restore_instructions(output_path, source_path, pre_backup)
    manifest = {
        "release_version": 1,
        "mode": "production" if apply_production else "dry-run",
        "status": "running",
        "started_at": started_at,
        "source_database": source_path,
        "source_sha256_before": source_sha,
        "audit_path": audit_path,
        "audit_sha256": audit_sha,
        "source_checks": source_checks,
        "pre_backup": pre_backup,
        "restore_instructions": restore_path,
        "working_database": working_copy,
    }
    manifest_path = output_path / "release_manifest.json"
    try:
        drill = _apply_release_to_database(
            working_copy,
            audit,
            audit_sha,
            source_sha,
            "dry-run",
            started_at,
        )
        shadow_database = prepare_live_shadow_database(
            working_copy,
            source_sha256=source_sha,
        )
        manifest["working_database_environment"] = shadow_database
        manifest["copy_drill"] = drill
        if apply_production:
            if sha256_file(source_path) != source_sha:
                raise AuditDriftError("Production database changed during copy drill")
            fence = acquire_production_writer_fence(source_path)
            try:
                # Revalidate authorization and the fresh audit immediately before
                # entering the source database's writer transaction. The locked
                # preflight below repeats database/WAL checks after BEGIN IMMEDIATE.
                revalidation_time = now or datetime.now(SHANGHAI)
                authorization_time = validate_production_authorization(
                    source_path, confirm_token, now=revalidation_time
                )
                validate_fresh_audit_for_production(audit, authorization_time)

                def locked_preflight(connection):
                    if sha256_file(source_path) != source_sha:
                        raise AuditDriftError(
                            "Production database changed before locked mutation"
                        )
                    validate_database(connection)
                    validate_against_audit(connection, source_path, audit)
                    validate_v6_name_collisions(connection)
                    validate_v7_name_collisions(connection)

                applied = _apply_release_to_database(
                    source_path,
                    audit,
                    audit_sha,
                    source_sha,
                    "production",
                    started_at,
                    locked_preflight=locked_preflight,
                )
            finally:
                release_production_writer_fence(fence)
            manifest.update(applied)
            manifest["working_database"] = source_path
            post_backup = online_backup(source_path, output_path / "post_release_backup.db")
            manifest["post_backup"] = post_backup
            manifest["source_sha256_after"] = sha256_file(source_path)
        else:
            manifest.update(drill)
            post_backup = online_backup(working_copy, output_path / "post_release_copy.db")
            manifest["post_backup"] = post_backup
            if sha256_file(source_path) != source_sha:
                raise AuditDriftError("Dry run changed the source database")
        manifest["status"] = "completed"
        manifest["completed_at"] = datetime.now(SHANGHAI).isoformat()
        _write_json_atomic(manifest_path, manifest)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["failed_at"] = datetime.now(SHANGHAI).isoformat()
        _write_json_atomic(manifest_path, manifest)
        raise


def run_release(
    *,
    source_db,
    audit_path,
    output_dir,
    apply_production=False,
    confirm_token=None,
    now=None,
):
    """Exclude daily runners for the complete release/copy-drill lifecycle."""
    mode = "production" if apply_production else "dry-run"
    with writer_fence(
        source_db,
        owner=f"production-release:{mode}",
        timeout=0.0,
    ):
        return _run_release_unlocked(
            source_db=source_db,
            audit_path=audit_path,
            output_dir=output_dir,
            apply_production=apply_production,
            confirm_token=confirm_token,
            now=now,
        )


def _default_audit_path():
    return str(Path(__file__).resolve().parents[2] / "reports" / "production_db_audit_20260715.json")


def _default_output_dir():
    timestamp = datetime.now(SHANGHAI).strftime("%Y%m%d_%H%M%S")
    return str(Path(__file__).resolve().parents[2] / "reports" / f"production_release_{timestamp}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=get_canonical_production_path())
    parser.add_argument("--audit", default=_default_audit_path())
    parser.add_argument("--output-dir", default=_default_output_dir())
    parser.add_argument(
        "--refresh-audit-output",
        help="Write a fresh read-only audit using --audit as the frozen-selector baseline, then exit",
    )
    parser.add_argument(
        "--apply-production",
        action="store_true",
        help="Explicitly request production mutation; default is copy-only dry run",
    )
    parser.add_argument("--confirm-token")
    args = parser.parse_args(argv)
    try:
        if args.refresh_audit_output:
            refreshed = refresh_audit(
                source_db=args.source_db,
                baseline_audit_path=args.audit,
                output_path=args.refresh_audit_output,
            )
            print(json.dumps(refreshed, ensure_ascii=False, indent=2))
            return 0
        manifest = run_release(
            source_db=args.source_db,
            audit_path=args.audit,
            output_dir=args.output_dir,
            apply_production=args.apply_production,
            confirm_token=args.confirm_token,
        )
    except Exception as exc:
        print(f"Release stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
