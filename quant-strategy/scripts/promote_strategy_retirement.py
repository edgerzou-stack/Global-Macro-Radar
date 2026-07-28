#!/usr/bin/env python3
"""Atomically promote an audited strategy-retirement database candidate."""

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import db_utils
import retire_strategies
from core.writer_lock import writer_fence


CONFIRM_TOKEN = "APPLY-STRATEGY-RETIREMENT-V1"


class PromotionError(RuntimeError):
    pass


def canonical_production_path():
    return Path(db_utils.get_production_db_path()).expanduser().resolve()


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _connect_readonly(path):
    return sqlite3.connect(
        f"file:{Path(path).resolve()}?mode=ro",
        uri=True,
        timeout=30.0,
    )


def _validate_physical_database(path):
    with _connect_readonly(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        environment_row = connection.execute(
            "SELECT value FROM meta_data WHERE key='database_environment'"
        ).fetchone()
    if integrity != "ok":
        raise PromotionError(f"{path} integrity check failed: {integrity}")
    if foreign_keys:
        raise PromotionError(f"{path} foreign key violations: {foreign_keys}")
    if version != 8:
        raise PromotionError(f"{path} must use schema v8, got v{version}")
    if environment_row is None or environment_row[0] != "production":
        raise PromotionError(f"{path} is not labelled production")


def _require_self_contained_database(path):
    wal_path = Path(str(Path(path).resolve()) + "-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise PromotionError(
            f"{path} has an uncheckpointed WAL and is not self-contained"
        )


def validate_candidate(
    *,
    production_db,
    candidate_db,
    acceptance_report,
    expected_production_sha256,
    expected_candidate_sha256,
):
    production = Path(production_db).resolve()
    candidate = Path(candidate_db).resolve()
    report_path = Path(acceptance_report).resolve()
    if production != canonical_production_path():
        raise PromotionError(
            f"Production target is not canonical: {production}"
        )
    if not production.is_file() or not candidate.is_file() or not report_path.is_file():
        raise PromotionError("Production DB, candidate DB, and report must exist")

    production_sha = retire_strategies.sha256_file(production)
    candidate_sha = retire_strategies.sha256_file(candidate)
    if production_sha != expected_production_sha256:
        raise PromotionError(
            "Production SHA-256 mismatch: "
            f"expected={expected_production_sha256}, actual={production_sha}"
        )
    if candidate_sha != expected_candidate_sha256:
        raise PromotionError(
            "Candidate SHA-256 mismatch: "
            f"expected={expected_candidate_sha256}, actual={candidate_sha}"
        )

    report = _read_json(report_path)
    expected_report_values = {
        "retirement_id": retire_strategies.RETIREMENT_ID,
        "source_sha256": production_sha,
        "output_sha256": candidate_sha,
    }
    for field, expected in expected_report_values.items():
        if report.get(field) != expected:
            raise PromotionError(
                f"Acceptance report {field} mismatch: "
                f"expected={expected!r}, actual={report.get(field)!r}"
            )
    if Path(report.get("output_db", "")).resolve() != candidate:
        raise PromotionError("Acceptance report points to a different candidate DB")
    verification = report.get("verification") or {}
    if verification.get("integrity_check") != "ok":
        raise PromotionError("Acceptance report does not certify integrity")
    if verification.get("foreign_key_check") not in ([], None):
        raise PromotionError("Acceptance report contains foreign-key violations")

    _validate_physical_database(production)
    _validate_physical_database(candidate)
    _require_self_contained_database(production)
    _require_self_contained_database(candidate)
    with _connect_readonly(production) as source, _connect_readonly(candidate) as target:
        if retire_strategies.business_digests(source) != (
            retire_strategies.business_digests(target)
        ):
            raise PromotionError("Candidate changed raw business ledger rows")
        retirement_row = target.execute(
            "SELECT value FROM meta_data WHERE key='strategy_retirement_v1'"
        ).fetchone()
        if retirement_row is None:
            raise PromotionError("Candidate lacks strategy_retirement_v1 metadata")
        retire_strategies.verify(target)
    return {
        "production_sha256": production_sha,
        "candidate_sha256": candidate_sha,
        "report": report,
    }


def _acquire_release_lock(production):
    lock_path = Path(str(production) + ".release.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception as error:
        os.close(descriptor)
        raise PromotionError(f"Production release lock is busy: {lock_path}") from error
    return descriptor


def _release_lock(descriptor):
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _online_backup(source, target):
    with sqlite3.connect(source, timeout=30.0) as source_connection:
        with sqlite3.connect(target, timeout=30.0) as target_connection:
            source_connection.backup(target_connection)
    _validate_physical_database(target)


def _stage_file(source, destination_directory, prefix):
    descriptor, temporary = tempfile.mkstemp(
        dir=destination_directory,
        prefix=prefix,
        suffix=".db.tmp",
    )
    try:
        with open(source, "rb") as source_handle, os.fdopen(
            descriptor, "wb"
        ) as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        return Path(temporary)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _replace_database(staged, production):
    for suffix in ("-wal", "-shm"):
        Path(str(production) + suffix).unlink(missing_ok=True)
    os.replace(staged, production)
    directory_descriptor = os.open(production.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _write_manifest(path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def promote(
    *,
    production_db,
    candidate_db,
    acceptance_report,
    output_dir,
    expected_production_sha256,
    expected_candidate_sha256,
    confirm_token,
    apply_production=False,
    fault_injector=None,
):
    if not apply_production:
        raise PromotionError("Promotion requires --apply-production")
    if confirm_token != CONFIRM_TOKEN:
        raise PromotionError("Invalid strategy-retirement confirmation token")

    production = Path(production_db).expanduser().resolve()
    candidate = Path(candidate_db).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output.exists():
        raise FileExistsError(output)

    with writer_fence(
        production,
        owner="promote_strategy_retirement:production",
        timeout=0.0,
    ):
        release_descriptor = _acquire_release_lock(production)
        try:
            validated = validate_candidate(
                production_db=production,
                candidate_db=candidate,
                acceptance_report=acceptance_report,
                expected_production_sha256=expected_production_sha256,
                expected_candidate_sha256=expected_candidate_sha256,
            )
            output.mkdir(parents=True, exist_ok=False)
            online_backup = output / "pre_cutover_online_backup.db"
            _online_backup(production, online_backup)
            backup = output / "pre_cutover_backup.db"
            backup_stage = _stage_file(
                production, output, "pre_cutover_exact_"
            )
            os.replace(backup_stage, backup)
            _validate_physical_database(backup)
            backup_sha = retire_strategies.sha256_file(backup)
            if backup_sha != validated["production_sha256"]:
                raise PromotionError("Exact pre-cutover backup SHA mismatch")
            staged = _stage_file(candidate, production.parent, "strategy_retirement_")
            try:
                _replace_database(staged, production)
                if fault_injector is not None:
                    fault_injector("after_replace")
                actual = retire_strategies.sha256_file(production)
                if actual != validated["candidate_sha256"]:
                    raise PromotionError(
                        "Post-cutover production SHA does not match candidate"
                    )
                _validate_physical_database(production)
                with _connect_readonly(production) as connection:
                    retire_strategies.verify(connection)
                manifest = {
                    "status": "completed",
                    "promotion": "strategy_retirement_v1",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "production_db": str(production),
                    "candidate_db": str(candidate),
                    "acceptance_report": str(Path(acceptance_report).resolve()),
                    "production_sha256_before": validated["production_sha256"],
                    "production_sha256_after": actual,
                    "candidate_sha256": validated["candidate_sha256"],
                    "pre_cutover_backup": str(backup),
                    "pre_cutover_backup_sha256": backup_sha,
                    "pre_cutover_online_backup": str(online_backup),
                    "pre_cutover_online_backup_sha256": (
                        retire_strategies.sha256_file(online_backup)
                    ),
                    "rolled_back": False,
                }
                _write_manifest(output / "cutover_manifest.json", manifest)
                return manifest
            except Exception as error:
                rollback_stage = _stage_file(
                    backup, production.parent, "strategy_retirement_rollback_"
                )
                _replace_database(rollback_stage, production)
                restored_sha = retire_strategies.sha256_file(production)
                manifest = {
                    "status": "rolled_back",
                    "promotion": "strategy_retirement_v1",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "production_db": str(production),
                    "production_sha256_before": validated["production_sha256"],
                    "production_sha256_after_rollback": restored_sha,
                    "pre_cutover_backup": str(backup),
                    "pre_cutover_backup_sha256": backup_sha,
                    "pre_cutover_online_backup": str(online_backup),
                    "pre_cutover_online_backup_sha256": (
                        retire_strategies.sha256_file(online_backup)
                    ),
                    "rolled_back": True,
                    "error": f"{type(error).__name__}: {error}",
                }
                _write_manifest(output / "cutover_manifest.json", manifest)
                if restored_sha != validated["production_sha256"]:
                    raise PromotionError(
                        "Promotion failed and rollback SHA did not match source"
                    ) from error
                raise PromotionError(
                    f"Promotion failed and was rolled back safely: {error}"
                ) from error
            finally:
                staged.unlink(missing_ok=True)
        finally:
            _release_lock(release_descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-db", default=canonical_production_path())
    parser.add_argument("--candidate-db", required=True)
    parser.add_argument("--acceptance-report", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expected-production-sha256", required=True)
    parser.add_argument("--expected-candidate-sha256", required=True)
    parser.add_argument("--confirm-token", required=True)
    parser.add_argument("--apply-production", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = promote(
            production_db=args.production_db,
            candidate_db=args.candidate_db,
            acceptance_report=args.acceptance_report,
            output_dir=args.output_dir,
            expected_production_sha256=args.expected_production_sha256,
            expected_candidate_sha256=args.expected_candidate_sha256,
            confirm_token=args.confirm_token,
            apply_production=args.apply_production,
        )
    except Exception as error:
        print(f"Promotion failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
