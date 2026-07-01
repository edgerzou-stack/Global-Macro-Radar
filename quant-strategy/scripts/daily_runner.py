import akshare as ak
import pandas as pd
import datetime
import subprocess
import sys
import os

# === Path Configuration ===
HOME = os.path.expanduser("~")
PROJECT_DIR = os.path.join(HOME, "Workplace", "a_share_factor_flow") # You may want to migrate this to Global-Macro-Radar later
RADAR_DIR = os.path.join(HOME, "Workplace", "Global-Macro-Radar", "industry-radar")
SCRIPTS_DIR = os.path.join(HOME, "Workplace", "Global-Macro-Radar", "quant-strategy", "scripts")

def main():
    print(f"--- Starting daily run at {datetime.datetime.now()} ---")
    
    # 1. Check if today is a trading day
    today = datetime.date.today()
    try:
        if os.environ.get("FORCE_RUN") == "1":
            print("FORCE_RUN=1 detected. Bypassing trading day check.")
        else:
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
            
    print(f"{today} is a trading day. Running global strategy pipeline...")
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
            print(f"Skipping already completed step: {cmd}")
            return
            
        print(f"Running: {cmd}")
        log_file = os.path.join(PROJECT_DIR, "reports", "daily_run.log")
        with open(log_file, "a") as lf:
            lf.write(f"\n[{datetime.datetime.now()}] Running: {cmd}\n")
            result = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lf.write(result.stdout)
            print(result.stdout)
            
        if result.returncode != 0:
            print(f"Command failed with exit code {result.returncode}")
            sys.exit(result.returncode)
            
        # Record success
        checkpoint_data["completed_steps"].append(cmd)
        import json
        with open(checkpoint_file, "w") as f:
            json.dump(checkpoint_data, f)

    run_cmd("venv/bin/python main.py", RADAR_DIR)
        
    # Commands for screening
    cmds = [
        f"python3 {SCRIPTS_DIR}/fetch_universe.py",
        f"python3 {SCRIPTS_DIR}/screen_hot_spot.py",
        f"python3 {SCRIPTS_DIR}/screen_global_quant.py --require-continuous-growth --output-file {PROJECT_DIR}/global_screen.json",
        f"python3 {SCRIPTS_DIR}/plot_pnl.py",
        f"python3 {SCRIPTS_DIR}/generate_report.py {PROJECT_DIR}/global_screen.json {PROJECT_DIR}/reports/screening_results.md",
        f"/Users/zouzhengting/Workplace/industry-radar/venv/bin/python {SCRIPTS_DIR}/send_unified_email.py"
    ]
    
    os.makedirs(os.path.join(PROJECT_DIR, "reports"), exist_ok=True)
    
    for cmd in cmds:
        run_cmd(cmd, PROJECT_DIR)
            
    print("Daily global strategy run completed successfully.\n")

if __name__ == "__main__":
    main()
