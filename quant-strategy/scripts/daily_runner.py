import argparse
import subprocess
import sys
import os
import logging
import shlex
from pathlib import Path
from core.clock import clock
from core.db_manager import DBManager
from core.run_context import (
    CheckpointStore,
    DeliveryMode,
    FixtureBundle,
    RunContext,
    RunContextError,
    RunMode,
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
        import db_utils

        canonical_production = str(Path(db_utils.get_production_db_path()).resolve())
        if database_path != canonical_production:
            raise RunContextError(
                "Production mode must target the canonical production database: "
                f"{canonical_production}"
            )
    fixture_root = getattr(args, "fixture_root", None)
    if args.mode == RunMode.OFFLINE.value and not fixture_root:
        raise RunContextError("Offline mode requires an explicit --fixture-root")
    fixture_bundle = FixtureBundle.from_root(fixture_root) if fixture_root else None
    configuration = {
        "project_dir": PROJECT_DIR,
        "radar_dir": RADAR_DIR,
        "radar_python": os.environ.get("RADAR_PYTHON", "python3"),
        "quant_python": os.environ.get("QUANT_PYTHON", "python3"),
        "mode": args.mode,
        "database_path": database_path,
        "delivery_mode": delivery_mode.value,
        "fixtures": fixture_bundle.manifest if fixture_bundle else None,
    }
    return RunContext.create(
        mode=args.mode,
        database_path=database_path,
        effective_date=effective_date,
        configuration=configuration,
        artifact_root=args.artifact_root,
        run_id=run_id,
        delivery_mode=delivery_mode,
        fixture_paths=fixture_bundle.environment if fixture_bundle else None,
    )


def _run_pipeline_inner(context, checkpoint_path=None, popen_factory=subprocess.Popen):
    logger.info(
        "--- Starting daily run id=%s mode=%s effective_date=%s database=%s ---",
        context.run_id,
        context.mode.value,
        context.effective_date,
        context.database_path,
    )

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

    # 1. Check if today is a trading day
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
            is_a_trade = AShareMarket().is_trading_day()
            is_us_trade = USMarket().is_trading_day()
            is_hk_trade = HKMarket().is_trading_day()

            if not (is_a_trade or is_us_trade or is_hk_trade):
                logger.info(f"{today} is a holiday/weekend across all monitored markets (A/US/HK). Exiting.")
                return None

            logger.info(f"Trading day status -> A-Share: {is_a_trade}, US: {is_us_trade}, HK: {is_hk_trade}")

    except Exception as e:
        logger.error(f"Failed to fetch trading calendar: {e}")
        if context.mode is RunMode.PRODUCTION:
            raise RuntimeError(
                "Production trading calendar lookup failed; refusing weekday fallback"
            ) from e
        # Fallback to weekday check
        if today.weekday() >= 5:
            logger.info(f"{today} is a weekend. Exiting.")
            return None
        else:
            logger.info(f"Assuming {today} is a trading day as fallback.")

    if os.environ.get("FORCE_RUN") == "1":
        logger.info("Running global strategy pipeline (FORCED)...")
    else:
        logger.info(f"{today} is a trading day. Running global strategy pipeline...")

    checkpoint_file = Path(
        checkpoint_path
        or context.artifact_root / context.run_id / "pipeline-checkpoint.json"
    )
    checkpoint = CheckpointStore(checkpoint_file, context)
    checkpoint_data = checkpoint.load()

    def run_cmd(cmd, cwd):
        command_key = shlex.join(cmd)
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

    radar_python = shlex.split(os.environ.get("RADAR_PYTHON", "python3"))
    quant_python = shlex.split(os.environ.get("QUANT_PYTHON", "python3"))

    logger.info("=== Phase 1: API Cross-Check Enhancement ===")
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "check_stock_apis.py")], PROJECT_DIR)

    logger.info("=== Phase 2: Pre-Market DB Integrity & Capital Balancing ===")
    run_cmd(
        quant_python
        + [
            os.path.join(SCRIPTS_DIR, "check_db_integrity.py"),
            "--database",
            str(context.database_path),
        ],
        PROJECT_DIR,
    )

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

    logger.info("=== Phase 5: Ledger Settlement & NAV ===")
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "calc_nav.py")], PROJECT_DIR)

    logger.info("=== Phase 6: Post-Market Double-Entry Ledger Sanity Check ===")
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
    delivery_command = quant_python + [
        os.path.join(SCRIPTS_DIR, "send_unified_email.py")
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
        },
    )
    checkpoint.clear()
    logger.info("Cleared completed checkpoint; durable manifest: %s", manifest_path)

    logger.info("Daily global strategy run completed successfully.")
    return manifest_path


def run_pipeline(context, checkpoint_path=None, popen_factory=subprocess.Popen):
    """Run with scoped environment so one invocation cannot poison the next."""
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
