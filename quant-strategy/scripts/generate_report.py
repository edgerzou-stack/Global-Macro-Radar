import json
import sys
import os
import base64

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from llm_utils import call_llm
except ImportError:
    call_llm = None

STRAT_NAMES = {
    "dividend_a_stock": "A股核心红利精选",
    "dividend_us_stock": "美股核心红利精选",
    "dividend_hk_stock": "港股核心红利精选",
    "growth_a_stock": "A股高增成长精选",
    "growth_us_stock": "美股高增成长精选",
    "growth_hk_stock": "港股高增成长精选",
    "hot_spot_a_stock": "A股热点突击 (个股)",
    "hot_spot_a_etf": "A股热点突击 (ETF)",
    "hot_spot_us_stock": "美股热点突击 (个股)",
    "hot_spot_us_etf": "美股热点突击 (ETF)",
    "hot_spot_hk_stock": "港股热点突击 (个股)",
    "hot_spot_hk_etf": "港股热点突击 (ETF)",
}

def render_table_md(items, headers):
    if len(items) == 0:
        return "暂无符合条件的标的。\n\n"
    
    res = "| " + " | ".join(headers) + " |\n"
    res += "|" + "|".join(["---"] * len(headers)) + "|\n"
    for row in items:
        cells = []
        for h in headers:
            val = row.get(h)
            if val is None:
                cells.append("")
            elif isinstance(val, float):
                if h == "入选价格" and val <= 0:
                    cells.append("等待开盘")
                else:
                    cells.append(f"{val:.2f}")
            else:
                cells.append(str(val))
        res += "| " + " | ".join(cells) + " |\n"
    return res + "\n\n"

def render_table_html(items, headers):
    if len(items) == 0:
        return "<p>暂无符合条件的标的。</p>\n"
        
    res = "<table>\n"
    res += "  <thead>\n    <tr>\n"
    for h in headers:
        res += f"      <th class='nowrap'>{h}</th>\n"
    res += "    </tr>\n  </thead>\n  <tbody>\n"
    
    for row in items:
        res += "    <tr>\n"
        for h in headers:
            val = row.get(h)
            if val is None:
                cell = ""
            elif isinstance(val, float):
                if h == "入选价格" and val <= 0:
                    cell = "等待开盘"
                else:
                    cell = f"{val:.2f}"
            else:
                cell = str(val)
                
            if h == "累计涨跌幅":
                if cell.startswith("-"):
                    cell = f"<span class='loss'>{cell}</span>"
                elif cell != "0.00%" and cell != "":
                    cell = f"<span class='win'>+{cell}</span>"
            if h in ["股票代码", "股票简称", "买入日期", "卖出日期", "最新价", "入选日期", "入选价格", "累计涨跌幅"]:
                res += f"      <td class='nowrap'>{cell}</td>\n"
            else:
                res += f"      <td>{cell}</td>\n"
        res += "    </tr>\n"
    res += "  </tbody>\n</table>\n"
    return res

def render_history_md(strategy_id, trade_history):
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    if not strat_trades:
        return "暂无历史交割记录。\n\n"
        
    # 修改 P0.5：将 PnL 计算方式从简单累加改为复利计算 (Compounded Returns)
    total_compounded_return = 1.0
    for t in strat_trades:
        total_compounded_return *= (1 + t.get("pnl", 0))
    total_pnl = (total_compounded_return - 1) * 100
    total_pnl_str = f"<span style='color:red'>+{total_pnl:.2f}%</span>" if total_pnl > 0 else f"<span style='color:green'>{total_pnl:.2f}%</span>"
    
    res = f"**历史总净收益：{total_pnl_str}** (共 {len(strat_trades)} 笔交易)\n\n"
    res += "| 股票代码/简称 | 买入日期 | 买入价格 | 卖出日期 | 卖出价格 | 最终盈亏率 | 交割单备注 |\n"
    res += "|---|---|---|---|---|---|---|\n"
    for trade in reversed(strat_trades):
        name = trade.get("name", "")
        in_d = trade.get("entry_date", "")
        in_p = trade.get("entry_price", 0)
        out_d = trade.get("exit_date", "")
        out_p = trade.get("exit_price", 0)
        pnl = trade.get("pnl", 0) * 100
        
        in_p_str = f"{in_p:.2f}" if in_p > 0 else "等待开盘"
        out_p_str = f"{out_p:.2f}" if out_p > 0 else "等待开盘"
        
        if in_p <= 0 or out_p <= 0:
            pnl_str = "N/A"
        else:
            pnl_str = f"<span style='color:red'>+{pnl:.2f}%</span>" if pnl > 0 else f"<span style='color:green'>{pnl:.2f}%</span>"
            
        reason = trade.get("reason", "")
        res += f"| {name} | {in_d} | {in_p_str} | {out_d} | {out_p_str} | {pnl_str} | {reason} |\n"
    return res + "\n\n"

def render_history_html(strategy_id, trade_history):
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    if not strat_trades:
        return "<p>暂无历史交割记录。</p>\n"
        
    # 修改 P0.5：将 PnL 计算方式从简单累加改为复利计算 (Compounded Returns)
    total_compounded_return = 1.0
    for t in strat_trades:
        total_compounded_return *= (1 + t.get("pnl", 0))
    total_pnl = (total_compounded_return - 1) * 100
    total_pnl_str = f"<span class='win'>+{total_pnl:.2f}%</span>" if total_pnl > 0 else f"<span class='loss'>{total_pnl:.2f}%</span>"
    
    res = f"<p><strong>历史总净收益：{total_pnl_str}</strong> (共 {len(strat_trades)} 笔交易)</p>\n"
    res += "<table>\n  <thead>\n    <tr>\n"
    for h in ["股票代码/简称", "买入日期", "买入价格", "卖出日期", "卖出价格", "最终盈亏率", "交割单备注"]:
        res += f"      <th class='nowrap'>{h}</th>\n"
    res += "    </tr>\n  </thead>\n  <tbody>\n"
    
    for trade in reversed(strat_trades):
        name = trade.get("name", "")
        in_d = trade.get("entry_date", "")
        in_p = trade.get("entry_price", 0)
        out_d = trade.get("exit_date", "")
        out_p = trade.get("exit_price", 0)
        pnl = trade.get("pnl", 0) * 100
        
        in_p_str = f"{in_p:.2f}" if in_p > 0 else "等待开盘"
        out_p_str = f"{out_p:.2f}" if out_p > 0 else "等待开盘"
        
        if in_p <= 0 or out_p <= 0:
            pnl_str = "N/A"
        else:
            pnl_cls = "win" if pnl > 0 else "loss" if pnl < 0 else ""
            pnl_sign = "+" if pnl > 0 else ""
            pnl_str = f"<span class='{pnl_cls}'>{pnl_sign}{pnl:.2f}%</span>"
        
        res += f"    <tr>\n"
        res += f"      <td class='nowrap' style='text-align:left'>{name}</td>\n"
        res += f"      <td class='nowrap'>{in_d}</td>\n"
        res += f"      <td class='nowrap'>{in_p_str}</td>\n"
        res += f"      <td class='nowrap'>{out_d}</td>\n"
        res += f"      <td class='nowrap'>{out_p_str}</td>\n"
        res += f"      <td class='nowrap'>{pnl_str}</td>\n"
        res += f"      <td>{trade.get('reason', '')}</td>\n"
        res += f"    </tr>\n"
    res += "  </tbody>\n</table>\n"
    return res

def get_chart_md(chart_name):
    artifact_dir = "/Users/zouzhengting/.gemini/antigravity/brain/cb368359-75c4-4195-b42f-77230af3485d"
    chart_path = os.path.join(artifact_dir, chart_name)
    if os.path.exists(chart_path):
        return f"![{chart_name}]({artifact_dir}/{chart_name})\n\n"
    return ""

def get_chart_html(chart_name, base_dir):
    chart_path = os.path.join(base_dir, "reports", chart_name)
    if not os.path.exists(chart_path):
        artifact_dir = "/Users/zouzhengting/.gemini/antigravity/brain/cb368359-75c4-4195-b42f-77230af3485d"
        chart_path = os.path.join(artifact_dir, chart_name)
        
    if os.path.exists(chart_path):
        with open(chart_path, "rb") as img:
            b64 = base64.b64encode(img.read()).decode("utf-8")
            return f"<div class='chart-container'><img src='data:image/png;base64,{b64}' alt='{chart_name}'></div>\n"
    return ""

def generate_llm_review(strategy_id, results):
    if not results or not call_llm:
        return ""
        
    prompt = f"""作为资深量化基金经理，以下是量化模型今日选出的 Top 10 股票及其核心指标：
{json.dumps(results, ensure_ascii=False)}
请你结合这些公司的基本面常识和行业地位，给出一个简短的质性评价。请以 JSON 格式返回，结构如下：
{{
  "reviews": [
    {{
      "股票代码": "代码",
      "股票简称": "简称",
      "护城河打分": 3.5,
      "成长性打分": 4.2,
      "一句话点评": "点评内容"
    }}
  ],
  "summary": "总结一小段总评"
}}
"""
    
    import time
    max_retries = 3
    base_delay = 5
    
    for attempt in range(max_retries):
        print(f"Generating LLM review for {strategy_id} (Attempt {attempt+1}/{max_retries})...", flush=True)
        try:
            res = call_llm(prompt, require_json=True)
            if res and isinstance(res, dict) and "reviews" in res:
                reviews = res["reviews"]
                # Sort by (护城河打分 + 成长性打分) descending
                for r in reviews:
                    r["合计分"] = float(r.get("护城河打分", 0)) + float(r.get("成长性打分", 0))
                reviews.sort(key=lambda x: x["合计分"], reverse=True)
                
                html = "<div class='llm-review'>\n<h3>🤖 AI 质性点评与打分</h3>\n"
                html += "<table>\n  <thead>\n    <tr>\n      <th>股票代码</th><th>股票简称</th><th>护城河打分(1-5)</th><th>成长性打分(1-5)</th><th>合计分(满分10)</th><th>一句话点评</th>\n    </tr>\n  </thead>\n  <tbody>\n"
                for r in reviews:
                    html += f"    <tr>\n      <td>{r.get('股票代码','')}</td><td>{r.get('股票简称','')}</td><td>{r.get('护城河打分','')}</td><td>{r.get('成长性打分','')}</td><td>{r['合计分']:.1f}</td><td>{r.get('一句话点评','')}</td>\n    </tr>\n"
                html += "  </tbody>\n</table>\n"
                if res.get("summary"):
                    html += f"<p><strong>总评：</strong>{res['summary']}</p>\n"
                html += "</div>\n"
                return html
            else:
                print(f"LLM returned invalid json for {strategy_id}: {res}")
        except Exception as e:
            print(f"Failed to generate LLM review for {strategy_id}: {e}")
            
        if attempt < max_retries - 1:
            sleep_time = base_delay * (2 ** attempt)
            print(f"Rate limited or failed. Retrying {strategy_id} in {sleep_time}s...")
            time.sleep(sleep_time)
            
    return ""

def generate_subsection_md(strategy_id, results, headers, diff, trade_history, llm_review=""):
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    if not results and not strat_trades:
        return ""

    title = STRAT_NAMES.get(strategy_id, strategy_id)
    out = f"### {title}\n\n"
    out += "**当前持仓列表**\n\n"
    out += render_table_md(results, headers)
    
    strat_diff = diff.get(strategy_id, {})
    if strat_diff.get("added") or strat_diff.get("removed"):
        out += f"> **今日调仓提示**：\n"
        if strat_diff.get("added"):
            added_strs = []
            for item in strat_diff["added"]:
                if isinstance(item, dict):
                    ep = item.get('entry_price', 0)
                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    added_strs.append(f"{item['name']} (入选价: {ep_str})")
                else:
                    added_strs.append(str(item))
            out += f"> 🟢 **新增入池**：{', '.join(added_strs)}\n"
        if strat_diff.get("removed"):
            removed_strs = []
            for item in strat_diff["removed"]:
                if isinstance(item, dict):
                    ep = item.get("entry_price", 0)
                    cp = item.get("exit_price", 0)
                    pnl = item.get("pnl", 0) * 100
                    
                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    cp_str = f"{cp:.2f}" if cp > 0 else "等待开盘"
                    pnl_str = f"{pnl:.2f}%" if ep > 0 and cp > 0 else "N/A"
                    
                    removed_strs.append(f"{item['name']} (入选价: {ep_str}, 剔除价: {cp_str}, 盈亏: {pnl_str})")
                else:
                    removed_strs.append(str(item))
            out += f"> 🔴 **掉出观测**：{', '.join(removed_strs)}\n"
        out += "\n\n"
        
    if llm_review:
        out += llm_review

    if strat_trades:
        out += "**历史平仓交割单明细**\n\n"
        out += render_history_md(strategy_id, trade_history)
        out += "**资金净值曲线图**\n\n"
        out += get_chart_md(f"pnl_chart_{strategy_id}.png")
    return out

def generate_subsection_html(strategy_id, results, headers, diff, trade_history, base_dir, llm_review=""):
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    if not results and not strat_trades:
        return ""

    title = STRAT_NAMES.get(strategy_id, strategy_id)
    html = f"<h3>{title}</h3>\n"
    html += "<h4>当前持仓列表</h4>\n"
    html += render_table_html(results, headers)
    
    strat_diff = diff.get(strategy_id, {})
    if strat_diff.get("added") or strat_diff.get("removed"):
        html += f"<div class='alert'>\n  <p><strong>今日调仓提示：</strong></p>\n"
        if strat_diff.get("added"):
            added_strs = []
            for item in strat_diff["added"]:
                if isinstance(item, dict):
                    ep = item.get('entry_price', 0)
                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    added_strs.append(f"{item['name']} (入选价: {ep_str})")
                else:
                    added_strs.append(str(item))
            html += f"  <p>🟢 <strong>新增入池</strong>：{', '.join(added_strs)}</p>\n"
        if strat_diff.get("removed"):
            removed_strs = []
            for item in strat_diff["removed"]:
                if isinstance(item, dict):
                    ep = item.get("entry_price", 0)
                    cp = item.get("exit_price", 0)
                    pnl = item.get("pnl", 0) * 100
                    
                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    cp_str = f"{cp:.2f}" if cp > 0 else "等待开盘"
                    pnl_str = f"{pnl:.2f}%" if ep > 0 and cp > 0 else "N/A"
                    
                    removed_strs.append(f"{item['name']} (入选价: {ep_str}, 剔除价: {cp_str}, 盈亏: {pnl_str})")
                else:
                    removed_strs.append(str(item))
            html += f"  <p>🔴 <strong>掉出观测</strong>：{', '.join(removed_strs)}</p>\n"
        html += "</div>\n"
        
    if llm_review:
        html += f"<div style='margin-top:20px; padding:15px; background-color:#eef2ff; border-radius:8px;'>{llm_review}</div>\n"
    if strat_trades:
        html += "<h4>历史平仓交割单明细</h4>\n"
        html += render_history_html(strategy_id, trade_history)
        html += "<h4>资金净值曲线图</h4>\n"
        html += get_chart_html(f"pnl_chart_{strategy_id}.png", base_dir)
    return html

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 generate_report.py <input_json> <output_md>")
        sys.exit(1)
        
    input_file = sys.argv[1]
    output_md_file = sys.argv[2]
    base_dir = os.path.dirname(input_file)
    output_html_file = os.path.splitext(output_md_file)[0] + ".html"
    
    import db_utils
    
    _, trade_history = db_utils.load_portfolio_and_trades()
    payload = db_utils.load_meta_data("daily_results")
    if not payload:
        print("No daily_results found in database")
        sys.exit(1)
        
    results = payload.get("results", {})
    diff = payload.get("diff", {})

    for strat, items in results.items():
        if not items:
            continue
        if "dividend" in strat:
            items.sort(key=lambda x: float('inf') if x.get("估值公式值") is None else float(x.get("估值公式值")))
        elif "growth" in strat:
            items.sort(key=lambda x: -float('inf') if x.get("净资产收益率") is None else float(x.get("净资产收益率")), reverse=True)
        elif "hot_spot" in strat:
            pass # Keep original sorting which is by turnover

    div_headers = ["股票代码", "股票简称", "PE", "PB", "估值公式值", "TTM股息率", "3年净利润CAGR", "入选日期", "入选价格", "累计涨跌幅"]
    gro_headers = ["股票代码", "股票简称", "PE", "净资产收益率", "营业总收入同比增长率", "净利润同比增长率", "入选日期", "入选价格", "累计涨跌幅"]
    hot_headers = ["股票代码", "股票简称", "最新价", "涨跌幅(%)", "成交额(亿)", "入选日期", "入选价格", "累计涨跌幅", "入选理由"]

    # Pre-generate LLM reviews concurrently for all strategies
    import concurrent.futures
    llm_reviews = {}
    if call_llm:
        strategies_to_review = [
            "dividend_a_stock", "growth_a_stock", "growth_us_stock", "growth_hk_stock",
            "hot_spot_a_stock", "hot_spot_a_etf", "hot_spot_us_stock", "hot_spot_us_etf",
            "hot_spot_hk_stock", "hot_spot_hk_etf"
        ]
        
        def fetch_review(strat):
            res = results.get(strat, [])
            if res:
                return strat, generate_llm_review(strat, res)
            return strat, ""
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_strat = {executor.submit(fetch_review, strat): strat for strat in strategies_to_review}
            for future in concurrent.futures.as_completed(future_to_strat):
                strat, review = future.result()
                llm_reviews[strat] = review
                
    # ================= MARKDOWN GENERATION =================
    out = f"# 每日全球策略量化报告\n\n"

    out += "## 🟢 第一章：稳健红利策略 (基本面护城河)\n\n"
    out += generate_subsection_md("dividend_a_stock", results.get("dividend_a_stock", []), div_headers, diff, trade_history, llm_reviews.get("dividend_a_stock", ""))

    out += "---\n\n## 🔵 第二章：高增成长策略 (基本面护城河)\n\n"
    out += generate_subsection_md("growth_a_stock", results.get("growth_a_stock", []), gro_headers, diff, trade_history, llm_reviews.get("growth_a_stock", ""))
    out += generate_subsection_md("growth_us_stock", results.get("growth_us_stock", []), gro_headers, diff, trade_history, llm_reviews.get("growth_us_stock", ""))
    out += generate_subsection_md("growth_hk_stock", results.get("growth_hk_stock", []), gro_headers, diff, trade_history, llm_reviews.get("growth_hk_stock", ""))

    out += "---\n\n## 🔴 第三章：产业热点战法 (AI 宏观洞察与事件驱动)\n\n"
    out += generate_subsection_md("hot_spot_a_stock", results.get("hot_spot_a_stock", []), hot_headers, diff, trade_history, llm_reviews.get("hot_spot_a_stock", ""))
    out += generate_subsection_md("hot_spot_a_etf", results.get("hot_spot_a_etf", []), hot_headers, diff, trade_history, llm_reviews.get("hot_spot_a_etf", ""))
    out += generate_subsection_md("hot_spot_us_stock", results.get("hot_spot_us_stock", []), hot_headers, diff, trade_history, llm_reviews.get("hot_spot_us_stock", ""))
    out += generate_subsection_md("hot_spot_us_etf", results.get("hot_spot_us_etf", []), hot_headers, diff, trade_history, llm_reviews.get("hot_spot_us_etf", ""))
    out += generate_subsection_md("hot_spot_hk_stock", results.get("hot_spot_hk_stock", []), hot_headers, diff, trade_history, llm_reviews.get("hot_spot_hk_stock", ""))
    out += generate_subsection_md("hot_spot_hk_etf", results.get("hot_spot_hk_etf", []), hot_headers, diff, trade_history, llm_reviews.get("hot_spot_hk_etf", ""))

    out += "---\n\n## 🌟 四、全策略综合对比总结 (Master Chart)\n\n"
    out += get_chart_md("pnl_chart_all.png")
        
    with open(output_md_file, "w", encoding="utf-8") as f:
        f.write(out)
        
    # ================= HTML GENERATION =================
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>每日全球策略量化报告</title>
    <style>
        :root { --bg: #f9fafb; --card: #ffffff; --text: #1f2937; --border: #e5e7eb; --red: #dc2626; --green: #16a34a; }
        body { font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; background: var(--card); padding: 40px; border-radius: 12px; }
        h1 { text-align: center; color: #4f46e5; }
        h2 { border-bottom: 2px solid var(--border); padding-bottom: 10px; margin-top: 40px; }
        h3 { color: #4b5563; border-left: 4px solid #4f46e5; padding-left: 10px; }
        h4 { margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 14px; }
        th, td { padding: 10px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
        td:last-child, th:last-child { white-space: normal; min-width: 300px; }
        .nowrap { white-space: nowrap; }
        th { background: #f3f4f6; position: sticky; top: 0; }
        th:first-child, td:first-child { text-align: left; }
        .win { color: var(--red); font-weight: bold; }
        .loss { color: var(--green); font-weight: bold; }
        .alert { background: #eff6ff; border-left: 4px solid #4f46e5; padding: 15px; margin: 20px 0; }
        .chart-container { text-align: center; margin: 20px 0; }
        .chart-container img { max-width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="container">
        <h1>每日全球策略量化报告</h1>
"""
    
    html += "<h2>🟢 第一章：稳健红利策略 (基本面护城河)</h2>\n"
    html += generate_subsection_html("dividend_a_stock", results.get("dividend_a_stock", []), div_headers, diff, trade_history, base_dir, llm_reviews.get("dividend_a_stock", ""))
    
    html += "<hr>\n<h2>🔵 第二章：高增成长策略 (基本面护城河)</h2>\n"
    html += generate_subsection_html("growth_a_stock", results.get("growth_a_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_a_stock", ""))
    html += generate_subsection_html("growth_us_stock", results.get("growth_us_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_us_stock", ""))
    html += generate_subsection_html("growth_hk_stock", results.get("growth_hk_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_hk_stock", ""))

    html += "<h2>🔴 第三章：产业热点战法 (AI 宏观洞察与事件驱动)</h2>\n"
    html += generate_subsection_html("hot_spot_a_stock", results.get("hot_spot_a_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_a_stock", ""))
    html += generate_subsection_html("hot_spot_a_etf", results.get("hot_spot_a_etf", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_a_etf", ""))
    html += generate_subsection_html("hot_spot_us_stock", results.get("hot_spot_us_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_us_stock", ""))
    html += generate_subsection_html("hot_spot_us_etf", results.get("hot_spot_us_etf", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_us_etf", ""))
    html += generate_subsection_html("hot_spot_hk_stock", results.get("hot_spot_hk_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_hk_stock", ""))
    html += generate_subsection_html("hot_spot_hk_etf", results.get("hot_spot_hk_etf", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_hk_etf", ""))

    html += "<h2>🌟 四、全策略综合对比总结 (Master Chart)</h2>\n"
    html += get_chart_html("pnl_chart_all.png", base_dir)
                
    html += "    </div>\n</body>\n</html>\n"
    with open(output_html_file, "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
