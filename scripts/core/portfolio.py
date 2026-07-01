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
                
        if time_val < 9.5:
            return valid_price(prices_dict.get("昨收"), latest)
        elif 9.5 <= time_val < close_time:
            return valid_price(prices_dict.get("今开"), latest)
        else:
            return valid_price(latest, 0)
            
    def diff_and_update(self, strategy_targets: Dict[str, List[str]], current_prices: Dict[str, Any], snapshot_date: str):
        """
        Compares the new target positions with the old portfolio to calculate trades.
        Then atomically updates the database.
        
        strategy_targets: { strat_id: [stock_code1, stock_code2, ...] }
        current_prices: { stock_code: {"最新价": 100, "今开": 99, ...} }
        """
        old_portfolio, _ = self.db.load_portfolio_and_trades()
        new_portfolio = {s: {} for s in strategy_targets.keys()}
        new_trades = []
        diff = {s: {"added": [], "removed": []} for s in strategy_targets.keys()}
        
        for strat, target_keys in strategy_targets.items():
            target_keys_set = set(target_keys)
            old_keys = set(old_portfolio.get(strat, {}).keys())
            
            added = target_keys_set - old_keys
            removed = old_keys - target_keys_set
            
            for key in added:
                price = self.get_simulated_trade_price(current_prices.get(key, {}), strat)
                diff[strat]["added"].append({"name": key, "entry_price": price})
                new_portfolio[strat][key] = {"entry_date": snapshot_date, "entry_price": price}
                
            for key in removed:
                ep = old_portfolio[strat].get(key, {}).get("entry_price", 0)
                entry_date = old_portfolio[strat].get(key, {}).get("entry_date", "未知")
                
                from core.clock import clock
                now_local = clock.now(pytz.timezone("Asia/Shanghai"))
                if now_local.hour < 9 or (now_local.hour == 9 and now_local.minute < 30):
                    effective_today = (now_local - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
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
                        yf_sym = f"{key}.HK" if '_hk_' in strat else key
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
                
            # Maintain untouched positions
            for key in (target_keys_set & old_keys):
                new_portfolio[strat][key] = old_portfolio[strat][key]
                
        # Persist transactions safely (Phase 2 Transactional requirement)
        self.db.update_portfolio_and_trades(new_portfolio, new_trades)
        
        return new_portfolio, new_trades, diff
