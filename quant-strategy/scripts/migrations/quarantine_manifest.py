"""Additive quarantine evidence tables and normalized write guards.

These tables preserve exact copies of audit-selected legacy rows. They never
replace, update, delete, or reinterpret the legacy source records.
"""

import sqlite3


QUARANTINE_PRIMARY_KEYS = {
    "portfolio": ("id",),
    "trade_history": ("id",),
    "portfolio_snapshots": ("id",),
    "strategy_daily_results": ("id",),
    "strategy_accounts": ("strategy_id",),
    "strategy_nav_history": ("date", "strategy_id"),
}


def _execute_statements(conn, statements):
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        for statement in statements:
            conn.execute(statement)
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def apply_quarantine_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    _execute_statements(
        conn,
        [
            """
            CREATE TABLE IF NOT EXISTS quarantine_manifests (
                manifest_id TEXT PRIMARY KEY,
                audit_sha256 TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                audit_version INTEGER NOT NULL,
                release_mode TEXT NOT NULL CHECK(release_mode IN ('dry-run', 'production')),
                created_at TEXT NOT NULL,
                candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quarantine_candidates (
                manifest_id TEXT NOT NULL REFERENCES quarantine_manifests(manifest_id),
                candidate_id TEXT NOT NULL,
                confidence TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                selector_json TEXT NOT NULL,
                copied_row_count INTEGER NOT NULL CHECK(copied_row_count >= 0),
                PRIMARY KEY(manifest_id, candidate_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quarantine_rows (
                quarantine_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                manifest_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_pk_json TEXT NOT NULL,
                row_json TEXT NOT NULL,
                row_sha256 TEXT NOT NULL,
                FOREIGN KEY(manifest_id, candidate_id)
                    REFERENCES quarantine_candidates(manifest_id, candidate_id),
                UNIQUE(manifest_id, candidate_id, source_table, source_pk_json)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quarantine_key_index (
                manifest_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                source_table TEXT NOT NULL,
                key_arity INTEGER NOT NULL CHECK(key_arity IN (1, 2)),
                key_1 TEXT NOT NULL,
                key_2 TEXT NOT NULL DEFAULT '',
                source_pk_json TEXT NOT NULL,
                FOREIGN KEY(manifest_id, candidate_id)
                    REFERENCES quarantine_candidates(manifest_id, candidate_id),
                UNIQUE(
                    manifest_id, candidate_id, source_table,
                    key_arity, key_1, key_2
                )
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_quarantine_rows_source
                ON quarantine_rows(source_table, candidate_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_quarantine_key_lookup
                ON quarantine_key_index(source_table, key_arity, key_1, key_2)
            """,
        ],
    )


def install_quarantine_write_guards(conn: sqlite3.Connection) -> None:
    """Protect every normalized quarantine identity from UPDATE and DELETE.

    Trigger installation is additive and deliberately skips legacy tables that
    are absent in a reduced test or historical database. The normalized index
    is populated by the release coordinator after validating source primary
    keys against ``QUARANTINE_PRIMARY_KEYS``.
    """

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    statements = []
    for table, columns in QUARANTINE_PRIMARY_KEYS.items():
        if table not in tables:
            continue
        key_1 = f"CAST(OLD.\"{columns[0]}\" AS TEXT)"
        key_2 = (
            f"CAST(OLD.\"{columns[1]}\" AS TEXT)" if len(columns) == 2 else "''"
        )
        predicate = (
            "EXISTS (SELECT 1 FROM quarantine_key_index q "
            f"WHERE q.source_table='{table}' "
            f"AND q.key_arity={len(columns)} "
            f"AND q.key_1={key_1} AND q.key_2={key_2})"
        )
        for operation in ("UPDATE", "DELETE"):
            trigger = f"protect_quarantine_{table}_{operation.lower()}"
            statements.append(
                f'''CREATE TRIGGER IF NOT EXISTS "{trigger}"
                    BEFORE {operation} ON "{table}"
                    WHEN {predicate}
                    BEGIN
                        SELECT RAISE(ABORT, 'quarantined legacy row is immutable');
                    END'''
            )
    _execute_statements(conn, statements)
