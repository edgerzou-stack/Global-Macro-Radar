import akshare as ak
import pandas as pd
import datetime
import subprocess
import sys
import os

def main():
    print(f"--- Starting daily run at {datetime.datetime.now()} ---")
    
    # 1. Check if today is a trading day
    today = datetime.date.today()
    try:
        trade_dates = ak.tool_trade_date_hist_sina()
        trade_dates_list = pd.to_datetime(trade_dates['trade_date']).dt.date.tolist()
        if today not in trade_dates_list:
            print(f"{today} is not a trading day. Exiting.")
            sys.exit(0)
    except Exception as e:
        print(f"Failed to fetch trading calendar: {e}")
        # Fallback to weekday check
        if today.weekday() >= 5:
            print(f"{today} is a weekend. Exiting.")
            sys.exit(0)
        else:
            print(f"Assuming {today} is a trading day as fallback.")
            
    print(f"{today} is a trading day. Running strategy pipeline...")
    
    # Paths
    project_dir = "/Users/zouzhengting/Workplace/a_share_factor_flow"
    scripts_dir = "/Users/zouzhengting/.codex/skills/a-share-factor-screen/scripts"
    
    # Commands
    cmds = [
        f"python3 {scripts_dir}/screen_a_share.py --require-continuous-growth --output-file {project_dir}/dual_screen.json",
        f"python3 {scripts_dir}/plot_pnl.py",
        f"python3 {scripts_dir}/generate_report.py {project_dir}/dual_screen.json {project_dir}/screening_results.md"
    ]
    
    # Ensure reports dir exists
    os.makedirs(os.path.join(project_dir, "reports"), exist_ok=True)
    
    for cmd in cmds:
        print(f"Running: {cmd}")
        result = subprocess.run(cmd, shell=True, cwd=project_dir)
        if result.returncode != 0:
            print(f"Command failed with exit code {result.returncode}")
            sys.exit(result.returncode)
            
    print("Daily strategy run completed successfully.\n")

if __name__ == "__main__":
    main()
