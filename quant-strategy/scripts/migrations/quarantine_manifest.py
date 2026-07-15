"""Additive quarantine evidence tables.

These tables preserve exact copies of audit-selected legacy rows. They never
replace, update, delete, or reinterpret the legacy source records.
"""

import sqlite3


def apply_quarantine_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS quarantine_manifests (
                manifest_id TEXT PRIMARY KEY,
                audit_sha256 TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                audit_version INTEGER NOT NULL,
                release_mode TEXT NOT NULL CHECK(release_mode IN ('dry-run', 'production')),
                created_at TEXT NOT NULL,
                candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0)
            );

            CREATE TABLE IF NOT EXISTS quarantine_candidates (
                manifest_id TEXT NOT NULL REFERENCES quarantine_manifests(manifest_id),
                candidate_id TEXT NOT NULL,
                confidence TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                selector_json TEXT NOT NULL,
                copied_row_count INTEGER NOT NULL CHECK(copied_row_count >= 0),
                PRIMARY KEY(manifest_id, candidate_id)
            );

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
            );

            CREATE INDEX IF NOT EXISTS idx_quarantine_rows_source
                ON quarantine_rows(source_table, candidate_id);
            """
        )
