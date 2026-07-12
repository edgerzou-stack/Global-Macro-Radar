import os
import sqlite3
import pandas as pd
from core.data_anomaly import DataAnomalyError

import datetime
import yfinance as yf
import akshare as ak
import baostock as bs
import logging
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

class DataIntegrityError(Exception):
    """Raised when fetched data fails integrity checks (e.g., price <= 0)."""
    pass

class CircuitBreakerError(Exception):
    """Raised when a data source triggers a circuit breaker."""
    pass

class FatalSystemError(Exception):
    """Raised when all data sources are broken (double circuit breaker)."""
    pass

class CircuitBreaker:
    def __init__(self, name: str, threshold: int = 2):
        self.name = name
        self.threshold = threshold
        self.failures = 0
        self.tripped = False
        
    def record_failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            if not self.tripped:
                logger.error(f"[CIRCUIT BREAKER TRIPPED] Data source '{self.name}' has failed {self.failures} times sequentially. Bypassing it entirely for this run.")
            self.tripped = True
            
    def record_success(self):
        # Only reset if not tripped. Once tripped, it stays tripped for the run.
        if not self.tripped:
            self.failures = 0

class DataGateway:
    # Class level circuit breakers (persist across requests in a single run)
    CB_BAOSTOCK = CircuitBreaker("baostock", threshold=2)
    CB_SINA = CircuitBreaker("sina_akshare", threshold=2)
    CB_YFINANCE = CircuitBreaker("yfinance", threshold=2)

    def __init__(self, db_path=None):
        if db_path is None:
            scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cache_dir = os.path.join(scripts_dir, ".cache")
            os.makedirs(cache_dir, exist_ok=True)
            db_path = os.path.join(cache_dir, "market_data_cache.db")
        
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            c = conn.cursor()
            c.execute('PRAGMA journal_mode=WAL;')
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
        if '.' in symbol and symbol.upper().endswith(('HK', 'US', 'SS', 'SZ', 'BJ')):
            return symbol
        symbol_str = str(symbol)
        if len(symbol_str) == 6 and symbol_str.isdigit():
            if symbol_str.startswith('6'):
                return f"{symbol_str}.SS"
            elif symbol_str.startswith(('8', '4', '9')):
                return f"{symbol_str}.BJ"
            else:
                return f"{symbol_str}.SZ"
        return symbol

    def _to_bs_symbol(self, symbol: str) -> str:
        """Converts internal symbol (like 600519) to baostock symbol (sh.600519)."""
        symbol_str = str(symbol)
        if len(symbol_str) == 6 and symbol_str.isdigit():
            if symbol_str.startswith('6'):
                return f"sh.{symbol_str}"
            elif symbol_str.startswith(('8', '4', '9')):
                return f"bj.{symbol_str}"
            else:
                return f"sz.{symbol_str}"
        return symbol

    def _to_sina_symbol(self, symbol: str) -> str:
        """Converts internal symbol (like 600519) to sina symbol (sh600519)."""
        symbol_str = str(symbol)
        if len(symbol_str) == 6 and symbol_str.isdigit():
            if symbol_str.startswith('6'):
                return f"sh{symbol_str}"
            elif symbol_str.startswith(('8', '4', '9')):
                return f"bj{symbol_str}"
            else:
                return f"sz{symbol_str}"
        return symbol

    
    def verify_extreme_move(self, symbol: str, duration_days: int, entry_price: float, exit_price: float) -> bool:
        """
        Verifies if a price move is mathematically possible given the duration and market limits.
        Raises DataAnomalyError if the move is impossible (indicating mixed adjusted/unadjusted data).
        """
        if entry_price <= 0: return True
        
        ret = (exit_price / entry_price) - 1
        
        is_a_share = len(symbol) == 6 and symbol.isdigit()
        if is_a_share:
            # A-shares have a daily limit of 10% or 20% (STAR/ChiNext).
            # We allow a maximum of 25% per trading day (to account for edge cases / new IPOs / resumptions).
            # Over multiple days, the max return compounds.
            # If duration is very short (e.g. 1-3 days) and return is absurdly high/low, block it.
            max_daily_return = 0.25
            min_daily_return = -0.25
            
            # Simple linear approximation for bounds check to prevent extreme silent corruption
            max_allowed = (1 + max_daily_return) ** duration_days - 1
            min_allowed = (1 + min_daily_return) ** duration_days - 1
            
            # Allow slightly more leniency for compounding, but block obvious anomalies like -92% or +80% in 1 day
            if duration_days <= 5:
                if ret < -0.50 or ret > 0.80:
                    raise DataAnomalyError(f"Impossible A-share return of {ret:.2%} over {duration_days} days for {symbol}. Suspect unadjusted ex-dividend data.")
                    
        return True

    def _get_from_cache(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute('PRAGMA journal_mode=WAL;')
            query = """
                SELECT date as 日期, open as 开盘, close as 收盘, high as 最高, low as 最低, volume as 成交量
                FROM daily_prices
                WHERE symbol = ? AND adjust = ? AND date >= ? AND date <= ?
                ORDER BY date ASC
            """
            df = pd.read_sql_query(query, conn, params=(symbol, adjust, start_date, end_date))
        return df

    def _save_to_cache(self, symbol: str, df: pd.DataFrame, adjust: str):
        if df is None or df.empty:
            return
            
        if '日期' in df.columns:
            date_series = df['日期'].astype(str).str.replace('-', '').str.slice(0, 8)
            open_series = pd.to_numeric(df.get('开盘', 0.0), errors='coerce').fillna(0.0)
            close_series = pd.to_numeric(df.get('收盘', 0.0), errors='coerce').fillna(0.0)
            high_series = pd.to_numeric(df.get('最高', 0.0), errors='coerce').fillna(0.0)
            low_series = pd.to_numeric(df.get('最低', 0.0), errors='coerce').fillna(0.0)
            vol_series = pd.to_numeric(df.get('成交量', 0.0), errors='coerce').fillna(0.0)
        else:
            date_series = pd.to_datetime(df.index).strftime('%Y%m%d')
            open_series = pd.to_numeric(df.get('Open', 0.0), errors='coerce').fillna(0.0)
            close_series = pd.to_numeric(df.get('Close', 0.0), errors='coerce').fillna(0.0)
            high_series = pd.to_numeric(df.get('High', 0.0), errors='coerce').fillna(0.0)
            low_series = pd.to_numeric(df.get('Low', 0.0), errors='coerce').fillna(0.0)
            vol_series = pd.to_numeric(df.get('Volume', 0.0), errors='coerce').fillna(0.0)

        df_db = pd.DataFrame({
            'symbol': symbol,
            'date': date_series,
            'open': open_series,
            'close': close_series,
            'high': high_series,
            'low': low_series,
            'volume': vol_series,
            'adjust': adjust
        })
        
        df_db = df_db[(df_db['date'] != '') & (df_db['open'] > 0) & (df_db['close'] > 0)]
        
        if not df_db.empty:
            to_insert = list(df_db.itertuples(index=False, name=None))
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                conn.execute('PRAGMA journal_mode=WAL;')
                c = conn.cursor()
                c.executemany("""
                    INSERT OR REPLACE INTO daily_prices 
                    (symbol, date, open, close, high, low, volume, adjust)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, to_insert)
                conn.commit()

    @retry(stop=stop_after_attempt(1))
    def _fetch_from_baostock(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        if DataGateway.CB_BAOSTOCK.tripped:
            raise CircuitBreakerError("BaoStock circuit breaker is tripped.")
            
        bs_sym = self._to_bs_symbol(symbol)
        start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        
        # adjustflag: '1': hfq (post-adjust), '2': qfq (pre-adjust), '3': non-adjust
        flag = "2" if adjust == "qfq" else ("1" if adjust == "hfq" else "3")
        
        try:
            bs.login()
            rs = bs.query_history_k_data_plus(
                bs_sym, "date,open,high,low,close,volume",
                start_date=start_fmt, end_date=end_fmt, frequency="d", adjustflag=flag
            )
            
            if rs.error_code != '0':
                raise ValueError(f"BaoStock query error: {rs.error_msg}")
                
            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            if not data_list:
                raise ValueError(f"BaoStock returned empty dataframe for {bs_sym}")
                
            df = pd.DataFrame(data_list, columns=rs.fields)
            df = df.rename(columns={
                'date': '日期', 'open': '开盘', 'close': '收盘',
                'high': '最高', 'low': '最低', 'volume': '成交量'
            })
            df['日期'] = df['日期'].str.replace('-', '')
            
            # Numeric conversion
            for col in ['开盘', '收盘', '最高', '最低', '成交量']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            
            DataGateway.CB_BAOSTOCK.record_success()
            return df
        except ValueError as e:
            raise e
        except Exception as e:
            DataGateway.CB_BAOSTOCK.record_failure()
            raise e
        finally:
            bs.logout()

    @retry(stop=stop_after_attempt(1))
    def _fetch_from_sina(self, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        if DataGateway.CB_SINA.tripped:
            raise CircuitBreakerError("Sina circuit breaker is tripped.")
            
        sina_sym = self._to_sina_symbol(symbol)
        try:
            df = ak.stock_zh_a_daily(symbol=sina_sym, start_date=start_date, end_date=end_date, adjust=adjust)
            if df.empty:
                raise ValueError(f"Sina returned empty dataframe for {sina_sym}")
            
            df = df.reset_index()
            if 'date' in df.columns:
                df = df.rename(columns={
                    'date': '日期', 'open': '开盘', 'close': '收盘',
                    'high': '最高', 'low': '最低', 'volume': '成交量'
                })
                # Check if it's datetime or str
                if pd.api.types.is_datetime64_any_dtype(df['日期']):
                    df['日期'] = df['日期'].dt.strftime('%Y%m%d')
                else:
                    df['日期'] = df['日期'].astype(str).str.replace('-', '')
            
            DataGateway.CB_SINA.record_success()
            return df
        except ValueError as e:
            raise e
        except Exception as e:
            DataGateway.CB_SINA.record_failure()
            raise e

    @retry(stop=stop_after_attempt(1))
    def _fetch_from_yfinance(self, symbol: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
        if DataGateway.CB_YFINANCE.tripped:
            raise CircuitBreakerError("YFinance circuit breaker is tripped.")
            
        yf_sym = self._to_yf_symbol(symbol)
        start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        end_dt = datetime.datetime.strptime(end_date, "%Y%m%d") + datetime.timedelta(days=1)
        end_fmt = end_dt.strftime("%Y-%m-%d")
        
        auto_adj = True if adjust == "hfq" else False
        try:
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(start=start_fmt, end=end_fmt, auto_adjust=auto_adj)
            
            if df.empty:
                raise ValueError(f"YFinance returned empty dataframe for {yf_sym}")
                
            df = df.reset_index()
            if 'Date' in df.columns:
                df = df.rename(columns={
                    'Date': '日期', 'Open': '开盘', 'Close': '收盘',
                    'High': '最高', 'Low': '最低', 'Volume': '成交量'
                })
                df['日期'] = df['日期'].dt.strftime('%Y%m%d')
                
            DataGateway.CB_YFINANCE.record_success()
            return df
        except ValueError as e:
            raise e
        except Exception as e:
            DataGateway.CB_YFINANCE.record_failure()
            raise e

    def get_historical_prices(self, symbol: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
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
            # Multi-level Failover Strategy for A-Shares
            try:
                df_new = self._fetch_from_baostock(symbol, start_date, end_date, adjust)
            except Exception as e_bs:
                logger.warning(f"DataGateway: baostock failed for {symbol}: {e_bs}. Falling back to Sina.")
                try:
                    df_new = self._fetch_from_sina(symbol, start_date, end_date, adjust)
                except Exception as e_sina:
                    logger.warning(f"DataGateway: Sina failed for {symbol}: {e_sina}. Falling back to YFinance.")
                    try:
                        df_new = self._fetch_from_yfinance(symbol, start_date, end_date, adjust)
                    except Exception as e_yf:
                        logger.error(f"DataGateway: All A-share sources failed for {symbol}.")
                        
                        # Check global double (or triple) circuit breaker logic
                        if DataGateway.CB_BAOSTOCK.tripped and DataGateway.CB_SINA.tripped and DataGateway.CB_YFINANCE.tripped:
                            err_msg = "FATAL ERROR: All three data sources (BaoStock, Sina, YFinance) have tripped their circuit breakers. Aborting pipeline to prevent dirty data."
                            logger.critical(err_msg)
                            raise FatalSystemError(err_msg)
        else:
            # Non-A shares (US/HK)
            try:
                df_new = self._fetch_from_yfinance(symbol, start_date, end_date, adjust)
            except Exception as e:
                logger.error(f"DataGateway: YFinance failed for {symbol}: {e}.")
                if DataGateway.CB_YFINANCE.tripped:
                    err_msg = "FATAL ERROR: YFinance circuit breaker tripped for non-A-share asset. Aborting pipeline."
                    logger.critical(err_msg)
                    raise FatalSystemError(err_msg)
                
        if not df_new.empty:
            if (df_new['收盘'] <= 0).any():
                logger.error(f"DataGateway: Integrity check failed (price <= 0) for {symbol}")
                df_new = df_new[df_new['收盘'] > 0]
                
            self._save_to_cache(symbol, df_new, adjust)
            
            if not df_cache.empty:
                df_new = pd.concat([df_cache, df_new]).drop_duplicates(subset=['日期']).sort_values('日期')
                
            df_new['日期'] = df_new['日期'].astype(str).str.replace('-', '').str[:8]
            df_new = df_new[(df_new['日期'] >= start_date) & (df_new['日期'] <= end_date)]
            return df_new
            
        return df_cache

    def get_open_price(self, symbol: str, target_date: str) -> float:
        from core.market import AShareMarket, HKMarket, USMarket
        if symbol.endswith('.HK'):
            market = HKMarket()
        elif len(symbol) == 6 and symbol.isdigit():
            market = AShareMarket()
        else:
            market = USMarket()
            
        target_date_str = str(target_date).replace('-', '')[:8]
        target_dt = datetime.datetime.strptime(target_date_str, "%Y%m%d").date()
        
        exact_trade_date = market.get_previous_trading_date(target_dt)
        exact_date_str = exact_trade_date.strftime("%Y%m%d")
        
        try:
            df = self.get_historical_prices(symbol, exact_date_str, exact_date_str, adjust="")
            if not df.empty and '日期' in df.columns:
                dt_series = df['日期'].astype(str).str.replace('-', '')
                match = df[dt_series == exact_date_str]
                if not match.empty:
                    return float(match.iloc[0]['开盘'])
        except FatalSystemError:
            raise
        except Exception as e:
            logger.error(f"Failed to get open price for {symbol} around {target_date}: {e}")
            
        return 0.0

    def get_current_price(self, symbol: str) -> float:
        from core.market import AShareMarket, HKMarket, USMarket
        if symbol.endswith('.HK'):
            market = HKMarket()
        elif len(symbol) == 6 and symbol.isdigit():
            market = AShareMarket()
        else:
            market = USMarket()
            
        effective_date_str = market.get_effective_trading_date().replace('-', '')
        
        try:
            df = self.get_historical_prices(symbol, effective_date_str, effective_date_str, adjust="")
            if not df.empty:
                return float(df.iloc[-1]['收盘'])
        except FatalSystemError:
            raise
        except Exception as e:
            logger.error(f"Failed to get current price for {symbol}: {e}")
            
        return 0.0
