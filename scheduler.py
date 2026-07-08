import schedule
import time
import subprocess
import logging
import sys
import argparse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [Scheduler] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def run_radar_pipeline():
    logger.info("=====================================================")
    logger.info("Triggering Unified Global Macro Radar Pipeline...")
    logger.info("=====================================================")
    try:
        # Run the daily_runner.py script using subprocess
        # We stream the output to stdout so it shows up in Docker logs
        process = subprocess.Popen(
            ["python3", "quant-strategy/scripts/daily_runner.py"],
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True
        )
        process.wait()
        
        if process.returncode == 0:
            logger.info("Pipeline executed successfully.")
        else:
            logger.error(f"Pipeline failed with return code {process.returncode}")
    except Exception as e:
        logger.exception(f"Exception occurred while running pipeline: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Global Macro Radar Scheduler")
    parser.add_argument("--run-now", action="store_true", help="Run the pipeline immediately and exit")
    args = parser.parse_args()

    if args.run_now:
        logger.info("Manual execution requested (--run-now).")
        run_radar_pipeline()
        sys.exit(0)

    # Schedule the job twice a day: 08:00 and 20:00
    logger.info("Initializing Global Macro Radar Daemon...")
    
    schedule.every().day.at("08:00").do(run_radar_pipeline)
    schedule.every().day.at("20:00").do(run_radar_pipeline)
    
    logger.info("Scheduled to run daily at 08:00 and 20:00.")
    logger.info("Waiting for next execution...")
    
    # Infinite loop to keep the daemon running
    while True:
        schedule.run_pending()
        time.sleep(60) # Wake up every minute to check if a job is due
