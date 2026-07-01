import datetime
import os
from dateutil.relativedelta import relativedelta
from core.clock import clock
from screen_global_quant import main as run_screen

def run_backtest(start_date: datetime.date, end_date: datetime.date, step_days: int = 30):
    current_date = start_date
    
    # Use a separate DB for backtesting to avoid polluting live trades
    os.environ["SQLITE_DB_PATH"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backtest_portfolio.db")
    
    while current_date <= end_date:
        print(f"\n{'='*50}\nRunning Backtest for date: {current_date}\n{'='*50}")
        # Inject mock time (15:00 to simulate post-market run)
        clock.set_mock_time(datetime.datetime.combine(current_date, datetime.time(15, 0)))
        
        try:
            run_screen()
        except Exception as e:
            print(f"Error on {current_date}: {e}")
            import traceback
            traceback.print_exc()
            
        current_date += datetime.timedelta(days=step_days)
        
    print("\nBacktest completed! DB saved to backtest_portfolio.db")
    clock.clear_mock_time()

if __name__ == "__main__":
    end = datetime.date.today()
    # Support past 5 years
    start = end - relativedelta(years=5)
    run_backtest(start, end, step_days=30)
