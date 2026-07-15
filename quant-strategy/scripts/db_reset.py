import sqlite3
import os
import argparse

import db_utils

RESETTABLE_ENVIRONMENTS = {"test", "backtest"}


def _read_database_environment(db_path):
    try:
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            row = conn.execute(
                "SELECT value FROM meta_data WHERE key = ?",
                (db_utils.DATABASE_ENV_KEY,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError(
            f"Database {db_path} has no valid environment sentinel"
        ) from exc
    return row[0] if row else None


def reset_db(db_path=None):
    """Recreate an explicitly marked test/backtest database.

    The production database is rejected by canonical absolute path before it is
    opened. Recreating the file avoids weakening the immutable trade-history
    trigger merely to clear test data.
    """
    if db_path is None:
        raise ValueError("reset_db requires an explicit db_path")

    path = db_utils.normalize_db_path(db_path)
    if path == db_utils.get_production_db_path():
        raise ValueError("Refusing to reset the production database")
    if not os.path.isfile(path):
        raise ValueError(f"Database does not exist: {path}")

    environment = _read_database_environment(path)
    if environment not in RESETTABLE_ENVIRONMENTS:
        raise ValueError(
            f"Database reset requires a test/backtest sentinel; found {environment!r}"
        )

    # Checkpoint committed WAL pages before closing our validation connection.
    # All file removal below is allowed only after the sentinel and canonical
    # production-path checks have succeeded.
    with sqlite3.connect(path, timeout=30.0) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    db_utils.forget_initialized_path(path)
    for candidate in (path + "-wal", path + "-shm", path):
        if os.path.exists(candidate):
            os.unlink(candidate)

    conn = db_utils.init_db(path, environment=environment)
    conn.close()
    print(f"Recreated isolated {environment} database at {path}")
    return path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Recreate an explicitly marked test/backtest SQLite database"
    )
    parser.add_argument("--db-path", required=True)
    args = parser.parse_args()
    reset_db(args.db_path)
