#!/usr/bin/env python3
"""Promote v8 growth ledger rebuild to canonical production database.

Requires:
- Exact confirmation token: APPLY-GROWTH-LEDGER-V8-2026-07-26
- Current Asia/Shanghai date: 2026-07-26
- Canonical production path
- Multi-layer writer fences, online pre-cutover backup, atomic os.replace,
  post-cutover verification, and automatic rollback on failure.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Add scripts directory to import path
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.writer_lock import writer_fence
from migrations.quarantine_manifest import QUARANTINE_PRIMARY_KEYS
import db_utils


CONFIRM_TOKEN_EXPECTED = "APPLY-GROWTH-LEDGER-V8-2026-07-26"
ALLOWED_DATE = "2026-07-26"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class CutoverError(RuntimeError):
    pass


class CutoverAuthorizationError(CutoverError):
    pass


class CutoverValidationError(CutoverError):
    pass


def normalize_path(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(path))))


def get_canonical_production_path():
    return normalize_path(Path(__file__).resolve().parents[1] / "quant_system.db")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def acquire_production_writer_fence(source_path):
    lock_path = normalize_path(source_path) + ".release.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception as exc:
        os.close(descriptor)
        raise CutoverAuthorizationError(
            f"Another production release holds the writer fence: {lock_path}"
        ) from exc
    return descriptor


def release_production_writer_fence(descriptor):
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def online_backup(source_path, target_path):
    source_path = normalize_path(source_path)
    target_path = normalize_path(target_path)
    with sqlite3.connect(source_path, timeout=30.0) as src, sqlite3.connect(
        target_path, timeout=30.0
    ) as dst:
        src.backup(dst)
    return target_path


def validate_cutover_authorization(production_db, confirm_token, now=None):
    canonical = get_canonical_production_path()
    if normalize_path(production_db) != canonical:
        raise CutoverAuthorizationError(
            f"Cutover targets non-canonical path: {production_db!r} vs {canonical!r}"
        )
    if confirm_token != CONFIRM_TOKEN_EXPECTED:
        raise CutoverAuthorizationError("Confirmation token is missing or incorrect")
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    shanghai_now = current.astimezone(SHANGHAI)
    if shanghai_now.date().isoformat() != ALLOWED_DATE:
        raise CutoverAuthorizationError(
            f"Cutover is allowed only on Asia/Shanghai date {ALLOWED_DATE}, current: {shanghai_now.date().isoformat()}"
        )
    return shanghai_now


def _table_digest(conn, table_name, pk_cols):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
    order_clause = ", ".join(pk_cols) if pk_cols else ", ".join(cols)
    cols_clause = ", ".join(f"COALESCE(CAST({c} AS TEXT), 'NULL')" for c in cols)
    sql = f"SELECT {cols_clause} FROM {table_name} ORDER BY {order_clause}"
    digest = hashlib.sha256()
    for row in conn.execute(sql):
        digest.update("|".join(row).encode("utf-8") + b"\n")
    return digest.hexdigest()


def verify_non_growth_tables_unchanged(source_conn, candidate_conn):
    tables = [
        "portfolio",
        "portfolio_snapshots",
        "strategy_daily_results",
        "strategy_nav_history",
        "trade_history",
        "strategy_accounts",
    ]
    for t in tables:
        pk_cols = QUARANTINE_PRIMARY_KEYS.get(t, ())
        pk_str = ", ".join(pk_cols) if pk_cols else "rowid"
        where_src = f"WHERE strategy NOT LIKE 'growth_%'" if "strategy" in [c[1] for c in source_conn.execute(f"PRAGMA table_info({t})").fetchall()] else f"WHERE strategy_id NOT LIKE 'growth_%'" if "strategy_id" in [c[1] for c in source_conn.execute(f"PRAGMA table_info({t})").fetchall()] else ""
        where_dst = where_src

        src_rows = source_conn.execute(f"SELECT * FROM {t} {where_src} ORDER BY {pk_str}").fetchall()
        dst_rows = candidate_conn.execute(f"SELECT * FROM {t} {where_dst} ORDER BY {pk_str}").fetchall()
        if src_rows != dst_rows:
            raise CutoverValidationError(f"Non-growth rows changed in table {t}")


def verify_candidate_db(candidate_path):
    path = normalize_path(candidate_path)
    with sqlite3.connect(path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CutoverValidationError(f"Candidate integrity check failed: {integrity}")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise CutoverValidationError(f"Candidate foreign key violations: {fk}")

        env = conn.execute("SELECT value FROM meta_data WHERE key='database_environment'").fetchone()
        if not env or env[0] != "production":
            raise CutoverValidationError(f"Candidate database_environment must be 'production', got {env[0] if env else 'missing'}")

        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        if uv != 8:
            raise CutoverValidationError(f"Candidate user_version must be 8, got {uv}")

        # Strategy accounts for growth
        accounts = conn.execute(
            "SELECT strategy_id, total_capital, available_cash FROM strategy_accounts WHERE strategy_id LIKE 'growth_%' ORDER BY strategy_id"
        ).fetchall()
        if len(accounts) != 3:
            raise CutoverValidationError(f"Expected 3 growth strategy accounts, found {len(accounts)}")
        for acc in accounts:
            if float(acc["total_capital"]) != 1000000.0 or float(acc["available_cash"]) != 1000000.0:
                raise CutoverValidationError(f"Growth account {acc['strategy_id']} balances mismatch: {dict(acc)}")

        # Effective growth positions
        pos_count = conn.execute("SELECT COUNT(*) FROM portfolio WHERE strategy LIKE 'growth_%' AND shares > 0").fetchone()[0]
        if pos_count != 0:
            raise CutoverValidationError(f"Expected 0 effective growth positions, found {pos_count}")

        # Pending intents
        intents = conn.execute(
            """
            SELECT strategy_id, market, state, COUNT(*) as cnt, MIN(eligible_session) as min_s, MAX(eligible_session) as max_s
            FROM trade_intents t
            WHERE strategy_id LIKE 'growth_%'
              AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s WHERE s.intent_id=t.intent_id)
            GROUP BY strategy_id, market, state
            ORDER BY strategy_id
            """
        ).fetchall()
        expected_intents = {
            "growth_a_stock": ("A", "PENDING", 10, "2026-07-27", "2026-07-27"),
            "growth_hk_stock": ("HK", "PENDING", 2, "2026-07-27", "2026-07-27"),
            "growth_us_stock": ("US", "PENDING", 6, "2026-07-27", "2026-07-27"),
        }
        actual_map = {row["strategy_id"]: (row["market"], row["state"], row["cnt"], row["min_s"], row["max_s"]) for row in intents}
        if actual_map != expected_intents:
            raise CutoverValidationError(f"Growth pending intents mismatch: expected {expected_intents}, got {actual_map}")

        # Ensure no 2026-07-27 FILLED intents
        filled_count = conn.execute("SELECT COUNT(*) FROM trade_intents WHERE state='FILLED' AND eligible_session='2026-07-27'").fetchone()[0]
        if filled_count != 0:
            raise CutoverValidationError(f"Found {filled_count} FILLED intents for 2026-07-27 in candidate")


def promote_cutover(
    *,
    production_db,
    candidate_db,
    rebuild_report_path,
    output_dir,
    expected_production_sha256,
    confirm_token,
    apply_production=False,
    now=None,
):
    if not apply_production:
        raise CutoverAuthorizationError("Cutover requires explicit --apply-production flag")

    prod_path = normalize_path(production_db)
    cand_path = normalize_path(candidate_db)
    output_path = Path(normalize_path(output_dir))

    if output_path.exists():
        raise FileExistsError(f"Output directory already exists: {output_path}")

    authorization_time = validate_cutover_authorization(
        prod_path, confirm_token, now=now
    )

    with writer_fence(prod_path, owner="promote_growth_ledger_rebuild:production", timeout=0.0):
        release_fence = acquire_production_writer_fence(prod_path)
        try:
            prod_sha_before = sha256_file(prod_path)
            if prod_sha_before.lower() != expected_production_sha256.lower():
                raise CutoverValidationError(
                    f"Production DB SHA-256 mismatch before cutover: expected={expected_production_sha256}, actual={prod_sha_before}"
                )

            # Pre-verification of candidate
            verify_candidate_db(cand_path)
            cand_sha = sha256_file(cand_path)

            output_path.mkdir(parents=True, exist_ok=False)

            # Step 1: Pre-cutover backup
            pre_backup_path = output_path / "pre_cutover_backup.db"
            online_backup(prod_path, pre_backup_path)
            pre_backup_sha = sha256_file(pre_backup_path)

            # Verify pre-backup integrity & FK
            with sqlite3.connect(pre_backup_path) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise CutoverValidationError("pre_cutover_backup integrity check failed")
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise CutoverValidationError("pre_cutover_backup foreign key check failed")

            # Verify non-growth tables match between production and candidate
            with sqlite3.connect(prod_path) as src_conn, sqlite3.connect(cand_path) as dst_conn:
                verify_non_growth_tables_unchanged(src_conn, dst_conn)

            # Step 2: Atomic File Swap via temporary file in same directory
            prod_dir = Path(prod_path).parent
            temp_fd, temp_file_path = tempfile.mkstemp(
                dir=prod_dir, prefix="quant_system_cutover_", suffix=".db.tmp"
            )
            try:
                # Copy candidate DB contents to temporary file
                with open(cand_path, "rb") as cand_file, os.fdopen(temp_fd, "wb") as temp_file:
                    for chunk in iter(lambda: cand_file.read(1024 * 1024), b""):
                        temp_file.write(chunk)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())

                # Atomic swap via os.replace
                os.replace(temp_file_path, prod_path)

                # Fsync parent directory
                dir_fd = os.open(prod_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)

                # Post-cutover verification
                try:
                    prod_sha_after = sha256_file(prod_path)
                    if prod_sha_after != cand_sha:
                        raise CutoverValidationError(
                            f"Post-cutover production SHA-256 ({prod_sha_after}) does not match candidate SHA-256 ({cand_sha})"
                        )
                    verify_candidate_db(prod_path)

                    # Post-cutover backup
                    post_backup_path = output_path / "post_cutover_backup.db"
                    online_backup(prod_path, post_backup_path)
                    post_backup_sha = sha256_file(post_backup_path)

                    manifest = {
                        "status": "completed",
                        "cutover_version": 1,
                        "authorized_at": authorization_time.isoformat(),
                        "production_db": prod_path,
                        "candidate_db": cand_path,
                        "rebuild_report_path": normalize_path(rebuild_report_path),
                        "production_sha256_before": prod_sha_before,
                        "candidate_sha256": cand_sha,
                        "production_sha256_after": prod_sha_after,
                        "pre_cutover_backup": str(pre_backup_path),
                        "pre_cutover_backup_sha256": pre_backup_sha,
                        "post_cutover_backup": str(post_backup_path),
                        "post_cutover_backup_sha256": post_backup_sha,
                        "rolled_back": False,
                    }
                    manifest_path = output_path / "cutover_manifest.json"
                    with open(manifest_path, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, ensure_ascii=False, indent=2)

                    return manifest

                except Exception as post_err:
                    # Automatic atomic rollback on post-cutover verification failure
                    print(f"CRITICAL: Post-cutover verification failed ({post_err}). Rollback initiated!", file=sys.stderr)
                    os.replace(pre_backup_path, prod_path)
                    dir_fd = os.open(prod_dir, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)

                    rb_sha = sha256_file(prod_path)
                    with sqlite3.connect(prod_path) as conn:
                        rb_int = conn.execute("PRAGMA integrity_check").fetchone()[0]
                        rb_fk = conn.execute("PRAGMA foreign_key_check").fetchall()

                    manifest = {
                        "status": "rolled_back",
                        "cutover_version": 1,
                        "authorized_at": authorization_time.isoformat(),
                        "production_db": prod_path,
                        "production_sha256_before": prod_sha_before,
                        "production_sha256_after_rollback": rb_sha,
                        "rollback_integrity": rb_int,
                        "rollback_fk_violations": len(rb_fk),
                        "error": str(post_err),
                        "rolled_back": True,
                    }
                    manifest_path = output_path / "cutover_manifest.json"
                    with open(manifest_path, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, ensure_ascii=False, indent=2)

                    raise CutoverError(f"Cutover failed and rolled back safely: {post_err}") from post_err

            finally:
                if os.path.exists(temp_file_path):
                    try:
                        os.unlink(temp_file_path)
                    except Exception:
                        pass
        finally:
            release_production_writer_fence(release_fence)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-db", default=get_canonical_production_path())
    parser.add_argument("--candidate-db", required=True)
    parser.add_argument("--rebuild-report", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expected-production-sha256", required=True)
    parser.add_argument("--confirm-token", required=True)
    parser.add_argument("--apply-production", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = promote_cutover(
            production_db=args.production_db,
            candidate_db=args.candidate_db,
            rebuild_report_path=args.rebuild_report,
            output_dir=args.output_dir,
            expected_production_sha256=args.expected_production_sha256,
            confirm_token=args.confirm_token,
            apply_production=args.apply_production,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"Cutover failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
