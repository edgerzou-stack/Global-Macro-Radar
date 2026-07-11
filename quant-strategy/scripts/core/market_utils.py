import pandas_market_calendars as mcal
import datetime
import pytz
import pandas as pd

_cal_cache = {}

def get_calendar(market: str):
    if market not in _cal_cache:
        if market == 'US':
            _cal_cache[market] = mcal.get_calendar('NYSE')
        elif market == 'HK':
            _cal_cache[market] = mcal.get_calendar('HKEX')
        elif market == 'A':
            _cal_cache[market] = mcal.get_calendar('SSE')
        else:
            _cal_cache[market] = mcal.get_calendar('NYSE')
    return _cal_cache[market]

def get_effective_today(strat: str, now_local: datetime.datetime) -> str:
    """
    Returns the effective trading session date string (YYYY-MM-DD).
    If it's before the market open or a non-trading day, returns the previous trading day.
    If the market is open or already closed for the day, returns today.
    """
    market = 'US'
    if '_a_' in strat:
        market = 'A'
    elif '_hk_' in strat:
        market = 'HK'
        
    cal = get_calendar(market)
    
    # We query the schedule for the last 30 days to current day to find trading days (covers long holidays)
    start_date = (now_local - datetime.timedelta(days=30)).date()
    end_date = now_local.date()
    
    try:
        schedule = cal.schedule(start_date=start_date, end_date=end_date)
        
        # Ensure now_local is timezone-aware and matches the calendar's timezone
        now_utc = now_local.astimezone(pytz.utc)
        
        # Check if today is a trading day
        today_ts = pd.Timestamp(now_local.date())
        if today_ts in schedule.index:
            # If multiple rows return, take the first one
            row = schedule.loc[today_ts]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            market_open_utc = row['market_open']
            if now_utc >= market_open_utc:
                # We are past the open, today is the effective day
                return today_ts.strftime('%Y-%m-%d')
                
        # If today is not a trading day, or we are before the open, we find the last trading day
        valid_days = schedule[schedule['market_open'] <= pd.Timestamp(now_utc)]
        
        if not valid_days.empty:
            # Return the most recent trading day's date string
            return valid_days.index[-1].strftime('%Y-%m-%d')
        else:
            return start_date.strftime('%Y-%m-%d')
    except Exception as e:
        import logging
        logging.warning(f"mcal schedule failed: {e}. Falling back to weekday logic.")
        # Fallback to simple weekday logic
        current = now_local
        # If today is a weekend, find the last Friday
        while current.weekday() >= 5:
            current -= datetime.timedelta(days=1)
        # We don't have accurate time, just assume today if it's a weekday, or the last Friday
        return current.strftime('%Y-%m-%d')
