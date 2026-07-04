import akshare as ak
import pandas as pd
import datetime
import subprocess
import sys
import os

# === Path Configuration ===
HOME = os.path.expanduser("~")
PROJECT_DIR = os.path.join(HOME, "Workplace", "Global-Macro-Radar-Core", "a_share_factor_flow") # You may want to migrate this to Global-Macro-Radar later
RADAR_DIR = os.path.join(HOME, "Workplace", "Global-Macro-Radar-Core", "industry-radar_archived")
SCRIPTS_DIR = os.path.join(HOME, "Workplace", "Global-Macro-Radar", "quant-strategy", "scripts")

def main():
    print(f"--- Starting daily run at {datetime.datetime.now()} ---")
    
    # 1. Check if today is a trading day
    today = datetime.date.today()
    try:
        if os.environ.get("FORCE_RUN") == "1":
            print("FORCE_RUN=1 detected. Bypassing trading day check.")
        else:
            # 修改 P0.4: 分别检查 A股, 美股, 港股 的节假日日历
            import ecal
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
                print(f"Failed to fetch A-share trading calendar via akshare: {e}")
                is_a_trade = today.weekday() < 5
                
            # US/HK Check via pandas_market_calendars (or fallback)
            try:
                import pandas_market_calendars as mcal
                nyse = mcal.get_calendar('NYSE')
                hkex = mcal.get_calendar('HKEX')
                
                # Check if today is in the schedule
                us_sched = nyse.schedule(start_date=today, end_date=today)
                is_us_trade = not us_sched.empty
                
                hk_sched = hkex.schedule(start_date=today, end_date=today)
                is_hk_trade = not hk_sched.empty
            except ImportError:
                print("pandas_market_calendars not installed. Using simple weekday check for US/HK as fallback.")
                is_us_trade = today.weekday() < 5
                is_hk_trade = today.weekday() < 5
            except Exception as e:
                print(f"Error checking US/HK calendar: {e}")
                is_us_trade = today.weekday() < 5
                is_hk_trade = today.weekday() < 5

            if not (is_a_trade or is_us_trade or is_hk_trade):
                print(f"{today} is a holiday/weekend across all monitored markets (A/US/HK). Exiting.")
                sys.exit(0)
            
            print(f"Trading day status -> A-Share: {is_a_trade}, US: {is_us_trade}, HK: {is_hk_trade}")
            
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
            
        print(f"Running: {cmd}", flush=True)
        log_file = os.path.join(PROJECT_DIR, "reports", "daily_run.log")
        with open(log_file, "a") as lf:
            lf.write(f"\n[{datetime.datetime.now()}] Running: {cmd}\n")
            lf.flush()
            process = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in process.stdout:
                print(line, end='', flush=True)
                lf.write(line)
                lf.flush()
            process.wait()
            
        if process.returncode != 0:
            print(f"Command failed with exit code {process.returncode}", flush=True)
            sys.exit(process.returncode)
            
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
        f"/Users/zouzhengting/Workplace/Global-Macro-Radar-Core/industry-radar_archived/venv/bin/python {SCRIPTS_DIR}/send_unified_email.py"
    ]
    
    os.makedirs(os.path.join(PROJECT_DIR, "reports"), exist_ok=True)
    
    for cmd in cmds:
        run_cmd(cmd, PROJECT_DIR)
            
    print("Daily global strategy run completed successfully.\n")

if __name__ == "__main__":
    main()
