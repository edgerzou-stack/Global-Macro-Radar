import os
import sys
import datetime
import subprocess
import logging
from core.clock import clock
from core.data_gateway import DataGateway
import db_reset
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def run_cmd(cmd, cwd, env):
    logger.info(f"Running: {cmd}")
    process = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    for line in process.stdout:
        print(line.strip())
    process.wait()
    if process.returncode != 0:
        logger.error(f"Command failed with exit code {process.returncode}")
        sys.exit(process.returncode)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Global Macro Quant Backtest Engine")
    parser.add_argument("--days", type=int, default=365, help="Number of calendar days to backtest")
    args = parser.parse_args()

    # 1. Reset Database
    logger.info("Resetting Database for fresh backtest...")
    db_reset.reset_db()

    # 2. Get Trading Days
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=args.days)
    logger.info(f"Backtesting from {start_date} to {today}")

    import akshare as ak
    try:
        trade_dates = ak.tool_trade_date_hist_sina()
        trade_dates_list = pd.to_datetime(trade_dates['trade_date']).dt.date.tolist()
        
        # Filter for dates within our backtest range
        valid_dates = [d for d in trade_dates_list if start_date <= d <= today]
    except Exception as e:
        logger.error(f"Failed to fetch trading dates: {e}")
        # Fallback to simple weekdays
        valid_dates = []
        curr = start_date
        while curr <= today:
            if curr.weekday() < 5:
                valid_dates.append(curr)
            curr += datetime.timedelta(days=1)

    # 3. Preparation: Fetch Universe ONCE for the backtest
    project_root = os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    quant_python = os.environ.get("QUANT_PYTHON", "python3")
    
    logger.info("Fetching latest universe components (only runs once)...")
    run_cmd(f"{quant_python} scripts/fetch_universe.py", project_root, os.environ.copy())

    # 4. Execution Loop
    for dt in valid_dates:
        dt_str = dt.strftime("%Y-%m-%d")
        logger.info(f"========== MOCK DATE: {dt_str} ==========")
        
        env = os.environ.copy()
        env["PROJECT_ROOT"] = project_root
        env["MOCK_DATE"] = dt_str
        
        # We skip hot_spot in backtest to avoid LLM quota drain and look-ahead bias (scraping today's news for historical dates).
        cmds = [
            f"{quant_python} scripts/screen_global_quant.py --require-continuous-growth --disable-llm --output-file {project_root}/global_screen.json",
            f"{quant_python} scripts/calc_nav.py"
        ]
        
        for cmd in cmds:
            run_cmd(cmd, project_root, env)

    # 4. Generate final plots and reports
    logger.info("========== BACKTEST COMPLETE. GENERATING REPORTS ==========")
    final_env = os.environ.copy()
    final_env["PROJECT_ROOT"] = project_root
    if "MOCK_DATE" in final_env:
        del final_env["MOCK_DATE"]
        
    cmds = [
        f"{quant_python} scripts/plot_pnl.py",
        f"{quant_python} scripts/generate_report.py {project_root}/global_screen.json {project_root}/reports/backtest_results.md"
    ]
    for cmd in cmds:
        run_cmd(cmd, project_root, final_env)
        
    logger.info("Backtest results successfully generated!")

if __name__ == "__main__":
    main()
