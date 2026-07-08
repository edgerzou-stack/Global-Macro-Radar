import datetime
import pytz
import yfinance as yf
import akshare as ak
import pandas as pd
from typing import Dict, List, Any
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch_a_hist(*args, **kwargs):
    return ak.stock_zh_a_hist(*args, **kwargs)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch_etf_hist(*args, **kwargs):
    return ak.fund_etf_hist_em(*args, **kwargs)

class PortfolioManager:
    def __init__(self, db_utils_module):
        """
        Pass the db_utils module to interact with the database.
        """
        self.db = db_utils_module
        
    def get_simulated_trade_price(self, prices_dict: Dict[str, Any], market_type: str) -> float:
        """
        Returns the simulated trade price based on report generation time in the local market's timezone.
        """
        if not isinstance(prices_dict, dict):
            return float(prices_dict) if isinstance(prices_dict, (int, float)) else 0.0
            
        from core.clock import clock
        now_utc = clock.now(pytz.utc)
        
        if "_a_" in market_type or "_hk_" in market_type:
            local_time = now_utc.astimezone(pytz.timezone("Asia/Shanghai"))
            close_time = 16.0 if "_hk_" in market_type else 15.0
        elif "_us_" in market_type:
            local_time = now_utc.astimezone(pytz.timezone("US/Eastern"))
            close_time = 16.0
        else:
            local_time = now_utc.astimezone(pytz.timezone("Asia/Shanghai"))
            close_time = 15.0
            
        time_val = local_time.hour + local_time.minute / 60.0
        latest = prices_dict.get("最新价", 0)
        
        def valid_price(p, fallback):
            try:
                return float(p) if p is not None and float(p) > 0 else float(fallback)
            except (ValueError, TypeError):
                return float(fallback)
                
        # If market is closed (pre-market or post-market), we cannot execute immediately.
        # Return 0.0 to mark the trade as "Pending" until the next Open.
        if time_val < 9.5 or time_val >= close_time:
            return 0.0
        else:
            return valid_price(latest, 0.0)
            
    def resolve_pending_prices(self):
        """
        Scans portfolio and trade_history for 0.0 (Pending) prices.
        Fetches the historical Open price of the subsequent trading session to resolve them.
        """
        import akshare as ak
        import yfinance as yf
        import datetime
        import pandas as pd
        import os
        
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        def get_a_share_name_to_code_map():
            cache_file = os.path.join(os.path.dirname(__file__), "a_share_map_cache.json")
            if os.path.exists(cache_file):
                try:
                    import json
                    with open(cache_file, "r") as f:
                        return json.load(f)
                except Exception:
                    pass
            name_to_code = {}
            try:
                df1 = ak.stock_zh_a_spot_em()
                if not df1.empty:
                    for _, row in df1.iterrows():
                        name_to_code[row["名称"]] = row["代码"]
                df2 = ak.fund_etf_spot_em()
                if not df2.empty:
                    for _, row in df2.iterrows():
                        name_to_code[row["名称"]] = row["代码"]
                import json
                with open(cache_file, "w") as f:
                    json.dump(name_to_code, f, ensure_ascii=False)
            except Exception as e:
                print(f"Failed to fetch name to code map: {e}")
            return name_to_code
            
        cursor.execute("SELECT id, strategy, name_or_code, entry_date FROM portfolio WHERE entry_price <= 0.0")
        portfolio_pending = cursor.fetchall()
        
        cursor.execute("SELECT id, strategy, name_or_code, entry_date, entry_price, exit_date, exit_price FROM trade_history WHERE entry_price <= 0.0 OR exit_price <= 0.0")
        trade_pending = cursor.fetchall()

        if not portfolio_pending and not trade_pending:
            conn.close()
            return
            
        reqs = []
        for row in portfolio_pending:
            reqs.append({'type': 'portfolio', 'id': row[0], 'strat': row[1], 'key': row[2], 'date': row[3], 'field': 'ep'})
        for row in trade_pending:
            if row[4] <= 0.0:
                reqs.append({'type': 'trade', 'id': row[0], 'strat': row[1], 'key': row[2], 'date': row[3], 'field': 'ep'})
            if row[6] <= 0.0:
                reqs.append({'type': 'trade', 'id': row[0], 'strat': row[1], 'key': row[2], 'date': row[5], 'field': 'xp'})

        yf_tickers = set()
        min_yf_date = None
        max_yf_date = None
        a_reqs = []
        
        for req in reqs:
            dt_obj = datetime.datetime.strptime(req['date'][:10], "%Y-%m-%d")
            if '_a_' in req['strat']:
                a_reqs.append(req)
            else:
                yf_sym = f"{req['key']}.HK" if '_hk_' in req['strat'] and not req['key'].upper().endswith('.HK') else req['key']
                yf_tickers.add(yf_sym)
                req['yf_sym'] = yf_sym
                if min_yf_date is None or dt_obj < min_yf_date:
                    min_yf_date = dt_obj
                if max_yf_date is None or dt_obj > max_yf_date:
                    max_yf_date = dt_obj
        
        yf_prices = {}
        if yf_tickers:
            start_str = min_yf_date.strftime("%Y-%m-%d")
            end_str = (max_yf_date + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
            tickers_list = list(yf_tickers)
            print(f"Batch downloading yfinance prices for {len(tickers_list)} tickers from {start_str} to {end_str}...")
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    df_yf = yf.download(tickers_list, start=start_str, end=end_str, group_by="ticker", threads=True, progress=False)
                
                if len(tickers_list) == 1:
                    sym = tickers_list[0]
                    for idx, row in df_yf.iterrows():
                        dt_str = idx.strftime("%Y-%m-%d")
                        if not pd.isna(row.get('Open')):
                            yf_prices[(sym, dt_str)] = float(row['Open'])
                else:
                    for sym in tickers_list:
                        if sym in df_yf.columns.levels[0]:
                            df_sym = df_yf[sym]
                            for idx, row in df_sym.iterrows():
                                dt_str = idx.strftime("%Y-%m-%d")
                                if not pd.isna(row.get('Open')):
                                    yf_prices[(sym, dt_str)] = float(row['Open'])
            except Exception as e:
                print(f"Batch yfinance download failed: {e}")

        name_to_code_map = get_a_share_name_to_code_map() if a_reqs else {}
        a_cache = {}
        
        def fetch_open_price(req):
            dt_obj = datetime.datetime.strptime(req['date'][:10], "%Y-%m-%d")
            if '_a_' in req['strat']:
                fetch_key = name_to_code_map.get(req['key'], req['key'])
                start_dt = dt_obj.strftime("%Y%m%d")
                df = None
                if fetch_key in a_cache:
                    df = a_cache[fetch_key]
                else:
                    end_dt = (datetime.datetime.now() + datetime.timedelta(days=7)).strftime("%Y%m%d")
                    try:
                        df = _fetch_a_hist(symbol=fetch_key, start_date=start_dt, end_date=end_dt, adjust="")
                    except Exception:
                        pass
                    if df is None or df.empty:
                        try:
                            df = _fetch_etf_hist(symbol=fetch_key, start_date=start_dt, end_date=end_dt, adjust="")
                        except Exception:
                            pass
                    a_cache[fetch_key] = df
                    
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        if str(row['日期']).replace('-', '') >= start_dt:
                            return float(row['开盘'])
            else:
                sym = req['yf_sym']
                for i in range(7):
                    test_date = (dt_obj + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                    if (sym, test_date) in yf_prices:
                        return yf_prices[(sym, test_date)]
            return 0.0
            
        updated = False
        updates = {'portfolio': {}, 'trade': {}}
        for req in reqs:
            price = fetch_open_price(req)
            if price > 0:
                if req['id'] not in updates[req['type']]:
                    updates[req['type']][req['id']] = {}
                updates[req['type']][req['id']][req['field']] = price

        for pid, data in updates['portfolio'].items():
            if 'ep' in data:
                cursor.execute("UPDATE portfolio SET entry_price = ? WHERE id = ?", (data['ep'], pid))
                updated = True
                
        for tid, data in updates['trade'].items():
            cursor.execute("SELECT strategy, entry_price, exit_price FROM trade_history WHERE id = ?", (tid,))
            strat, ep, xp = cursor.fetchone()
            ep = data.get('ep', ep)
            xp = data.get('xp', xp)
            
            if ep > 0 and xp > 0:
                fee_hk = float(os.getenv("FEE_HK", 0.002))
                fee_a = float(os.getenv("FEE_A", 0.001))
                fee_us = float(os.getenv("FEE_US", 0.000))
                
                if '_hk_' in strat: fee = fee_hk
                elif '_a_' in strat: fee = fee_a
                else: fee = fee_us
                
                pnl = (xp / ep - 1) - fee
                cursor.execute("UPDATE trade_history SET entry_price = ?, exit_price = ?, pnl = ? WHERE id = ?", (ep, xp, pnl, tid))
                updated = True
            elif 'ep' in data:
                cursor.execute("UPDATE trade_history SET entry_price = ? WHERE id = ?", (data['ep'], tid))
                updated = True
            elif 'xp' in data:
                cursor.execute("UPDATE trade_history SET exit_price = ? WHERE id = ?", (data['xp'], tid))
                updated = True
                
        if updated:
            conn.commit()
        conn.close()

    def diff_and_update(self, strategy_targets: Dict[str, List[str]], current_prices: Dict[str, Any], snapshot_date: str):
        """
        Compares the new target positions with the old portfolio to calculate trades.
        Then atomically updates the database.
        
        strategy_targets: { strat_id: [stock_code1, stock_code2, ...] }
        current_prices: { stock_code: {"最新价": 100, "今开": 99, ...} }
        """
    def diff_and_update(self, strategy_targets: dict, current_prices: dict, snapshot_date: str):
        """
        Calculates diff between current targets and old portfolio, updates DB, generates trades.
        """
        self.resolve_pending_prices()
        old_portfolio, _ = self.db.load_portfolio_and_trades()
        new_portfolio = {s: {} for s in strategy_targets.keys()}
        new_trades = []
        diff = {s: {"added": [], "removed": []} for s in strategy_targets.keys()}
        
        from core.cash_manager import CashManager
        import pytz
        import datetime
        import pandas as pd
        import yfinance as yf
        from core.clock import clock
        import os
        
        cash_mgr = CashManager()
        
        for strat, target_keys in strategy_targets.items():
            cash_mgr.initialize_strategy(strat)
            target_keys_set = set(target_keys)
            old_keys = set(old_portfolio.get(strat, {}).keys())
            
            # --- Pre-process Hard Stop-Loss (Overrides LLM recommendations) ---
            hard_stop_keys = set()
            for key in old_keys:
                old_pos = old_portfolio[strat][key]
                ep = old_pos.get("entry_price", 0)
                shares = old_pos.get("shares", 1)
                cp = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                
                # Hard stop loss: drops -25% AND has max tranches (3)
                if ep > 0 and shares >= 3 and cp > 0 and cp <= ep * 0.75:
                    print(f"HARD STOP-LOSS: {key} in {strat} dropped -25% from cost {ep:.2f}. Forcing liquidation.")
                    hard_stop_keys.add(key)
                    if key in target_keys_set:
                        target_keys_set.remove(key)
                        
            added = target_keys_set - old_keys
            removed = (old_keys - target_keys_set) | hard_stop_keys
            
            # Track stocks sold today to enforce T+0 re-entry guard
            sold_today_keys = set()
            
            # --- Process REMOVED first (so we know what was sold today and release cash) ---
            for key in removed:
                old_pos = old_portfolio[strat][key]
                ep = old_pos.get("entry_price", 0)
                shares = old_pos.get("shares", 1)
                entry_date = old_pos.get("entry_date", "未知")
                
                tz_str = "US/Eastern" if "_us_" in strat else ("Asia/Hong_Kong" if "_hk_" in strat else "Asia/Shanghai")
                now_local = clock.now(pytz.timezone(tz_str))
                
                is_pre_market = now_local.hour < 9 or (now_local.hour == 9 and now_local.minute < 30)
                
                if is_pre_market:
                    effective_today_dt = now_local - datetime.timedelta(days=1)
                    while effective_today_dt.weekday() >= 5: # 5=Sat, 6=Sun
                        effective_today_dt -= datetime.timedelta(days=1)
                    effective_today = effective_today_dt.strftime("%Y-%m-%d")
                else:
                    effective_today = now_local.strftime("%Y-%m-%d")
                    
                if entry_date[:10] >= effective_today or ep <= 0:
                    # T+1 rule: ignore trades if the market hasn't opened for a new session since entry
                    # OR if entry_price <= 0 (meaning the buy order hasn't been confirmed/resolved yet)
                    new_portfolio[strat][key] = old_pos
                    if key in hard_stop_keys:
                        print(f"T+1 BLOCK: Cannot hard stop-loss {key} today as it was bought today or is still pending execution.")
                    continue
                    
                cp = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                if cp <= 0:
                    print(f"WARNING: Could not fetch exit price for {key}, using entry price as fallback")
                    cp = ep
                    
                fee_hk = float(os.getenv("FEE_HK", 0.002))
                fee_a = float(os.getenv("FEE_A", 0.001))
                fee_us = float(os.getenv("FEE_US", 0.000))
                
                if '_hk_' in strat:
                    fee = fee_hk
                elif '_a_' in strat:
                    fee = fee_a
                else:
                    fee = fee_us
                    
                raw_pnl = (cp / ep - 1) - fee if ep > 0 else 0
                pnl = raw_pnl
                
                # Fetch adjusted prices for accurate returns
                try:
                    if '_a_' in strat and ep > 0:
                        df = pd.DataFrame()
                        try:
                            df = _fetch_a_hist(symbol=key, start_date=entry_date.replace('-','')[:8], end_date=snapshot_date.replace('-','')[:8], adjust="hfq")
                        except Exception as fetch_err:
                            print(f"akshare fetch failed for {key}: {fetch_err}, trying yfinance fallback.")
                            
                        if not df.empty and len(df) >= 2:
                            adj_ep = float(df.iloc[0]['收盘'])
                            adj_cp = float(df.iloc[-1]['收盘'])
                            pnl = (adj_cp / adj_ep - 1) - fee
                        else:
                            yf_sym = f"{key}.SS" if str(key).startswith('6') else f"{key}.SZ"
                            ticker = yf.Ticker(yf_sym)
                            end_dt = datetime.datetime.strptime(snapshot_date[:10], "%Y-%m-%d") + datetime.timedelta(days=1)
                            df_yf = ticker.history(start=entry_date[:10], end=end_dt.strftime("%Y-%m-%d"))
                            if not df_yf.empty and len(df_yf) >= 2:
                                adj_ep = float(df_yf.iloc[0]['Close'])
                                adj_cp = float(df_yf.iloc[-1]['Close'])
                                pnl = (adj_cp / adj_ep - 1) - fee
                            else:
                                raise ValueError(f"CRITICAL: Failed to fetch adjusted prices for A-share {key}. Aborting.")
                    elif ('_hk_' in strat or '_us_' in strat) and ep > 0:
                        yf_sym = f"{key}.HK" if '_hk_' in strat and not key.upper().endswith('.HK') else key
                        ticker = yf.Ticker(yf_sym)
                        end_dt = datetime.datetime.strptime(snapshot_date[:10], "%Y-%m-%d") + datetime.timedelta(days=1)
                        df = ticker.history(start=entry_date[:10], end=end_dt.strftime("%Y-%m-%d"))
                        if not df.empty and len(df) >= 2:
                            adj_ep = float(df.iloc[0]['Close'])
                            adj_cp = float(df.iloc[-1]['Close'])
                            pnl = (adj_cp / adj_ep - 1) - fee
                        else:
                            raise ValueError(f"CRITICAL: Failed to fetch adjusted prices for {key} from yfinance. Aborting.")
                except Exception as e:
                    raise ValueError(f"CRITICAL: Data fetching failed for {key}: {e}. Aborting.")
                    
                if key in hard_stop_keys:
                    reason = "[风控熔断] 加仓满3次后仍重度亏损，触发绝对止损线强制清仓"
                else:
                    try:
                        from core.diagnose import diagnose_elimination
                        reason = diagnose_elimination(key, strat)
                    except Exception as e:
                        print(f"Failed to diagnose elimination for {key}: {e}")
                        reason = ""
                    
                diff[strat]["removed"].append({"name": key, "entry_price": ep, "exit_price": cp, "pnl": pnl, "reason": reason})
                t = {"strategy": strat, "name": key, "entry_date": entry_date, "entry_price": ep, "exit_date": snapshot_date, "exit_price": cp, "pnl": pnl, "reason": reason, "shares": shares}
                new_trades.append(t)
                sold_today_keys.add(key)
                
                # IMPORTANT: Release cash!
                cash_mgr.release(strat, shares, pnl)
            
            # --- Process RETAINED (Average Down) ---
            retained = target_keys_set & old_keys
            for key in retained:
                old_pos = old_portfolio[strat][key]
                ep = old_pos.get("entry_price", 0)
                shares = old_pos.get("shares", 1)
                new_portfolio[strat][key] = old_pos
                
                if ep > 0 and shares < 3:
                    cp = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                    if cp > 0 and cp <= ep * 0.90:  # -10% threshold
                        if cash_mgr.allocate(strat): # Try to lock funds!
                            print(f"POSITION AVERAGING: {key} in {strat} dropped -10% from cost {ep:.2f} to {cp:.2f}. Adding tranche {shares + 1}/3.")
                            new_ep = (shares + 1) / ((shares / ep) + (1 / cp))
                            new_portfolio[strat][key]["entry_price"] = new_ep
                            new_portfolio[strat][key]["shares"] = shares + 1
                            diff[strat]["added"].append({"name": key, "entry_price": cp, "reason": f"网格加仓(当前第{shares+1}份)"})
                        else:
                            print(f"POSITION AVERAGING DENIED: {key} in {strat} dropped, but insufficient cash for tranche {shares + 1}.")
            
            # --- Process ADDED (with T+0 re-entry guard) ---
            for key in added:
                if key in sold_today_keys and '_a_' in strat:
                    print(f"T+0 GUARD: Skipping re-entry of {key} in {strat} (sold today)")
                    continue
                    
                price = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                if cash_mgr.allocate(strat):
                    diff[strat]["added"].append({"name": key, "entry_price": price})
                    new_portfolio[strat][key] = {"entry_date": snapshot_date, "entry_price": price, "shares": 1}
                else:
                    print(f"NEW ENTRY DENIED: {key} in {strat} skipped due to insufficient cash.")
                        
        # Persist transactions safely
        self.db.update_portfolio_and_trades(new_portfolio, new_trades, snapshot_date=snapshot_date)
        
        return new_portfolio, new_trades, diff
