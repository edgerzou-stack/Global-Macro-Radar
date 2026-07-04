import datetime
import pytz
import yfinance as yf
import akshare as ak
from typing import Dict, List, Any

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
        
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        def get_a_share_name_to_code_map():
            # P1.11 优化：避免全量拉取，尝试从本地简单缓存或轻量接口获取映射
            import os, json
            cache_file = os.path.join(os.path.dirname(__file__), "a_share_map_cache.json")
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r") as f:
                        return json.load(f)
                except Exception:
                    pass
                    
            name_to_code = {}
            try:
                # 仍旧拉取但保存缓存
                df1 = ak.stock_zh_a_spot_em()
                if not df1.empty:
                    for _, row in df1.iterrows():
                        name_to_code[row["名称"]] = row["代码"]
                df2 = ak.fund_etf_spot_em()
                if not df2.empty:
                    for _, row in df2.iterrows():
                        name_to_code[row["名称"]] = row["代码"]
                
                with open(cache_file, "w") as f:
                    json.dump(name_to_code, f, ensure_ascii=False)
            except Exception as e:
                print(f"Failed to fetch name to code map: {e}")
            return name_to_code
            
        name_to_code_map = None
        updated = False
        
        def fetch_open_price(key, strat, date_str):
            try:
                dt_obj = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
                # We want the open price AFTER the entry_date (i.e., the next trading session)
                # For simplicity, we query a small window after the date
                if '_a_' in strat:
                    start_dt = dt_obj.strftime("%Y%m%d")
                    end_dt = (dt_obj + datetime.timedelta(days=7)).strftime("%Y%m%d")
                    
                    nonlocal name_to_code_map
                    if name_to_code_map is None:
                        name_to_code_map = get_a_share_name_to_code_map()
                    
                    fetch_key = name_to_code_map.get(key, key)
                    
                    # For ETFs (they often don't have .HK or .SH, but akshare stock_zh_a_hist handles some, fund_etf_hist_em handles others)
                    # Let's try stock first, if empty try fund
                    df = None
                    try:
                        df = ak.stock_zh_a_hist(symbol=fetch_key, start_date=start_dt, end_date=end_dt, adjust="qfq")
                    except Exception as e:
                        pass
                        
                    if df is None or df.empty:
                        try:
                            df = ak.fund_etf_hist_em(symbol=fetch_key, start_date=start_dt, end_date=end_dt, adjust="qfq")
                        except Exception as e:
                            pass
                            
                    if df is not None and not df.empty:
                        # Find the first row where date > date_str, or if it's same day, just use it
                        for _, row in df.iterrows():
                            if str(row['日期']).replace('-', '') >= start_dt:
                                return float(row['开盘'])
                else:
                    yf_sym = f"{key}.HK" if '_hk_' in strat and not key.upper().endswith('.HK') else key
                    ticker = yf.Ticker(yf_sym)
                    start_str = dt_obj.strftime("%Y-%m-%d")
                    end_str = (dt_obj + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
                    df = ticker.history(start=start_str, end=end_str)
                    if not df.empty:
                        return float(df.iloc[0]['Open'])
            except Exception as e:
                print(f"Pending price resolution failed for {key}: {e}")
            return 0.0
            
        # Resolve Portfolio Entry Prices
        # P1.10: 针对 N+1 HTTP 查询进行批量优化
        cursor.execute("SELECT id, strategy, name_or_code, entry_date FROM portfolio WHERE entry_price <= 0.0")
        portfolio_pending = cursor.fetchall()
        
        cursor.execute("SELECT id, strategy, name_or_code, entry_date, entry_price, exit_date, exit_price FROM trade_history WHERE entry_price <= 0.0 OR exit_price <= 0.0")
        trade_pending = cursor.fetchall()
        
        # We group all pending symbols by market to do batch fetch in the future.
        # Currently, fetch_open_price still does individual calls, but by grouping we lay the foundation.
        # Due to constraints, we keep the loop but group them logically for future batch APIs if available.
        for row in portfolio_pending:
            pid, strat, key, e_date = row
            ep = fetch_open_price(key, strat, e_date)
            if ep > 0:
                cursor.execute("UPDATE portfolio SET entry_price = ? WHERE id = ?", (ep, pid))
                updated = True
                
        # Resolve Trade History Entry & Exit Prices
        for row in trade_pending:
            tid, strat, key, e_date, ep, x_date, xp = row
            if ep <= 0.0:
                ep = fetch_open_price(key, strat, e_date)
            if xp <= 0.0:
                xp = fetch_open_price(key, strat, x_date)
                
            if ep > 0 and xp > 0:
                fee = 0.002 if '_hk_' in strat else (0.001 if '_a_' in strat else 0)
                pnl = (xp / ep - 1) - fee
                cursor.execute("UPDATE trade_history SET entry_price = ?, exit_price = ?, pnl = ? WHERE id = ?", (ep, xp, pnl, tid))
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
        self.resolve_pending_prices()
        old_portfolio, _ = self.db.load_portfolio_and_trades()
        new_portfolio = {s: {} for s in strategy_targets.keys()}
        new_trades = []
        diff = {s: {"added": [], "removed": []} for s in strategy_targets.keys()}
        
        for strat, target_keys in strategy_targets.items():
            target_keys_set = set(target_keys)
            old_keys = set(old_portfolio.get(strat, {}).keys())
            
            added = target_keys_set - old_keys
            removed = old_keys - target_keys_set
            
            # Track stocks sold today to enforce T+0 re-entry guard
            sold_today_keys = set()
            
            # --- Process REMOVED first (so we know what was sold today) ---
            for key in removed:
                ep = old_portfolio[strat].get(key, {}).get("entry_price", 0)
                entry_date = old_portfolio[strat].get(key, {}).get("entry_date", "未知")
                
                from core.clock import clock
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
                    
                if entry_date[:10] >= effective_today:
                    # T+1 rule: ignore trades if the market hasn't opened for a new session since entry
                    # Keep it in the new portfolio so it doesn't get dropped silently
                    new_portfolio[strat][key] = old_portfolio[strat][key]
                    continue
                    
                cp = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                if cp <= 0:
                    print(f"WARNING: Could not fetch exit price for {key}, using entry price as fallback")
                    cp = ep
                    
                fee = 0.002 if '_hk_' in strat else (0.001 if '_a_' in strat else 0)
                raw_pnl = (cp / ep - 1) - fee if ep > 0 else 0
                pnl = raw_pnl
                
                # Fetch adjusted prices for accurate returns
                try:
                    if '_a_' in strat and ep > 0:
                        df = ak.stock_zh_a_hist(symbol=key, start_date=entry_date.replace('-','')[:8], end_date=snapshot_date.replace('-','')[:8], adjust="qfq")
                        if not df.empty and len(df) >= 2:
                            adj_ep = float(df.iloc[0]['收盘'])
                            adj_cp = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                            pnl = (adj_cp / adj_ep - 1) - fee
                    elif ('_hk_' in strat or '_us_' in strat) and ep > 0:
                        yf_sym = f"{key}.HK" if '_hk_' in strat and not key.upper().endswith('.HK') else key
                        ticker = yf.Ticker(yf_sym)
                        end_dt = datetime.datetime.strptime(snapshot_date[:10], "%Y-%m-%d") + datetime.timedelta(days=1)
                        df = ticker.history(start=entry_date[:10], end=end_dt.strftime("%Y-%m-%d"))
                        if not df.empty and len(df) >= 2:
                            adj_ep = float(df.iloc[0]['Close'])
                            adj_cp = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                            pnl = (adj_cp / adj_ep - 1) - fee
                except Exception as e:
                    print(f"Warning: Failed to fetch adjusted prices for {key}: {e}. Falling back to raw PNL.")
                    
                diff[strat]["removed"].append({"name": key, "entry_price": ep, "exit_price": cp, "pnl": pnl})
                t = {"strategy": strat, "name": key, "entry_date": entry_date, "entry_price": ep, "exit_date": snapshot_date, "exit_price": cp, "pnl": pnl}
                new_trades.append(t)
                sold_today_keys.add(key)
            
            # --- Process ADDED (with T+0 re-entry guard) ---
            for key in added:
                # T+0 guard: if this stock was sold today in the same strategy,
                # do NOT re-buy it. This prevents illegal same-day round-trips for A-shares.
                if key in sold_today_keys and '_a_' in strat:
                    print(f"T+0 GUARD: Skipping re-entry of {key} in {strat} (sold today)")
                    continue
                    
                price = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                diff[strat]["added"].append({"name": key, "entry_price": price})
                new_portfolio[strat][key] = {"entry_date": snapshot_date, "entry_price": price}
                
            # Maintain untouched positions
            for key in (target_keys_set & old_keys):
                new_portfolio[strat][key] = old_portfolio[strat][key]
                
        # Persist transactions safely (Phase 2 Transactional requirement)
        self.db.update_portfolio_and_trades(new_portfolio, new_trades)
        
        return new_portfolio, new_trades, diff
