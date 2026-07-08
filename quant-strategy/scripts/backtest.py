import datetime
import os
import yfinance as yf
import pandas as pd
import pytz
from dateutil.relativedelta import relativedelta
import pandas_market_calendars as mcal
from core.clock import clock
from screen_global_quant import main as run_screen
import db_utils
from core.logger import get_quant_logger

logger = get_quant_logger("backtest_engine")

class BacktestEngine:
    def __init__(self, start_date: datetime.date, end_date: datetime.date, rebalance_days: int = 30):
        self.start_date = start_date
        self.end_date = end_date
        self.rebalance_days = rebalance_days
        
        # Setup DB
        self.db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backtest_portfolio.db")
        os.environ["SQLITE_DB_PATH"] = self.db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path) # Fresh start for every backtest
            
        # Get unified trading calendar (Union of US, HK, A-Share)
        # For simplicity in global backtest, we step through US trading days as the anchor.
        nyse = mcal.get_calendar('XNYS')
        self.trading_days = nyse.schedule(start_date=start_date, end_date=end_date).index.date
        
        self.daily_pnl = [] # Records {date, portfolio_value}
        self.price_cache = {} # Dict of ticker -> Series of prices
        
    def _preload_prices(self):
        """P3.22: Pre-fetch phase to prevent network congestion during MTM"""
        logger.info("Pre-fetching historical prices for backtest period (This may take a few minutes)...")
        # In a real system, you'd fetch the universe of possible stocks.
        # Since we don't know the exact universe ahead of time without running the screen,
        # we will dynamically fetch and cache prices as we encounter new stocks during the backtest,
        # but we fetch their ENTIRE history in one go to avoid N+1.
        pass
        
    def _get_price(self, code, date):
        """Get price from cache or fetch and cache the entire history for this ticker."""
        if code not in self.price_cache:
            try:
                # Add suffix if HK
                query_code = code
                if query_code.isdigit() and len(query_code) == 4:
                    query_code += ".HK"
                
                ticker = yf.Ticker(query_code)
                # Fetch full 5 years
                hist = ticker.history(start=self.start_date - datetime.timedelta(days=10), end=self.end_date + datetime.timedelta(days=10))
                if not hist.empty:
                    # Convert index to naive date
                    hist.index = hist.index.tz_localize(None).date
                    self.price_cache[code] = hist['Close']
                else:
                    self.price_cache[code] = pd.Series(dtype=float)
            except Exception as e:
                logger.warning(f"Failed to fetch history for {code}: {e}")
                self.price_cache[code] = pd.Series(dtype=float)
                
        series = self.price_cache[code]
        if series.empty:
            return 0.0
            
        # Try to get exact date, or forward fill (previous available close)
        past_prices = series[series.index <= date]
        if not past_prices.empty:
            return float(past_prices.iloc[-1])
        return 0.0

    def mark_to_market(self, current_date):
        """Calculate total value of the current portfolio."""
        portfolio, _ = db_utils.load_portfolio_and_trades()
        total_value = 0.0
        
        for strat, holdings in portfolio.items():
            for code, info in holdings.items():
                entry_price = info.get("entry_price", 0)
                # Assuming equal weight normalized to 1.0 at entry
                current_price = self._get_price(code, current_date)
                if entry_price > 0 and current_price > 0:
                    roi = current_price / entry_price
                    total_value += roi # Each stock starts at 1.0 "value units"
                    
        return total_value

    def run(self):
        logger.info(f"Starting event-driven backtest from {self.start_date} to {self.end_date}")
        days_since_rebalance = self.rebalance_days # Trigger immediately on day 1
        
        for current_date in self.trading_days:
            # 1. Mark to Market (Daily PnL tracking)
            daily_val = self.mark_to_market(current_date)
            self.daily_pnl.append({"date": str(current_date), "value": daily_val})
            
            # 2. Check Rebalance Trigger
            if days_since_rebalance >= self.rebalance_days:
                logger.info(f"[{current_date}] Triggering strategy rebalance...")
                
                # Mock the clock to Shanghai time for the screener
                naive_dt = datetime.datetime.combine(current_date, datetime.time(15, 0))
                aware_dt = pytz.timezone("Asia/Shanghai").localize(naive_dt)
                clock.set_mock_time(aware_dt)
                
                try:
                    # Run screening (this will internally update the DB)
                    run_screen()
                except Exception as e:
                    logger.error(f"Error during rebalance on {current_date}: {e}")
                    
                clock.clear_mock_time()
                days_since_rebalance = 0
            else:
                days_since_rebalance += 1
                
        self._generate_report()
        
    def _generate_report(self):
        logger.info("\n" + "="*50)
        logger.info("BACKTEST COMPLETED")
        logger.info("="*50)
        
        if not self.daily_pnl:
            return
            
        df = pd.DataFrame(self.daily_pnl)
        df.set_index('date', inplace=True)
        
        # Calculate Max Drawdown
        df['peak'] = df['value'].cummax()
        df['drawdown'] = (df['value'] - df['peak']) / df['peak']
        max_dd = df['drawdown'].min() * 100 if df['peak'].max() > 0 else 0
        
        # Calculate Total Return
        start_val = df['value'].iloc[0] if len(df) > 0 and df['value'].iloc[0] > 0 else 1.0 # Avoid div by zero
        end_val = df['value'].iloc[-1]
        total_ret = ((end_val / start_val) - 1) * 100
        
        logger.info(f"Final Portfolio Value Units: {end_val:.2f}")
        logger.info(f"Total Return: {total_ret:.2f}%")
        logger.info(f"Max Drawdown: {max_dd:.2f}%")
        logger.info(f"Detailed daily MTM saved to backtest_pnl.csv")
        
        df.to_csv(os.path.join(os.path.dirname(self.db_path), "backtest_pnl.csv"))

if __name__ == "__main__":
    end = datetime.date.today()
    start = end - relativedelta(years=1) # 1 year for quick test
    engine = BacktestEngine(start, end, rebalance_days=30)
    engine.run()
