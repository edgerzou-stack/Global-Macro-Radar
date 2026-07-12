import pytest
import os
import shutil

@pytest.fixture(autouse=True)
def offline_data_gateway(monkeypatch):
    from core.data_gateway import DataGateway
    
    # 1. Override the DB path to point to the frozen DB
    frozen_db_path = os.path.join(os.path.dirname(__file__), "test_data", "frozen_market_cache.db")
    
    if not os.path.exists(frozen_db_path):
        pytest.skip(f"Frozen DB not found at {frozen_db_path}. Run script to generate it.")
        
    # We shouldn't modify the real config, so we monkeypatch DataGateway.__init__ to use it
    original_init = DataGateway.__init__
    
    def mock_init(self, cache_db=frozen_db_path, disable_api=True):
        original_init(self, cache_db=cache_db)
        # 2. Hard-block all API fetches so if cache misses, it fails loudly
        self._fetch_from_baostock = lambda *args, **kwargs: (_ for _ in ()).throw(Exception("OFFLINE MODE: API Call to Baostock blocked!"))
        self._fetch_from_sina = lambda *args, **kwargs: (_ for _ in ()).throw(Exception("OFFLINE MODE: API Call to Sina blocked!"))
        self._fetch_from_yfinance = lambda *args, **kwargs: (_ for _ in ()).throw(Exception("OFFLINE MODE: API Call to YFinance blocked!"))
        self._fetch_from_em = lambda *args, **kwargs: (_ for _ in ()).throw(Exception("OFFLINE MODE: API Call to EM blocked!"))
        self._fetch_from_akshare = lambda *args, **kwargs: (_ for _ in ()).throw(Exception("OFFLINE MODE: API Call to Akshare blocked!"))
        self._fetch_from_fmp = lambda *args, **kwargs: (_ for _ in ()).throw(Exception("OFFLINE MODE: API Call to FMP blocked!"))
        self.disable_api = disable_api
        
    monkeypatch.setattr(DataGateway, "__init__", mock_init)
    
    yield
