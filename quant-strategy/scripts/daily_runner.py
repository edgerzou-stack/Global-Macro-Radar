import argparse
import datetime as dt
import hashlib
import subprocess
import sys
import os
import logging
import shlex
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo
from core.clock import clock
from core.db_manager import DBManager
from core.run_context import (
    CheckpointStore,
    DeliveryMode,
    FixtureBundle,
    RunContext,
    RunContextError,
    RunMode,
    read_artifact_envelope,
    write_artifact_envelope,
)
from core.writer_lock import writer_fence

# === Path Configuration ===
# P2.13: 使用相对路径和环境变量，去除硬编码
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))
from config import PROJECT_ROOT
PROJECT_DIR = PROJECT_ROOT
RADAR_DIR = os.environ.get("RADAR_ROOT", os.path.join(ROOT_DIR, "industry-radar"))
SCRIPTS_DIR = CURRENT_DIR

# P3.19 优化：引入 logging 系统替代 print
os.makedirs(os.path.join(PROJECT_DIR, "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(PROJECT_DIR, "logs", "daily_runner.log")),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class PipelineCommandError(RuntimeError):
    def __init__(self, command_key, return_code):
        super().__init__(f"Command failed with exit code {return_code}: {command_key}")
        self.command_key = command_key
        self.return_code = int(return_code)


def resolve_stage_python(environment_name):
    """Use an explicit override or inherit the coordinator interpreter."""
    return shlex.split(os.environ.get(environment_name, sys.executable))


def build_parser():
    parser = argparse.ArgumentParser(description="Run the identity-bound daily pipeline")
    parser.add_argument(
        "--mode",
        required=True,
        choices=[mode.value for mode in RunMode],
        help="Explicit execution mode; there is no implicit production default",
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Explicit SQLite database used by every quantitative stage",
    )
    parser.add_argument("--run-id", help="Stable identity used to resume an interrupted run")
    parser.add_argument(
        "--effective-date",
        default=None,
        help="Run date in YYYY-MM-DD; defaults to the shared clock date",
    )
    parser.add_argument(
        "--artifact-root",
        default=os.path.join(PROJECT_DIR, "reports", "pipeline-runs"),
    )
    parser.add_argument(
        "--checkpoint",
        help="Optional explicit checkpoint path; defaults below the run artifact directory",
    )
    parser.add_argument(
        "--fixture-root",
        help=(
            "Directory containing the complete fixed-name fixture bundle; "
            "required in offline mode"
        ),
    )
    parser.add_argument(
        "--rss-fixture",
        help=(
            "Identity-bound RSS snapshot used by historical live-shadow runs "
            "without replacing the other live data sources"
        ),
    )
    parser.add_argument(
        "--expected-source-sha256",
        help=(
            "Required in live-shadow mode. Must match the immutable source "
            "SHA-256 recorded when the isolated database was prepared."
        ),
    )
    parser.add_argument(
        "--confirm-production-writes",
        action="store_true",
        help="Required second acknowledgement for production ledger writes",
    )
    parser.add_argument(
        "--delivery-mode",
        choices=[DeliveryMode.SINK.value, DeliveryMode.LIVE.value],
        default=DeliveryMode.SINK.value,
        help="Delivery defaults to a local sink; live SMTP is production-only",
    )
    parser.add_argument(
        "--confirm-live-delivery",
        action="store_true",
        help="Required second acknowledgement for live SMTP delivery",
    )
    return parser


def _resolve_identity_alias(cli_value, primary_name, legacy_name, default=None):
    candidates = [
        value
        for value in (
            cli_value,
            os.environ.get(primary_name),
            os.environ.get(legacy_name),
        )
        if value
    ]
    if len(set(candidates)) > 1:
        raise RunContextError(
            f"Conflicting {primary_name}/{legacy_name} identity values"
        )
    return candidates[0] if candidates else default


def create_run_context(args):
    run_id = _resolve_identity_alias(
        args.run_id, "PIPELINE_RUN_ID", "RUN_ID"
    )
    effective_date = _resolve_identity_alias(
        args.effective_date,
        "PIPELINE_EFFECTIVE_DATE",
        "EFFECTIVE_DATE",
        default=clock.today().isoformat(),
    )
    database_path = str(Path(args.database).expanduser().resolve())
    expected_source_sha256 = getattr(args, "expected_source_sha256", None)
    if expected_source_sha256:
        expected_source_sha256 = expected_source_sha256.strip().lower()
        if (
            len(expected_source_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_source_sha256)
        ):
            raise RunContextError(
                "--expected-source-sha256 must be a 64-character hexadecimal SHA-256"
            )
    import db_utils

    canonical_production = str(Path(db_utils.get_production_db_path()).resolve())
    delivery_mode = DeliveryMode(args.delivery_mode)
    if delivery_mode is DeliveryMode.LIVE:
        if args.mode != RunMode.PRODUCTION.value:
            raise RunContextError("Live delivery is only allowed in production mode")
        if not args.confirm_live_delivery:
            raise RunContextError(
                "Live delivery requires --confirm-live-delivery"
            )
    elif args.confirm_live_delivery:
        raise RunContextError(
            "--confirm-live-delivery is only valid with --delivery-mode live"
        )
    if args.mode == RunMode.PRODUCTION.value:
        if not args.confirm_production_writes:
            raise RunContextError(
                "Production mode requires --confirm-production-writes"
            )
        if database_path != canonical_production:
            raise RunContextError(
                "Production mode must target the canonical production database: "
                f"{canonical_production}"
            )
    else:
        if database_path == canonical_production:
            raise RunContextError(
                "Non-production modes cannot target the canonical production database"
            )
        if args.mode == RunMode.LIVE_SHADOW.value and not Path(database_path).is_file():
            raise RunContextError(
                "Live-shadow mode requires an existing isolated database copy"
            )
        if args.mode == RunMode.LIVE_SHADOW.value and not expected_source_sha256:
            raise RunContextError(
                "Live-shadow mode requires --expected-source-sha256 so an old "
                "production copy cannot be silently substituted"
            )
    if args.mode != RunMode.LIVE_SHADOW.value and expected_source_sha256:
        raise RunContextError(
            "--expected-source-sha256 is only valid in live-shadow mode"
        )
    fixture_root = getattr(args, "fixture_root", None)
    rss_fixture = getattr(args, "rss_fixture", None)
    if fixture_root and rss_fixture:
        raise RunContextError("--fixture-root and --rss-fixture cannot be combined")
    if rss_fixture and args.mode != RunMode.LIVE_SHADOW.value:
        raise RunContextError("--rss-fixture is only valid in live-shadow mode")
    if args.mode == RunMode.OFFLINE.value and not fixture_root:
        raise RunContextError("Offline mode requires an explicit --fixture-root")
    fixture_bundle = FixtureBundle.from_root(fixture_root) if fixture_root else None
    fixture_paths = fixture_bundle.environment if fixture_bundle else {}
    fixture_manifest = fixture_bundle.manifest if fixture_bundle else None
    if rss_fixture:
        rss_fixture_path = Path(rss_fixture).expanduser().resolve()
        if not rss_fixture_path.is_file():
            raise RunContextError(
                f"RSS fixture must be an existing file: {rss_fixture_path}"
            )
        fixture_paths["RADAR_RSS_FIXTURE"] = str(rss_fixture_path)
        fixture_manifest = {
            "RADAR_RSS_FIXTURE": {
                "path": str(rss_fixture_path),
                "sha256": _sha256_file(rss_fixture_path),
            }
        }
    configuration = {
        "project_dir": PROJECT_DIR,
        "radar_dir": RADAR_DIR,
        "radar_python": os.environ.get("RADAR_PYTHON", "python3"),
        "quant_python": os.environ.get("QUANT_PYTHON", "python3"),
        "mode": args.mode,
        "database_path": database_path,
        "delivery_mode": delivery_mode.value,
        "database_source_sha256": expected_source_sha256,
        "fixtures": fixture_manifest,
    }
    return RunContext.create(
        mode=args.mode,
        database_path=database_path,
        effective_date=effective_date,
        configuration=configuration,
        artifact_root=args.artifact_root,
        run_id=run_id,
        delivery_mode=delivery_mode,
        fixture_paths=fixture_paths or None,
        database_source_sha256=expected_source_sha256,
    )


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_live_shadow_database_identity(context):
    """Fail closed if the writable copy is not the explicitly approved source."""

    if context.mode is not RunMode.LIVE_SHADOW:
        return None
    expected = context.database_source_sha256
    if not expected:
        raise RunContextError("Live-shadow database source SHA-256 is missing")

    with sqlite3.connect(context.database_path, timeout=30.0) as connection:
        rows = connection.execute(
            "SELECT key, value FROM meta_data WHERE key IN (?, ?, ?)",
            (
                "database_environment",
                "database_environment_origin",
                "database_environment_source_sha256",
            ),
        ).fetchall()
        metadata = dict(rows)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]

    if integrity != "ok":
        raise RunContextError(
            f"Live-shadow database integrity check failed: {integrity}"
        )
    if metadata.get("database_environment") != "test":
        raise RunContextError(
            "Live-shadow database must be an isolated test copy prepared by "
            "prepare_live_shadow_database"
        )
    if not metadata.get("database_environment_origin"):
        raise RunContextError(
            "Live-shadow database is missing database_environment_origin provenance"
        )
    actual_source = metadata.get("database_environment_source_sha256", "").lower()
    if actual_source != expected:
        raise RunContextError(
            "Live-shadow database source mismatch: "
            f"expected={expected}, recorded={actual_source or 'missing'}"
        )
    return {
        "database_sha256_at_start": _sha256_file(context.database_path),
        "environment": metadata["database_environment"],
        "origin_environment": metadata["database_environment_origin"],
        "source_sha256": actual_source,
        "integrity_check": integrity,
    }


def validate_live_shadow_rss_input(context):
    """Require dated news input when replaying an earlier live-shadow date."""
    if context.mode is not RunMode.LIVE_SHADOW:
        return
    runtime_date = context.created_at.astimezone(
        ZoneInfo("Asia/Shanghai")
    ).date()
    if context.effective_date == runtime_date:
        return
    fixture_paths = dict(context.fixture_paths)
    if fixture_paths.get("RADAR_RSS_FIXTURE"):
        return
    raise RunContextError(
        "Historical live-shadow requires an explicit RSS fixture bound with "
        "--rss-fixture; live feeds cannot reconstruct point-in-time news for "
        f"effective date {context.effective_date} from runtime date {runtime_date}"
    )


def _checkpoint_path(context, checkpoint_path=None):
    return Path(
        checkpoint_path
        or context.artifact_root / context.run_id / "pipeline-checkpoint.json"
    ).expanduser().resolve()


def _pipeline_error_payload(error):
    payload = {
        "interrupted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "error_type": type(error).__name__,
        "error_message": str(error),
    }
    if isinstance(error, PipelineCommandError):
        payload["failed_command"] = error.command_key
        payload["return_code"] = error.return_code
    return payload


def record_pipeline_failure(context, checkpoint_path, error):
    """Persist a resumable interruption and a durable failed-run manifest."""
    resolved_checkpoint = _checkpoint_path(context, checkpoint_path)
    checkpoint = CheckpointStore(resolved_checkpoint, context)
    checkpoint_data = checkpoint.load()
    error_payload = _pipeline_error_payload(error)
    checkpoint.mark_interrupted(checkpoint_data, error_payload)
    manifest_path = context.artifact_root / context.run_id / "run-manifest.json"
    write_artifact_envelope(
        manifest_path,
        context,
        "pipeline-run-manifest",
        {
            "status": "failed",
            "resumable": True,
            "completed_steps": list(checkpoint_data["completed_steps"]),
            "error": error_payload,
        },
    )
    return manifest_path


def _run_pipeline_inner(context, checkpoint_path=None, popen_factory=subprocess.Popen):
    logger.info(
        "--- Starting daily run id=%s mode=%s effective_date=%s database=%s ---",
        context.run_id,
        context.mode.value,
        context.effective_date,
        context.database_path,
    )

    validate_live_shadow_rss_input(context)
    database_input = verify_live_shadow_database_identity(context)
    if database_input:
        identity_path = context.artifact_root / context.run_id / "database-input.json"
        write_artifact_envelope(
            identity_path,
            context,
            "pipeline-database-input",
            database_input,
        )
        logger.info("Verified live-shadow database identity: %s", identity_path)

    # --- P0.0 Backup DB before processing ---
    db_path = str(context.database_path)
    if os.path.exists(db_path):
        backup_dir = str(context.artifact_root / context.run_id / "backups")
        try:
            backup_path = DBManager(
                db_path=db_path, backup_dir=backup_dir
            ).backup(prefix="quant_system")
            logger.info(f"Successfully backed up DB to {backup_path}")
            os.environ["QUANT_RUN_BACKUP_COMPLETED"] = "1"
        except Exception as e:
            logger.error(f"Failed to backup DB: {e}")
            raise RuntimeError("Run-level database backup failed") from e

    # 1. Record market-calendar state.  Reporting is request-driven and must
    # continue even when every market is closed.  Settlement independently
    # defers each market unless its calendar and exact raw open are available.
    today = context.effective_date
    try:
        if context.mode is RunMode.OFFLINE:
            logger.info(
                "Offline mode uses the fixture effective date and does not query live calendars."
            )
        elif os.environ.get("FORCE_RUN") == "1":
            logger.info("FORCE_RUN=1 detected. Bypassing trading day check.")
        else:
            from core.market import AShareMarket, HKMarket, USMarket
            # Validate the explicit run identity date, not the wall-clock date
            # when this process happens to start.  Otherwise a Friday report
            # launched on Saturday is incorrectly discarded as a weekend run.
            is_a_trade = AShareMarket().is_trading_date(today)
            is_us_trade = USMarket().is_trading_date(today)
            is_hk_trade = HKMarket().is_trading_date(today)

            if not (is_a_trade or is_us_trade or is_hk_trade):
                logger.info(
                    "%s is closed across A/US/HK; continuing report generation "
                    "with market-aware settlement deferral.",
                    today,
                )

            logger.info(
                "Effective-date trading status (%s) -> A-Share: %s, US: %s, HK: %s",
                today,
                is_a_trade,
                is_us_trade,
                is_hk_trade,
            )

    except Exception as e:
        logger.warning(
            "Top-level market calendar probe failed (%s); continuing report "
            "generation. The settlement stage will defer every market whose "
            "calendar cannot be verified.",
            e,
        )

    if os.environ.get("FORCE_RUN") == "1":
        logger.info("Running global strategy pipeline (FORCED)...")
    else:
        logger.info(
            "Running requested global strategy and reporting pipeline for %s.",
            today,
        )

    checkpoint_file = _checkpoint_path(context, checkpoint_path)
    checkpoint = CheckpointStore(checkpoint_file, context)
    checkpoint_data = checkpoint.load()

    def run_cmd(cmd, cwd):
        command_key = shlex.join(cmd)
        if command_key in checkpoint_data["completed_steps"]:
            logger.info("Skipping checkpointed command: %s", command_key)
            return
        logger.info(f"Running: {command_key}")

        env = os.environ.copy()
        env["PROJECT_ROOT"] = PROJECT_DIR
        env["RADAR_ROOT"] = RADAR_DIR
        env["RADAR_REPORTS_DIR"] = os.environ.get(
            "RADAR_REPORTS_DIR", os.path.join(RADAR_DIR, "reports")
        )
        env.update(context.child_environment())

        process = popen_factory(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        for line in process.stdout:
            logger.info(line.strip())
        process.wait()

        if process.returncode != 0:
            logger.error(f"Command failed with exit code {process.returncode}")
            raise PipelineCommandError(command_key, process.returncode)

        # Record success
        checkpoint.mark_completed(command_key, checkpoint_data)
        injected_stage = os.environ.get("PIPELINE_TEST_FAIL_AFTER")
        if injected_stage and injected_stage in command_key:
            if context.mode is RunMode.PRODUCTION:
                raise RuntimeError("Failure injection is forbidden in production mode")
            logger.error("Injecting non-production test failure after %s", command_key)
            raise PipelineCommandError(command_key, 97)

    # Child stages inherit the interpreter that launched the coordinator.
    # This prevents an activated Python 3.11 environment from silently
    # falling back to an EOL system `python3` (3.9 on older macOS installs).
    radar_python = resolve_stage_python("RADAR_PYTHON")
    quant_python = resolve_stage_python("QUANT_PYTHON")

    logger.info("=== Phase 1: API Cross-Check Enhancement ===")
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "check_stock_apis.py")], PROJECT_DIR)

    logger.info("=== Phase 2: Pre-Run DB Integrity & Capital Balancing ===")
    run_cmd(
        quant_python
        + [
            os.path.join(SCRIPTS_DIR, "check_db_integrity.py"),
            "--database",
            str(context.database_path),
        ],
        PROJECT_DIR,
    )

    logger.info("=== Phase 2B: Settle Due Market-Aware Trade Intents ===")
    settlement_command = quant_python + [
            os.path.join(SCRIPTS_DIR, "execute_pending_intents.py"),
            "--database",
            str(context.database_path),
            "--session-date",
            context.effective_date.isoformat(),
        ]
    if context.mode is RunMode.PRODUCTION:
        settlement_command.append("--allow-production")
    run_cmd(settlement_command, PROJECT_DIR)

    logger.info("=== Phase 3: Radar News Fetching ===")
    run_cmd(radar_python + ["main.py"], RADAR_DIR)

    logger.info("=== Phase 4: Global Quant Screening ===")
    run_cmd(
        quant_python
        + [
            os.path.join(SCRIPTS_DIR, "fetch_universe.py"),
            "--project-dir",
            PROJECT_DIR,
        ],
        PROJECT_DIR,
    )
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "screen_hot_spot.py")], PROJECT_DIR)
    run_cmd(
        quant_python
        + [
            os.path.join(SCRIPTS_DIR, "screen_global_quant.py"),
            "--require-continuous-growth",
            "--output-file",
            os.path.join(PROJECT_DIR, "global_screen.json"),
        ],
        PROJECT_DIR,
    )

    logger.info("=== Phase 5: Per-Strategy NAV Refresh & Certification ===")
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "calc_nav.py")], PROJECT_DIR)

    logger.info("=== Phase 6: Post-Update Double-Entry Ledger Sanity Check ===")
    run_cmd(
        quant_python
        + [
            os.path.join(SCRIPTS_DIR, "check_ledger_sanity.py"),
            "--database",
            str(context.database_path),
            "--effective-date",
            context.effective_date.isoformat(),
        ],
        PROJECT_DIR,
    )

    logger.info("=== Phase 7: Draw Backtest PnL Curves ===")
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "plot_pnl.py")], PROJECT_DIR)

    logger.info("=== Phase 8: Reporting & Email ===")
    os.makedirs(os.path.join(PROJECT_DIR, "reports"), exist_ok=True)
    run_cmd(
        quant_python
        + [
            os.path.join(SCRIPTS_DIR, "generate_report.py"),
            os.path.join(PROJECT_DIR, "global_screen.json"),
            os.path.join(PROJECT_DIR, "reports", "screening_results.md"),
        ],
        PROJECT_DIR,
    )
    prepared_html = (
        context.artifact_root / context.run_id / "prepared-report.html"
    )
    prepared_audit_html = (
        context.artifact_root / context.run_id / "prepared-audit-report.html"
    )
    prepared_manifest = (
        context.artifact_root / context.run_id / "prepared-report.json"
    )
    run_cmd(
        quant_python
        + [
            os.path.join(SCRIPTS_DIR, "send_unified_email.py"),
            "--prepare-only",
            "--prepared-html",
            str(prepared_html),
            "--prepared-audit-html",
            str(prepared_audit_html),
            "--prepared-manifest",
            str(prepared_manifest),
            "--effective-date",
            context.effective_date.isoformat(),
        ],
        PROJECT_DIR,
    )
    run_cmd(
        quant_python
        + [
            os.path.join(SCRIPTS_DIR, "validate_report_html.py"),
            "--html",
            str(prepared_audit_html),
            "--database",
            str(context.database_path),
        ],
        PROJECT_DIR,
    )
    delivery_command = quant_python + [
        os.path.join(SCRIPTS_DIR, "send_unified_email.py"),
        "--prepared-manifest",
        str(prepared_manifest),
    ]
    if context.delivery_mode is DeliveryMode.LIVE:
        delivery_command.append("--confirm-live-delivery")
    run_cmd(delivery_command, PROJECT_DIR)

    manifest_path = context.artifact_root / context.run_id / "run-manifest.json"
    write_artifact_envelope(
        manifest_path,
        context,
        "pipeline-run-manifest",
        {
            "status": "completed",
            "completed_steps": list(checkpoint_data["completed_steps"]),
            "database_input": database_input,
        },
    )
    checkpoint.clear()
    logger.info("Cleared completed checkpoint; durable manifest: %s", manifest_path)

    logger.info("Daily global strategy run completed successfully.")
    return manifest_path


def run_pipeline(context, checkpoint_path=None, popen_factory=subprocess.Popen):
    """Run with scoped environment so one invocation cannot poison the next."""
    completed_manifest = (
        context.artifact_root / context.run_id / "run-manifest.json"
    )
    if completed_manifest.is_file():
        envelope = read_artifact_envelope(completed_manifest)
        context.assert_envelope_identity(envelope)
        if envelope["payload"].get("status") == "completed":
            logger.info(
                "Run %s is already completed; returning its durable manifest "
                "without repeating database writes or delivery.",
                context.run_id,
            )
            return completed_manifest

    scoped_values = context.child_environment()
    scoped_keys = set(scoped_values) | {"QUANT_RUN_BACKUP_COMPLETED"}
    previous_values = {key: os.environ.get(key) for key in scoped_keys}
    os.environ.update(scoped_values)
    os.environ.pop("QUANT_RUN_BACKUP_COMPLETED", None)
    try:
        with writer_fence(
            context.database_path,
            owner=f"daily-runner:{context.mode.value}:{context.run_id}",
            timeout=0.0,
        ):
            return _run_pipeline_inner(
                context,
                checkpoint_path=checkpoint_path,
                popen_factory=popen_factory,
            )
    except Exception as error:
        try:
            manifest_path = record_pipeline_failure(
                context, checkpoint_path, error
            )
            logger.error(
                "Persisted interrupted checkpoint and failure manifest: %s",
                manifest_path,
            )
        except Exception:
            logger.exception(
                "Could not persist pipeline failure metadata; preserving "
                "the original exception"
            )
        raise
    finally:
        for key, value in previous_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        context = create_run_context(args)
        run_pipeline(context, checkpoint_path=args.checkpoint)
    except PipelineCommandError as error:
        logger.error("Pipeline stage failed: %s", error)
        return error.return_code or 1
    except Exception as error:
        logger.error("Pipeline stopped: %s: %s", type(error).__name__, error, exc_info=True)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
