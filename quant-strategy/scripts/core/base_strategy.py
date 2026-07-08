from abc import ABC, abstractmethod
import pandas as pd
from typing import List, Dict

class BaseStrategy(ABC):
    """
    Abstract base class for all quantitative strategies.
    Implements the Strategy Factory Pattern to allow plug-and-play of new strategies.
    
    To add a new strategy:
    1. Create a new class inheriting from BaseStrategy.
    2. Implement `fetch_candidates()` to get the raw universe of stocks.
    3. Implement `compute_factors()` to calculate necessary financial metrics.
    4. Implement `llm_filter()` or override `execute()` to define final selection logic.
    """
    
    def __init__(self, strategy_id: str, max_holdings: int = 10):
        self.strategy_id = strategy_id
        self.max_holdings = max_holdings

    @abstractmethod
    def fetch_candidates(self) -> pd.DataFrame:
        """
        Fetch the initial universe of candidates (e.g., all A-shares, or specific sector).
        Returns a DataFrame with at least ['symbol', 'name']
        """
        pass

    @abstractmethod
    def compute_factors(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute quantitative factors for the candidates.
        (e.g., PE, PB, ROE, Dividend Yield, Momentum)
        """
        pass

    @abstractmethod
    def filter_candidates(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Apply hard mechanical filters (e.g., Market Cap > 10B, PE > 0).
        Returns the filtered DataFrame.
        """
        pass

    def llm_filter(self, df: pd.DataFrame, context: str = "") -> List[Dict]:
        """
        Optional: Pass the mechanically filtered top N candidates to an LLM for final selection.
        Returns a list of selected stock dictionaries.
        """
        return df.to_dict(orient='records')[:self.max_holdings]
        
    def execute(self) -> List[Dict]:
        """
        Main pipeline execution.
        """
        print(f"[{self.strategy_id}] Fetching candidates...")
        df = self.fetch_candidates()
        
        print(f"[{self.strategy_id}] Computing factors...")
        df = self.compute_factors(df)
        
        print(f"[{self.strategy_id}] Applying mechanical filters...")
        df = self.filter_candidates(df)
        
        print(f"[{self.strategy_id}] Applying LLM/Final selection...")
        selected = self.llm_filter(df)
        
        print(f"[{self.strategy_id}] Selected {len(selected)} targets.")
        return selected
