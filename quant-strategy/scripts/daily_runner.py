import akshare as ak
import pandas as pd
import datetime
import subprocess
import sys
import os
import logging
from core.clock import clock

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
        import shutil
        backup_dir = os.path.join(PROJECT_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = clock.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"quant_system_{timestamp}.db")
        try:
            shutil.copy2(db_path, backup_path)
            logger.info(f"Successfully backed up DB to {backup_path}")
        except Exception as e:
            logger.error(f"Failed to backup DB: {e}")
            sys.exit(1) # Abort if backup fails
            
    # 1. Check if today is a trading day
    today = clock.today().date()
    try:
        if os.environ.get("FORCE_RUN") == "1":
            logger.info("FORCE_RUN=1 detected. Bypassing trading day check.")
        else:
            # 修改 P0.4: 分别检查 A股, 美股, 港股 的节假日日历
            import warnings
            
            is_a_trade = False
            is_us_trade = False
            is_hk_trade = False
            
            # A-Share Check
            try:
                trade_dates = ak.tool_trade_date_hist_sina()
                trade_dates_list = pd.to_datetime(trade_dates['trade_date']).dt.date.tolist()
                is_a_trade = today in trade_dates_list
            except Exception as e:
                logger.warning(f"Failed to fetch A-share trading calendar via akshare: {e}")
                is_a_trade = today.weekday() < 5
                
            # US/HK Check via pandas_market_calendars (or fallback)
            try:
                import pandas_market_calendars as mcal
                nyse = mcal.get_calendar('NYSE')
                hkex = mcal.get_calendar('HKEX')
                
                # Check if today is in the schedule
                today_str = today.strftime('%Y-%m-%d')
                us_sched = nyse.schedule(start_date=today_str, end_date=today_str)
                is_us_trade = not us_sched.empty
                
                hk_sched = hkex.schedule(start_date=today_str, end_date=today_str)
                is_hk_trade = not hk_sched.empty
            except ImportError:
                logger.warning("pandas_market_calendars not installed. Using simple weekday check for US/HK as fallback.")
                is_us_trade = today.weekday() < 5
                is_hk_trade = today.weekday() < 5
            except Exception as e:
                logger.warning(f"Error checking US/HK calendar: {e}")
                is_us_trade = today.weekday() < 5
                is_hk_trade = today.weekday() < 5

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
            
    logger.info(f"{today} is a trading day. Running global strategy pipeline...")
    checkpoint_file = os.path.join(PROJECT_DIR, ".daily_checkpoint.json")
    checkpoint_data = {"date": "", "completed_steps": []}
    
    if os.path.exists(checkpoint_file):
        try:
            import json
            with open(checkpoint_file, "r") as f:
                checkpoint_data = json.load(f)
        except Exception:
            pass
            
    if checkpoint_data.get("date") != str(today):
        checkpoint_data = {"date": str(today), "completed_steps": []}
        
    def run_cmd(cmd, cwd):
        if cmd in checkpoint_data["completed_steps"]:
            logger.info(f"Skipping already completed step: {cmd}")
            return
            
        logger.info(f"Running: {cmd}")
        
        env = os.environ.copy()
        env["PROJECT_ROOT"] = PROJECT_DIR
        env["RADAR_ROOT"] = RADAR_DIR
        env["RADAR_REPORTS_DIR"] = os.path.join(RADAR_DIR, "reports")
        
        process = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        for line in process.stdout:
            logger.info(line.strip())
        process.wait()
            
        if process.returncode != 0:
            logger.error(f"Command failed with exit code {process.returncode}")
            sys.exit(process.returncode)
            
        # Record success
        checkpoint_data["completed_steps"].append(cmd)
        import json
        with open(checkpoint_file, "w") as f:
            json.dump(checkpoint_data, f)

    radar_python = os.environ.get("RADAR_PYTHON", "python3")
    run_cmd(f"{radar_python} main.py", RADAR_DIR)
        
    # Commands for screening
    quant_python = os.environ.get("QUANT_PYTHON", "python3")
    cmds = [
        f"{quant_python} {SCRIPTS_DIR}/fetch_universe.py",
        f"{quant_python} {SCRIPTS_DIR}/screen_hot_spot.py",
        f"{quant_python} {SCRIPTS_DIR}/screen_global_quant.py --require-continuous-growth --output-file {PROJECT_DIR}/global_screen.json",
        f"{quant_python} {SCRIPTS_DIR}/calc_nav.py",
        f"{quant_python} {SCRIPTS_DIR}/plot_pnl.py",
        f"{quant_python} {SCRIPTS_DIR}/generate_report.py {PROJECT_DIR}/global_screen.json {PROJECT_DIR}/reports/screening_results.md",
        f"{quant_python} {SCRIPTS_DIR}/send_unified_email.py"
    ]
    
    os.makedirs(os.path.join(PROJECT_DIR, "reports"), exist_ok=True)
    
    for cmd in cmds:
        run_cmd(cmd, PROJECT_DIR)
            
    logger.info("Daily global strategy run completed successfully.")

if __name__ == "__main__":
    main()
