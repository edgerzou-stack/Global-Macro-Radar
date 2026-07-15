import akshare as ak
import pandas as pd
import datetime
import subprocess
import sys
import os
import logging
import json
import shlex
import tempfile
from core.clock import clock
from core.db_manager import DBManager

# === Path Configuration ===
HOME = os.path.expanduser("~")
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

def main():
    logger.info(f"--- Starting daily run at {datetime.datetime.now()} ---")

    # --- P0.0 Backup DB before processing ---
    db_path = os.path.join(PROJECT_DIR, "quant_system.db")
    if os.path.exists(db_path):
        backup_dir = os.path.join(PROJECT_DIR, "backups")
        try:
            backup_path = DBManager(
                db_path=db_path, backup_dir=backup_dir
            ).backup(prefix="quant_system")
            logger.info(f"Successfully backed up DB to {backup_path}")
            os.environ["QUANT_RUN_BACKUP_COMPLETED"] = "1"
        except Exception as e:
            logger.error(f"Failed to backup DB: {e}")
            sys.exit(1) # Abort if backup fails

    # 1. Check if today is a trading day
    today = clock.today()
    try:
        if os.environ.get("FORCE_RUN") == "1":
            logger.info("FORCE_RUN=1 detected. Bypassing trading day check.")
        else:
            from core.market import AShareMarket, HKMarket, USMarket
            is_a_trade = AShareMarket().is_trading_day()
            is_us_trade = USMarket().is_trading_day()
            is_hk_trade = HKMarket().is_trading_day()

            if not (is_a_trade or is_us_trade or is_hk_trade):
                logger.info(f"{today} is a holiday/weekend across all monitored markets (A/US/HK). Exiting.")
                sys.exit(0)

            logger.info(f"Trading day status -> A-Share: {is_a_trade}, US: {is_us_trade}, HK: {is_hk_trade}")

    except Exception as e:
        logger.error(f"Failed to fetch trading calendar: {e}")
        # Fallback to weekday check
        if today.weekday() >= 5:
            logger.info(f"{today} is a weekend. Exiting.")
            sys.exit(0)
        else:
            logger.info(f"Assuming {today} is a trading day as fallback.")

    if os.environ.get("FORCE_RUN") == "1":
        logger.info("Running global strategy pipeline (FORCED)...")
    else:
        logger.info(f"{today} is a trading day. Running global strategy pipeline...")

    checkpoint_file = os.path.join(PROJECT_DIR, ".daily_checkpoint.json")
    checkpoint_data = {"date": "", "completed_steps": []}

    if os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file, "r") as f:
                checkpoint_data = json.load(f)
        except Exception as e:
            import logging
            logging.error(f"Failed to load checkpoint file {checkpoint_file}: {e}", exc_info=True)

    if checkpoint_data.get("date") != str(today):
        checkpoint_data = {"date": str(today), "completed_steps": []}

    def run_cmd(cmd, cwd):
        command_key = shlex.join(cmd)
        if command_key in checkpoint_data["completed_steps"]:
            logger.info(f"Skipping already completed step: {command_key}")
            return

        logger.info(f"Running: {command_key}")

        env = os.environ.copy()
        env["PROJECT_ROOT"] = PROJECT_DIR
        env["RADAR_ROOT"] = RADAR_DIR
        env["RADAR_REPORTS_DIR"] = os.path.join(RADAR_DIR, "reports")

        process = subprocess.Popen(
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
            sys.exit(process.returncode)

        # Record success
        checkpoint_data["completed_steps"].append(command_key)
        checkpoint_dir = os.path.dirname(checkpoint_file)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=checkpoint_dir,
            prefix=".daily_checkpoint.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_checkpoint = handle.name
            json.dump(checkpoint_data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_checkpoint, checkpoint_file)

    radar_python = shlex.split(os.environ.get("RADAR_PYTHON", "python3"))
    quant_python = shlex.split(os.environ.get("QUANT_PYTHON", "python3"))

    logger.info("=== Phase 1: API Cross-Check Enhancement ===")
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "check_stock_apis.py")], PROJECT_DIR)

    logger.info("=== Phase 2: Pre-Market DB Integrity & Capital Balancing ===")
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "check_db_integrity.py")], PROJECT_DIR)

    logger.info("=== Phase 3: Radar News Fetching ===")
    run_cmd(radar_python + ["main.py"], RADAR_DIR)

    logger.info("=== Phase 4: Global Quant Screening ===")
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "fetch_universe.py")], PROJECT_DIR)
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
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "check_ledger_sanity.py")], PROJECT_DIR)

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
    run_cmd(quant_python + [os.path.join(SCRIPTS_DIR, "send_unified_email.py")], PROJECT_DIR)

    if os.path.exists(checkpoint_file):
        os.remove(checkpoint_file)
        logger.info("Cleared daily checkpoint file for future intraday runs.")

    logger.info("Daily global strategy run completed successfully.")

if __name__ == "__main__":
    main()
