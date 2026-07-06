import akshare as ak
import pandas as pd
import json

_SPOT_CACHE = None

def get_spot_cache():
    global _SPOT_CACHE
    if _SPOT_CACHE is None:
        try:
            _SPOT_CACHE = ak.stock_zh_a_spot_em()
        except Exception:
            _SPOT_CACHE = pd.DataFrame()
    return _SPOT_CACHE

def diagnose_elimination(code: str, strategy: str) -> str:
    if "hot_spot" in strategy:
        return "新闻热度衰退或未出现在今日资金抢筹榜单"
        
    if "_a_" not in strategy:
        return "动能衰退或不满足海外/ETF量化量化标准"
        
    try:
        spot = get_spot_cache()
        if spot.empty:
            return "动能衰退或不满足量化筛选标准 (数据源暂时不可用)"
            
        row = spot[spot["代码"] == code]
        if row.empty:
            return "停牌或无交易数据"
            
        row = row.iloc[0]
        
        # Market Cap check
        mc = pd.to_numeric(row.get("总市值", 0), errors="coerce") / 1e8
        if pd.notna(mc) and mc < 100.0:
            return f"总市值缩水至 {mc:.1f} 亿，跌破 100 亿硬性要求"
            
        # Strategy specific
        if "dividend" in strategy:
            pe = pd.to_numeric(row.get("市盈率-动态", 0), errors="coerce")
            pb = pd.to_numeric(row.get("市净率", 0), errors="coerce")
            
            if pd.notna(pe) and pd.notna(pb) and pb != 0:
                val = pe * (pb - 1.0) / pb
                if val >= 10.0:
                    return f"估值公式值({val:.1f}) 超出上限 10.0"
                    
            try:
                from screen_a_share import calculate_ttm_dividend_yield_for_code
                ttm = calculate_ttm_dividend_yield_for_code(code)
                if ttm and ttm < 3.0:
                    return f"TTM股息率({ttm:.2f}%) 跌破 3.0% 阈值"
            except Exception:
                pass
                
        elif "growth" in strategy:
            try:
                from screen_a_share import load_dynamic_cagr_table
                import datetime
                cagr_table = load_dynamic_cagr_table(datetime.date.today(), [code])
                if not cagr_table.empty:
                    cagr_row = cagr_table.iloc[0]
                    cagr = pd.to_numeric(cagr_row.get("3年净利润CAGR", 0), errors="coerce")
                    if pd.notna(cagr) and cagr < 5.0:
                        return f"净利润三年复合增速({cagr:.1f}%) 跌破 5% 要求"
            except Exception:
                pass
                
        return "满足基础硬约束，但相对动能衰退被其他更优标的挤出前 10 名"
        
    except Exception as e:
        return f"不满足策略量化指标"
