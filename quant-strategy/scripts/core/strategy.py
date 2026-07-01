from typing import List, Dict, Any
from .market import Market

class Strategy:
    def __init__(self, strat_id: str, market: Market, top_n: int = 3):
        self.strat_id = strat_id
        self.market = market
        self.top_n = top_n
        
    def get_signals(self, **kwargs) -> List[Dict[str, Any]]:
        """
        Generate trading signals (target positions) for this strategy.
        Should be implemented by subclasses.
        Returns a list of dicts, e.g., [{"股票简称": "...", ...}]
        """
        raise NotImplementedError

class ADividendStrategy(Strategy):
    def __init__(self, top_n: int = 3):
        from .market import AShareMarket
        super().__init__("dividend_a_stock", AShareMarket(), top_n)
        
    def get_signals(self, **kwargs) -> List[Dict[str, Any]]:
        df = kwargs.get("df")
        if df is None or df.empty:
            return []
        df = df.sort_values(by="TTM股息率", ascending=False).head(self.top_n)
        return df.to_dict('records')

class AGrowthStrategy(Strategy):
    def __init__(self, top_n: int = 3):
        from .market import AShareMarket
        super().__init__("growth_a_stock", AShareMarket(), top_n)
        
    def get_signals(self, **kwargs) -> List[Dict[str, Any]]:
        df = kwargs.get("df")
        if df is None or df.empty:
            return []
        df = df.sort_values(by="净利润同比增长率", ascending=False)
        return df.to_dict('records')

class USHKQuantStrategy(Strategy):
    def __init__(self, strat_id: str, top_n: int = 3):
        from .market import USMarket, HKMarket
        market = HKMarket() if "_hk_" in strat_id else USMarket()
        super().__init__(strat_id, market, top_n)
        
    def get_signals(self, **kwargs) -> List[Dict[str, Any]]:
        df = kwargs.get("df")
        if df is None or df.empty:
            return []
        
        if 'dividend' in self.strat_id:
            if "TTM股息率" in df.columns:
                df = df.sort_values(by="TTM股息率", ascending=False).head(self.top_n)
            else:
                df = df.head(self.top_n)
        elif 'growth' in self.strat_id:
            if "净利润同比增长率" in df.columns:
                df = df.sort_values(by="净利润同比增长率", ascending=False)
        return df.to_dict('records')

class HotSpotStrategy(Strategy):
    def __init__(self, strat_id: str):
        from .market import AShareMarket, USMarket, HKMarket
        if "_a_" in strat_id:
            market = AShareMarket()
        elif "_hk_" in strat_id:
            market = HKMarket()
        else:
            market = USMarket()
        super().__init__(strat_id, market, top_n=999)
        
    def get_signals(self, **kwargs) -> List[Dict[str, Any]]:
        targets = kwargs.get("target_list", [])
        if targets and isinstance(targets[0], dict):
            return targets
        return [{"股票简称": name} for name in targets]
