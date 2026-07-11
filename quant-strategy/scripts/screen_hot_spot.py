import os
import re
import json
import requests
import math
import time
# Load .env manually (no dotenv dependency needed)
def _load_env(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass
    env_path = os.environ.get("RADAR_ENV", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "industry-radar", ".env"))
    _load_env(env_path)
import yfinance as yf
from datetime import datetime

# Import A-share fetcher for A-share hot spots
from data_provider import fetch_quote_snapshot_cached
from llm_utils import call_llm

def get_latest_radar_report():
    radar_reports_dir = os.environ.get("RADAR_REPORTS_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "industry-radar", "reports"))
    if not os.path.exists(radar_reports_dir):
        return None
        
    reports = [f for f in os.listdir(radar_reports_dir) if f.endswith(".md")]
    if not reports:
        return None
        
    reports.sort(reverse=True)
    latest_report = os.path.join(radar_reports_dir, reports[0])
    
    # Staleness check: only use if modified today
    import datetime
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(latest_report)).date()
    from core.clock import clock
    if mtime < clock.today():
        print(f"Warning: Latest radar report {latest_report} is from {mtime} (stale).")
        return None
        
    return latest_report

def extract_hot_news(report_file):
    with open(report_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    hot_news = []
    in_hot_section = False
    
    for line in lines:
        if "## 📈 产业焦点与流量狂欢" in line or "## 🔥 高热度产业动向" in line:
            in_hot_section = True
            continue
        if in_hot_section and line.startswith("## "):
            break
            
        if in_hot_section and line.strip() and not line.startswith("---"):
            hot_news.append(line.strip())
            
    return hot_news

def get_hot_stocks_from_llm(news_list, previous_holdings=None):
    prompt = (
        "以下是今天最热门的全球科技产业新闻摘要：\n" + "\n".join(news_list) +
        "\n请你根据这些热点，寻找能够在未来 1-2 年内迎来【极大的业绩反转预期】或【业绩爆发的可能】的核心受益标的。\n"
    )
    if previous_holdings:
        prompt += f"【注意】：以下是昨日该策略的当前持仓标的：{json.dumps(previous_holdings, ensure_ascii=False)}\n如果这些持仓标的【依然符合今天的新闻热点】，请优先在输出中保留它们以降低换手率。但如果它们已经【完全脱离了当前的热点主线】，请毫不犹豫地将它们剔除！不要为了保留而保留！\n"
    prompt += (
        "你拥有全球视野，请分别在A股、美股、港股三个市场中，发散挑选出与该热点强相关的核心概念股。\n" +
        "【极其重要的纪律】：\n" +
        "1. 针对每个热点，你可以在 A股、美股、港股 分别推荐最强相关的个股标的。注意：每个市场的个股推荐数量不能超过 20 只。\n" +
        "2. 极其重要：如果入选的公司所处行业相同（判断标准：两家公司的核心产品线或主要利润来源高度重合），请务必只选出一只最强龙头！同一个细分行业绝对不要重复推荐多家公司！\n" +
        "3. 宁缺毋滥！如果没有强关联度、或者该市场没有相关产业链，该数组必须为空 []！不要硬凑！\n" +
        "4. 产业链纵深发散：当新闻热点涉及某项核心硬件（如AI芯片）时，请务必展现专业投资人的推演能力，自动向高度绑定的上下游（如存储/DRAM/HBM、先进封装、核心算力调度等）发散，并寻找对应的细分行业个股，不要死板局限于新闻字面！\n" +
        "5. 必须输出标准 JSON 格式，包含以下3个数组，结构如下：\n" +
        "{\n" +
        "  \"A_Stock\": [{\"code\": \"600519\", \"reason\": \"与某热点强相关，预计带来业绩反转\"}],  // code为6位纯数字\n" +
        "  \"US_Stock\": [{\"code\": \"AAPL\", \"reason\": \"xxx\"}],   // code为标准美股Ticker\n" +
        "  \"HK_Stock\": [{\"code\": \"0700.HK\", \"reason\": \"xxx\"}] // 港股请加上 .HK 后缀\n" +
        "}\n" +
        "【特别要求】：reason 字段必须用一句话简明扼要地说明为什么选它（结合输入的热点新闻）。除了JSON数据外，不要输出任何其他文本或解释理由。"
    )
        
    res = call_llm(prompt, require_json=True)
    
    # Filter formats
    out = {
        "A_Stock": [{"code": str(c.get("code", "")), "reason": c.get("reason", "")} for c in res.get("A_Stock", []) if isinstance(c, dict) and re.match(r'^(60\d{4}|00\d{4}|30\d{4}|688\d{3}|689\d{3})$', str(c.get("code", "")))] [:20],
        "US_Stock": [{"code": str(c.get("code", "")).upper(), "reason": c.get("reason", "")} for c in res.get("US_Stock", []) if isinstance(c, dict)] [:20],
        "HK_Stock": [{"code": _normalize_hk(str(c.get("code", ""))), "reason": c.get("reason", "")} for c in res.get("HK_Stock", []) if isinstance(c, dict) and str(c.get("code", "")).upper().endswith(".HK")] [:20]
    }
    return out

def _normalize_hk(ticker):
    """Normalize HK ticker: 00700.HK -> 0700.HK, 03033.HK -> 3033.HK"""
    t = str(ticker).upper()
    if t.endswith(".HK"):
        num_part = t[:-3].lstrip("0") or "0"
        # yfinance expects 4-digit HK codes
        num_part = num_part.zfill(4)
        return num_part + ".HK"
    return t

def filter_a_share(items):
    if not items:
        return []
        
    code_to_reason = {item["code"]: item["reason"] for item in items}
    codes = list(code_to_reason.keys())
        
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    url = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    
    def to_secid(code: str) -> str:
        if code.startswith(("60", "68", "51")):
            return "1." + code
        return "0." + code
        
    secids = ",".join(to_secid(code) for code in codes)
    try:
        resp = session.get(url, params={"secids": secids, "fields": "f12,f14,f2,f3,f6,f8,f10,f20,f21"}, timeout=20)
        resp.raise_for_status()
        rows = (resp.json().get("data") or {}).get("diff") or []
    except Exception as e:
        print(f"Error fetching A-share detailed quotes: {e}")
        return []
        
    results = []
    for row in rows:
        code = str(row.get("f12", "")).zfill(6)
        name = row.get("f14", "")
        price = row.get("f2", 0)
        change = row.get("f3", 0)
        turnover_amount = row.get("f6", 0)
        float_market_cap = row.get("f21", 0)
        
        try:
            price = float(price) / 100 if price != "-" else 0
            change = float(change) / 100 if change != "-" else 0
            turnover_amount = float(turnover_amount) if turnover_amount != "-" else 0
            float_market_cap = float(float_market_cap) if float_market_cap != "-" else 1
        except Exception as e:
            import logging
            logging.error(f"Failed to parse A-share data for {code}: {e}", exc_info=True)
            continue
            
        results.append({
            "股票代码": code,
            "股票简称": name,
            "最新价": price,
            "涨跌幅(%)": change,
            "成交额(亿)": round(turnover_amount / 1e8, 2),
            "入选理由": code_to_reason.get(code, "")
        })
            
    results.sort(key=lambda x: x["成交额(亿)"], reverse=True)
    # 活跃度过滤：去掉成交额小于 1 亿的股票
    results = [r for r in results if r["成交额(亿)"] >= 1.0]
    return results

def filter_global(items):
    if not items:
        return []
        
    code_to_reason = {item["code"]: item["reason"] for item in items}
    import concurrent.futures

    def _fetch_one(t, reason):
        try:
            ticker = yf.Ticker(t)
            hist = ticker.history(period="5d").dropna(subset=["Close", "Volume"])
            if len(hist) < 2:
                return None
            
            close = float(hist['Close'].iloc[-1])
            prev_close = float(hist['Close'].iloc[-2])
            volume = float(hist['Volume'].iloc[-1])
            change = (close / prev_close - 1) * 100
            
            turnover = close * volume
                
            info = ticker.info
            name = info.get("shortName", t)
            return {
                "股票代码": t,
                "股票简称": name,
                "最新价": round(close, 2),
                "涨跌幅(%)": round(change, 2),
                "成交额(亿)": round(turnover / 1e8, 2),
                "入选理由": reason
            }
        except Exception as e:
            print(f"Failed to fetch {t}: {e}")
            return None

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_fetch_one, t, reason) for t, reason in code_to_reason.items()]
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result(timeout=20)
                if res:
                    results.append(res)
            except concurrent.futures.TimeoutError:
                print("Timeout fetching a global hot spot stock.")
            
    results.sort(key=lambda x: x["成交额(亿)"], reverse=True)
    # 活跃度过滤：去掉成交额小于 1 亿的股票
    results = [r for r in results if r["成交额(亿)"] >= 1.0]
    return results

def main():
    report_file = get_latest_radar_report()
    if not report_file:
        print("No radar report found.")
        return
        
    print(f"Reading radar report: {report_file}")
    hot_news = extract_hot_news(report_file)
    if not hot_news:
        print("No high-traffic news found.")
        return
        
    print(f"Found {len(hot_news)} high-traffic news items. Querying LLM...")
    
    # Extract previous holdings to anchor the LLM
    previous_holdings = {}
    from config import PROJECT_ROOT
    global_screen_path = os.path.join(PROJECT_ROOT, "global_screen.json")
    if os.path.exists(global_screen_path):
        try:
            with open(global_screen_path, "r", encoding="utf-8") as f:
                gd = json.load(f)
            port = gd.get("portfolio", {})
            for k in ["hot_spot_a_stock", "hot_spot_us_stock", "hot_spot_hk_stock"]:
                if k in port and port[k]:
                    previous_holdings[k] = list(port[k].keys())
        except Exception as e:
            import logging
            logging.error(f"Failed to load previous holdings from global_screen: {e}", exc_info=True)
            
    llm_pools = get_hot_stocks_from_llm(hot_news, previous_holdings)
    print(f"LLM suggested pools: {llm_pools}")
    
    final_output = {}
    
    # Process A shares
    final_output["hot_spot_a_stock"] = filter_a_share(llm_pools.get("A_Stock", []))
    
    # Process Global shares
    final_output["hot_spot_us_stock"] = filter_global(llm_pools.get("US_Stock", []))
    final_output["hot_spot_hk_stock"] = filter_global(llm_pools.get("HK_Stock", []))
    
    def rank_top_10_via_llm(category_name, candidates, hot_news, previous_holdings):
        if len(candidates) <= 10:
            return candidates
        
        print(f"Ranking top 10 for {category_name} out of {len(candidates)} candidates...")
        prompt = (
            "你是一个顶尖的游资/宏观对冲基金经理。\n"
            "以下是今天的核心产业热点新闻：\n" + "\n".join(hot_news) + "\n\n"
            "以下是初筛选出的相关概念股以及它们的今日市场表现数据：\n" + json.dumps(candidates, ensure_ascii=False) + "\n\n"
        )
        cat_holdings = previous_holdings.get(category_name, [])
        if cat_holdings:
            prompt += f"【注意】：以下是昨日该策略的持仓标的：{json.dumps(cat_holdings, ensure_ascii=False)}\n为了保证策略稳定性，如果新闻热点依然和它们高度相关，请优先保留它们以降低换手率。\n\n"
        prompt += (
            "请结合它们与新闻的绝对相关度、以及今天的市场活跃度（成交额越大越好、表现强势），精挑细选出最核心的最多 16 只龙头股，并务必【按相关度从高到低排序】。\n"
            "如果值得买的不足 16 只，宁缺毋滥，只返回值得买的几只即可。\n"
            "请严格返回一个 JSON 数组，只包含你选出的股票的「股票代码」，并按优先级排序（如 [\"600519\", \"AAPL\", \"0700.HK\"]）。不要输出任何其他文本或解释理由。"
        )
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                res = call_llm(prompt, require_json=True)
                if isinstance(res, list):
                    selected_codes = [str(c) for c in res]
                else:
                    # Attempt regex if json extraction yielded a dict or something weird
                    res_str = json.dumps(res) if isinstance(res, dict) else str(res)
                    selected_codes = re.findall(r'([A-Za-z0-9.]+)', res_str)
                    
                cand_map = {str(c.get("股票代码", "")): c for c in candidates}
                filtered = []
                for idx, code in enumerate(selected_codes):
                    if code not in cand_map:
                        continue
                        
                    if len(filtered) < 10:
                        filtered.append(cand_map[code])
                    else:
                        # 出池缓冲机制 (Buffer Zone): 跌出前 10，但在前 16 名内的原持仓予以保留
                        if code in cat_holdings:
                            print(f"BUFFER ZONE RETAIN: {code} ranked {idx+1}, kept to reduce turnover.")
                            filtered.append(cand_map[code])
                            
                    if len(filtered) >= 16:
                        break
                
                if not filtered:
                    print(f"Warning: LLM returned no matching valid codes for {category_name}.")
                    # As requested, strictly follow LLM even if it returns 0.
                    return []
                
                return filtered
                
            except Exception as e:
                print(f"Attempt {attempt + 1}/{max_retries} failed for {category_name}: {e}")
                if attempt < max_retries - 1:
                    print("Waiting 10 seconds before retrying...")
                    time.sleep(10)
                else:
                    print(f"All {max_retries} attempts failed. Throwing error.")
                    raise RuntimeError(f"Failed to rank {category_name} via LLM after {max_retries} attempts.") from e
            
    for k in ["hot_spot_a_stock", "hot_spot_us_stock", "hot_spot_hk_stock"]:
        final_output[k] = rank_top_10_via_llm(k, final_output.get(k, []), hot_news, previous_holdings)
        
    from config import PROJECT_ROOT
    output_path = os.path.join(PROJECT_ROOT, "hot_spot_today.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_output, f, ensure_ascii=False, indent=2)
        
    print(f"Saved global hot spot stocks to {output_path}")

if __name__ == "__main__":
    main()
