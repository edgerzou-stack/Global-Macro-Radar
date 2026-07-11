import os
import sqlite3
import pandas as pd
import datetime
import yfinance as yf
import akshare as ak
import logging
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

class DataIntegrityError(Exception):
    """Raised when fetched data fails integrity checks (e.g., price <= 0)."""
    pass

class DataGateway:
    def __init__(self, db_path=None):
        if db_path is None:
            # Default to scripts/.cache/market_data_cache.db
            scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cache_dir = os.path.join(scripts_dir, ".cache")
            os.makedirs(cache_dir, exist_ok=True)
            db_path = os.path.join(cache_dir, "market_data_cache.db")
        
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS daily_prices (
                    symbol TEXT,
                    date TEXT,
                    open REAL,
                    close REAL,
                    high REAL,
                    low REAL,
                    volume REAL,
                    adjust TEXT,
                    PRIMARY KEY (symbol, date, adjust)
                )
            """)
            conn.commit()

    def _to_yf_symbol(self, symbol: str) -> str:
        """Converts internal symbol (like 600519, 000001) to yfinance symbol."""
        # Check if already a yfinance symbol (contains dot)
        if '.' in symbol and symbol.upper().endswith(('HK', 'US', 'SS', 'SZ', 'BJ')):
            return symbol
            
        symbol_str = str(symbol)
        
        # A-share detection based on length and prefix
        if len(symbol_str) == 6 and symbol_str.isdigit():
            if symbol_str.startswith('6'):
                return f"{symbol_str}.SS"
            elif symbol_str.startswith(('8', '4', '9')): # Beijing exchange / NEEQ
                return f"{symbol_str}.BJ"
            else:
                return f"{symbol_str}.SZ"
                
        # Assume Hong Kong if not specified but part of HK strategy, or US.
        # But this function only translates raw symbols.
        return symbol

    def _get_from_cache(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        """Reads historical data from SQLite cache."""
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT date as 日期, open as 开盘, close as 收盘, high as 最高, low as 最低, volume as 成交量
                FROM daily_prices
                WHERE symbol = ? AND adjust = ? AND date >= ? AND date <= ?
                ORDER BY date ASC
            """
            df = pd.read_sql_query(query, conn, params=(symbol, adjust, start_date, end_date))
        return df

    def _save_to_cache(self, symbol: str, df: pd.DataFrame, adjust: str):
        """Saves historical data to SQLite cache."""
        if df is None or df.empty:
            return
            
        to_insert = []
        for idx, row in df.iterrows():
            date_val = ""
            open_val = 0.0
            close_val = 0.0
            high_val = 0.0
            low_val = 0.0
            volume_val = 0.0
            
            if '日期' in row:
                date_val = str(row['日期']).replace('-', '')[:8]
                open_val = float(row.get('开盘', 0.0))
                close_val = float(row.get('收盘', 0.0))
                high_val = float(row.get('最高', 0.0))
                low_val = float(row.get('最低', 0.0))
                volume_val = float(row.get('成交量', 0.0))
            elif isinstance(idx, pd.Timestamp) or isinstance(idx, datetime.datetime) or isinstance(idx, datetime.date):
                date_val = idx.strftime('%Y%m%d')
                open_val = float(row.get('Open', 0.0))
                close_val = float(row.get('Close', 0.0))
                high_val = float(row.get('High', 0.0))
                low_val = float(row.get('Low', 0.0))
                volume_val = float(row.get('Volume', 0.0))
            
            if date_val and open_val > 0 and close_val > 0:
                to_insert.append((symbol, date_val, open_val, close_val, high_val, low_val, volume_val, adjust))
                
        if to_insert:
            with sqlite3.connect(self.db_path) as conn:
                c = conn.cursor()
                c.executemany("""
                    INSERT OR REPLACE INTO daily_prices 
                    (symbol, date, open, close, high, low, volume, adjust)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, to_insert)
                conn.commit()

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=4))
    def _fetch_from_yfinance(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        yf_sym = self._to_yf_symbol(symbol)
        
        start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_dt = datetime.datetime.strptime(end_date, "%Y%m%d") + datetime.timedelta(days=1)
        end_fmt = end_dt.strftime("%Y-%m-%d")
        
        ticker = yf.Ticker(yf_sym)
        df = ticker.history(start=start_fmt, end=end_fmt, auto_adjust=False)
        
        if df.empty:
            raise ValueError(f"YFinance returned empty dataframe for {yf_sym}")
            
        df = df.reset_index()
        if 'Date' in df.columns:
            df = df.rename(columns={
                'Date': '日期', 'Open': '开盘', 'Close': '收盘',
                'High': '最高', 'Low': '最低', 'Volume': '成交量'
            })
            df['日期'] = df['日期'].dt.strftime('%Y%m%d')
            
        return df

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_from_akshare(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        if len(symbol) == 6 and symbol.isdigit():
            df = ak.stock_zh_a_hist(symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust)
            if df.empty:
                df = ak.fund_etf_hist_em(symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust)
        else:
            raise ValueError("Akshare fallback only implemented for A-shares/ETFs.")
            
        if df.empty:
            raise ValueError(f"Akshare returned empty dataframe for {symbol}")
            
        return df

    def get_historical_prices(self, symbol: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
        """
        Standardized method to get historical daily prices.
        """
        start_date = str(start_date).replace('-', '')
        end_date = str(end_date).replace('-', '')
        
        df_cache = self._get_from_cache(symbol, start_date, end_date, adjust)
        
        if not df_cache.empty:
            cache_max = str(df_cache['日期'].max()).replace('-', '')[:8]
            if cache_max >= end_date:
                return df_cache

        df_new = pd.DataFrame()
        is_a_share = len(symbol) == 6 and symbol.isdigit()
        
        if is_a_share:
            try:
                df_new = self._fetch_from_akshare(symbol, start_date, end_date, adjust)
            except Exception as e:
                logger.warning(f"DataGateway: akshare failed for {symbol} ({start_date}-{end_date}): {e}. Falling back to yfinance.")
                try:
                    df_new = self._fetch_from_yfinance(symbol, start_date, end_date)
                except Exception as e2:
                    logger.error(f"DataGateway: yfinance also failed for {symbol}: {e2}.")
        else:
            try:
                df_new = self._fetch_from_yfinance(symbol, start_date, end_date)
            except Exception as e:
                logger.error(f"DataGateway: yfinance failed for {symbol}: {e}.")
                
        if not df_new.empty:
            if (df_new['收盘'] <= 0).any():
                logger.error(f"DataGateway: Integrity check failed (price <= 0) for {symbol}")
                df_new = df_new[df_new['收盘'] > 0]
                
            self._save_to_cache(symbol, df_new, adjust)
            
            if not df_cache.empty:
                df_new = pd.concat([df_cache, df_new]).drop_duplicates(subset=['日期']).sort_values('日期')
                
            # Final filter to return only the requested range
            df_new['日期'] = df_new['日期'].astype(str).str.replace('-', '').str[:8]
            df_new = df_new[(df_new['日期'] >= start_date) & (df_new['日期'] <= end_date)]
            return df_new
            
        return df_cache

    def get_open_price(self, symbol: str, target_date: str) -> float:
        """
        Gets the open price for a specific date (or the next available trading day).
        """
        target_date = str(target_date).replace('-', '')[:8]
        start_date = target_date
        end_dt = datetime.datetime.strptime(target_date, "%Y%m%d") + datetime.timedelta(days=7)
        end_date = end_dt.strftime("%Y%m%d")
        
        df = self.get_historical_prices(symbol, start_date, end_date, adjust="")
        
        if not df.empty:
            for _, row in df.iterrows():
                dt_str = str(row['日期']).replace('-', '')
                if dt_str >= target_date:
                    return float(row['开盘'])
                    
        return 0.0

    def get_current_price(self, symbol: str) -> float:
        """
        Gets the latest available snapshot price (close).
        """
        today_str = datetime.datetime.now().strftime("%Y%m%d")
        start_dt = datetime.datetime.now() - datetime.timedelta(days=7)
        start_date = start_dt.strftime("%Y%m%d")
        
        df = self.get_historical_prices(symbol, start_date, today_str, adjust="")
        
        if not df.empty:
            return float(df.iloc[-1]['收盘'])
            
        return 0.0
