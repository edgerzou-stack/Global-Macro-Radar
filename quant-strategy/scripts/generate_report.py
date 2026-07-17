import json
import sys
import os
import base64
from core.diagnose import diagnose_elimination
from core.quarantine import quarantine_filter

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from llm_utils import call_llm
except ImportError:
    call_llm = None
try:
    from get_stock_name import get_stock_name
except ImportError:
    get_stock_name = lambda x: x

STRAT_NAMES = {
    "dividend_a_stock": "A股核心红利精选",
    "dividend_us_stock": "美股核心红利精选",
    "dividend_hk_stock": "港股核心红利精选",
    "growth_a_stock": "A股高增成长精选",
    "growth_us_stock": "美股高增成长精选",
    "growth_hk_stock": "港股高增成长精选",
    "hot_spot_a_stock": "A股热点突击 (个股)",
    "hot_spot_us_stock": "美股热点突击 (个股)",
    "hot_spot_hk_stock": "港股热点突击 (个股)",
}

STRAT_REASONS = {
    "dividend_a_stock": "红利避险 (高股息与稳定分红)",
    "dividend_us_stock": "红利避险 (高股息与稳定分红)",
    "dividend_hk_stock": "红利避险 (高股息与稳定分红)",
    "growth_a_stock": "高增成长 (营收利润连续增长及动量)",
    "growth_us_stock": "高增成长 (营收利润连续增长及动量)",
    "growth_hk_stock": "高增成长 (营收利润连续增长及动量)",
    "hot_spot_a_stock": "热点突击 (新闻突发热度及资金流向)",
    "hot_spot_us_stock": "热点突击 (新闻突发热度及资金流向)",
    "hot_spot_hk_stock": "热点突击 (新闻突发热度及资金流向)",
}


def load_active_strategy_accounts(db_path=None):
    import sqlite3
    from core.cash_manager import get_db_path

    conn = sqlite3.connect(db_path or get_db_path())
    try:
        account_filter, account_parameters, _ = quarantine_filter(
            conn, "strategy_accounts"
        )
        return conn.execute(
            "SELECT strategy_id, total_capital, available_cash "
            "FROM strategy_accounts WHERE 1=1"
            + account_filter
            + " ORDER BY strategy_id",
            account_parameters,
        ).fetchall()
    finally:
        conn.close()

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
            elif isinstance(val, (float, int)):
                if h == "入选价格" and val <= 0:
                    cells.append("等待开盘")
                else:
                    cells.append(f"{float(val):.2f}")
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
            elif isinstance(val, (float, int)):
                if h == "入选价格" and val <= 0:
                    cell = "等待开盘"
                else:
                    cell = f"{float(val):.2f}"
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

def render_history_md(strategy_id, trade_history, code_map=None):
    if code_map is None:
        code_map = {}
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    if not strat_trades:
        return "暂无历史交割记录。\n\n"

    res = f"**历史平仓交易明细** (共 {len(strat_trades)} 笔已完成交割)\n\n"
    res += "| 股票代码/简称 | 买入日期 | 均价 | 投入份数 | 卖出日期 | 卖出价格 | 最终盈亏率 | 交割单备注 |\n"
    res += "|---|---|---|---|---|---|---|---|\n"
    for trade in reversed(strat_trades):
        code = trade.get("name", "")
        name = code_map.get(str(code), code)
        if name == code:
            name = get_stock_name(code)

        if name != code:
            display_name = f"{code} ({name})"
        else:
            display_name = code

        in_d = trade.get("entry_date", "")
        in_p = trade.get("entry_price", 0)
        shares = trade.get("shares", 1)
        out_d = trade.get("exit_date", "")
        out_p = trade.get("exit_price", 0)
        pnl = trade.get("pnl", 0) * 100

        in_p_str = f"{in_p:.2f}" if in_p > 0 else "等待开盘"
        out_p_str = f"{out_p:.2f}" if out_p > 0 else "等待开盘"

        if in_p <= 0 or out_p <= 0:
            pnl_str = "N/A"
        else:
            pnl_str = f"<span style='color:red'>+{pnl:.2f}%</span>" if pnl > 0 else f"<span style='color:green'>{pnl:.2f}%</span>"

        reason = trade.get("reason")
        if reason is None or str(reason).strip() == "" or str(reason).strip().lower() == "none":
            reason = "-"

        res += f"| {display_name} | {in_d} | {in_p_str} | {shares} | {out_d} | {out_p_str} | {pnl_str} | {reason} |\n"
    return res + "\n\n"

def render_history_html(strategy_id, trade_history, code_map=None):
    if code_map is None:
        code_map = {}
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    if not strat_trades:
        return "<p>暂无历史交割记录。</p>\n"

    res = f"<p><strong>历史平仓交易明细</strong> (共 {len(strat_trades)} 笔已完成交割)</p>\n"
    res += "<table>\n  <thead>\n    <tr>\n"
    for h in ["股票代码/简称", "买入日期", "均价", "投入份数", "卖出日期", "卖出价格", "最终盈亏率", "交割单备注"]:
        res += f"      <th class='nowrap'>{h}</th>\n"
    res += "    </tr>\n  </thead>\n  <tbody>\n"

    for trade in reversed(strat_trades):
        code = trade.get("name", "")
        name = code_map.get(str(code), code)
        if name == code:
            name = get_stock_name(code)

        if name != code:
            display_name = f"{code} ({name})"
        else:
            display_name = code

        in_d = trade.get("entry_date", "")
        in_p = trade.get("entry_price", 0)
        shares = trade.get("shares", 1)
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

        reason = trade.get("reason")
        if reason is None or str(reason).strip() == "" or str(reason).strip().lower() == "none":
            reason = "-"

        res += f"    <tr>\n"
        res += f"      <td class='nowrap' style='text-align:left'>{display_name}</td>\n"
        res += f"      <td class='nowrap'>{in_d}</td>\n"
        res += f"      <td class='nowrap'>{in_p_str}</td>\n"
        res += f"      <td class='nowrap'>{shares}</td>\n"
        res += f"      <td class='nowrap'>{out_d}</td>\n"
        res += f"      <td class='nowrap'>{out_p_str}</td>\n"
        res += f"      <td class='nowrap'>{pnl_str}</td>\n"
        res += f"      <td>{reason}</td>\n"
        res += f"    </tr>\n"
    res += "  </tbody>\n</table>\n"
    return res

def get_chart_md(chart_name, base_dir):
    chart_path = os.path.join(base_dir, "reports", chart_name)
    if not os.path.exists(chart_path):
        chart_path = os.path.join(base_dir, chart_name)
    if os.path.exists(chart_path):
        return f"![{chart_name}]({chart_path})\n\n"
    return ""

def get_chart_html(chart_name, base_dir):
    chart_path = os.path.join(base_dir, "reports", chart_name)
    if not os.path.exists(chart_path):
        chart_path = os.path.join(base_dir, chart_name)

    if os.path.exists(chart_path):
        with open(chart_path, "rb") as img:
            b64 = base64.b64encode(img.read()).decode("utf-8")
            return f"<div class='chart-container'><img src='data:image/png;base64,{b64}' alt='{chart_name}'></div>\n"
    return ""

def generate_batch_llm_reviews(strategies_dict):
    if not strategies_dict or not call_llm:
        return {}

    # Slim down payload to save tokens
    slim_payload = {}
    for strat, items in strategies_dict.items():
        if not items: continue
        slim_items = []
        for item in items[:10]: # Only top 10
            slim_items.append({
                "代码": item.get("股票代码", ""),
                "简称": item.get("股票简称", ""),
                "行业": item.get("所属行业", ""),
                "市值": item.get("总市值", item.get("总市值(元)", "")),
                "PE": item.get("PE", item.get("市盈率(TTM)", "")),
                "最新价": item.get("最新价", "")
            })
        if slim_items:
            slim_payload[strat] = slim_items

    if not slim_payload:
        return {}

    prompt = f"""作为资深量化基金经理，以下是各大子策略今日选出的 Top 10 股票核心指标：
{json.dumps(slim_payload, ensure_ascii=False)}

请结合基本面常识，为每个策略分别给出质性评价。请严格以 JSON 格式返回，结构如下：
{{
  "strategy_reviews": {{
    "strategy_name": {{
      "reviews": [
        {{
          "股票代码": "代码",
          "股票简称": "简称",
          "护城河打分": 3.5,
          "成长性打分": 4.2,
          "一句话点评": "极短点评内容"
        }}
      ],
      "summary": "该策略总结"
    }}
  }}
}}
"""

    import time
    max_retries = 3
    base_delay = 5

    for attempt in range(max_retries):
        print(f"Generating LLM batch reviews (Attempt {attempt+1}/{max_retries})...", flush=True)
        try:
            res = call_llm(prompt, require_json=True)
            if res and isinstance(res, dict) and "strategy_reviews" in res:
                html_outputs = {}
                for strat, strat_data in res["strategy_reviews"].items():
                    reviews = strat_data.get("reviews", [])
                    for r in reviews:
                        r["合计分"] = float(r.get("护城河打分", 0)) + float(r.get("成长性打分", 0))
                    reviews.sort(key=lambda x: x.get("合计分", 0), reverse=True)

                    html = "<div class='llm-review'>\n<h3>🤖 AI 质性点评与打分</h3>\n"
                    html += "<table>\n  <thead>\n    <tr>\n      <th>股票代码</th><th>股票简称</th><th>护城河打分(1-5)</th><th>成长性打分(1-5)</th><th>合计分(满分10)</th><th>一句话点评</th>\n    </tr>\n  </thead>\n  <tbody>\n"
                    for r in reviews:
                        html += f"    <tr>\n      <td>{r.get('股票代码','')}</td><td>{r.get('股票简称','')}</td><td>{r.get('护城河打分','')}</td><td>{r.get('成长性打分','')}</td><td>{r.get('合计分',0):.1f}</td><td>{r.get('一句话点评','')}</td>\n    </tr>\n"
                    html += "  </tbody>\n</table>\n"
                    if strat_data.get("summary"):
                        html += f"<p><strong>总评：</strong>{strat_data['summary']}</p>\n"
                    html += "</div>\n"
                    html_outputs[strat] = html
                return html_outputs
            else:
                print(f"LLM returned invalid json for batch: {res}")
        except Exception as e:
            print(f"Failed to generate LLM batch reviews: {e}")

        if attempt < max_retries - 1:
            time.sleep(base_delay * (2 ** attempt))

    return {}

def generate_subsection_md(strategy_id, results, headers, diff, trade_history, base_dir, llm_review="", code_map=None, appendix_results=None):
    if appendix_results is None:
        appendix_results = []
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    title = STRAT_NAMES.get(strategy_id, strategy_id)
    if not results and not strat_trades and not appendix_results:
        return f"### {title}\n\n**当前持仓列表**\n\n暂无符合条件的标的，且暂无历史平仓交割单。\n\n"

    title = STRAT_NAMES.get(strategy_id, strategy_id)
    out = f"### {title}\n\n"
    out += "**当前持仓列表**\n\n"
    if results:
        out += render_table_md(results, headers)
    else:
        out += "暂无持仓。\n\n"

    if appendix_results:
        out += "**备选池 (Appendix)**\n\n"
        out += render_table_md(appendix_results, headers)

    strat_diff = diff.get(strategy_id, {})
    if strat_diff.get("added") or strat_diff.get("removed"):
        out += f"> **今日调仓提示**：\n"
        if strat_diff.get("added"):
            new_pool = []
            grid_adds = []
            for item in strat_diff["added"]:
                if isinstance(item, dict):
                    ep = item.get('entry_price', 0)
                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    code = str(item['name'])
                    name = code_map.get(code, code) if code_map else code
                    if name == code:
                        name = get_stock_name(code)
                    display_name = f"{code} ({name})" if name != code else code

                    item_reason = item.get("reason", "")
                    if "网格加仓" in str(item_reason):
                        grid_adds.append(f"{display_name} (加仓价: {ep_str}, 原因: {item_reason})")
                    else:
                        strat_reason = STRAT_REASONS.get(strategy_id, "策略量化指标")
                        new_pool.append(f"{display_name} (入选价: {ep_str}, 原因: 满足【{strat_reason}】入选标准)")
                else:
                    new_pool.append(str(item))
            if new_pool:
                out += f"> 🟢 **新增入池**：{', '.join(new_pool)}\n"
            if grid_adds:
                out += f"> 🔵 **网格加仓**：{', '.join(grid_adds)}\n"
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

                    code = str(item['name'])
                    name = code_map.get(code, code) if code_map else code
                    if name == code:
                        name = get_stock_name(code)
                    display_name = f"{code} ({name})" if name != code else code

                    specific_reason = diagnose_elimination(code, strategy_id)
                    removed_strs.append(f"{display_name} (入选价: {ep_str}, 剔除价: {cp_str}, 盈亏: {pnl_str}, 原因: {specific_reason})")
                else:
                    removed_strs.append(str(item))
            out += f"> 🔴 **掉出观测**：{', '.join(removed_strs)}\n"
        out += "\n\n"

    if llm_review:
        out += llm_review

    out += "**历史平仓交割单明细**\n\n"
    out += render_history_md(strategy_id, trade_history, code_map)

    out += "**资金净值曲线图**\n\n"
    out += get_chart_md(f"pnl_chart_{strategy_id}.png", base_dir)
    return out

def generate_subsection_html(strategy_id, results, headers, diff, trade_history, base_dir, llm_review="", code_map=None, appendix_results=None):
    if appendix_results is None:
        appendix_results = []
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    title = STRAT_NAMES.get(strategy_id, strategy_id)
    if not results and not strat_trades and not appendix_results:
        return f"<h3>{title}</h3>\n<h4>当前持仓列表</h4>\n<p>暂无符合条件的标的，且暂无历史平仓交割单。</p>\n"

    title = STRAT_NAMES.get(strategy_id, strategy_id)
    html = f"<h3>{title}</h3>\n"
    html += "<h4>当前持仓列表</h4>\n"
    if results:
        html += render_table_html(results, headers)
    else:
        html += "<p>暂无持仓。</p>\n"

    if appendix_results:
        html += "<h4>备选池 (Appendix)</h4>\n"
        html += render_table_html(appendix_results, headers)

    strat_diff = diff.get(strategy_id, {})
    if strat_diff.get("added") or strat_diff.get("removed"):
        html += f"<div class='alert'>\n  <p><strong>今日调仓提示：</strong></p>\n"
        if strat_diff.get("added"):
            new_pool = []
            grid_adds = []
            for item in strat_diff["added"]:
                if isinstance(item, dict):
                    ep = item.get('entry_price', 0)
                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    code = str(item['name'])
                    name = code_map.get(code, code) if code_map else code
                    if name == code:
                        name = get_stock_name(code)
                    display_name = f"{code} ({name})" if name != code else code

                    item_reason = item.get("reason", "")
                    if "网格加仓" in str(item_reason):
                        grid_adds.append(f"{display_name} (加仓价: {ep_str}, 原因: {item_reason})")
                    else:
                        strat_reason = STRAT_REASONS.get(strategy_id, "策略量化指标")
                        new_pool.append(f"{display_name} (入选价: {ep_str}, 原因: 满足【{strat_reason}】入选标准)")
                else:
                    new_pool.append(str(item))
            if new_pool:
                html += f"  <p>🟢 <strong>新增入池</strong>：{', '.join(new_pool)}</p>\n"
            if grid_adds:
                html += f"  <p>🔵 <strong>网格加仓</strong>：{', '.join(grid_adds)}</p>\n"
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
                    pnl_cls = "win" if pnl > 0 else "loss" if pnl < 0 else ""
                    pnl_sign = "+" if pnl > 0 else ""

                    code = str(item['name'])
                    name = code_map.get(code, code) if code_map else code
                    if name == code:
                        name = get_stock_name(code)
                    display_name = f"{code} ({name})" if name != code else code

                    specific_reason = diagnose_elimination(code, strategy_id)
                    removed_strs.append(f"{display_name} (入选价: {ep_str}, 剔除价: {cp_str}, <span class='{pnl_cls}'>盈亏: {pnl_sign}{pnl_str}</span>, 原因: {specific_reason})")
                else:
                    removed_strs.append(str(item))
            html += f"  <p>🔴 <strong>掉出观测</strong>：{', '.join(removed_strs)}</p>\n"
        html += "</div>\n"

    if llm_review:
        html += f"<div style='margin-top:20px; padding:15px; background-color:#eef2ff; border-radius:8px;'>{llm_review}</div>\n"
    html += "<h4>历史平仓交割单明细</h4>\n"
    html += render_history_html(strategy_id, trade_history, code_map)
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

    portfolio, trade_history = db_utils.load_portfolio_and_trades()
    payload = db_utils.load_latest_daily_results()
    if not payload:
        print("No daily_results found in strategy_daily_results table")
        sys.exit(1)

    results = payload.get("results", {})
    diff = payload.get("diff", {})
    appendix = payload.get("appendix", {})

    # --- Reconcile JSON payload with true SQLite DB state ---
    snapshot_date = payload.get("snapshot_date", "1970-01-01")[:10]

    for strat, items in diff.items():
        if "added" in items:
            for item in items["added"]:
                if isinstance(item, dict):
                    code = str(item.get("name"))
                    if strat in portfolio and code in portfolio[strat]:
                        item["entry_price"] = portfolio[strat][code].get("entry_price", 0)
        if "removed" in items:
            for item in items["removed"]:
                if isinstance(item, dict):
                    code = str(item.get("name"))
                    for t in reversed(trade_history):
                        if t["strategy"] == strat and str(t["name"]) == code and str(t["exit_date"]).startswith(snapshot_date):
                            item["entry_price"] = t["entry_price"]
                            item["exit_price"] = t["exit_price"]
                            item["pnl"] = t["pnl"]
                            break

    for strat, items in results.items():
        if not items: continue
        for item in items:
            code = str(item.get("股票代码"))
            if strat in portfolio and code in portfolio[strat]:
                item["入选价格"] = portfolio[strat][code].get("entry_price", 0)
    # --- End Reconciliation ---

    for strat, items in results.items():
        if not items:
            continue
        if "dividend" in strat:
            items.sort(key=lambda x: float('inf') if x.get("估值公式值") is None else float(x.get("估值公式值")))
        elif "growth" in strat:
            items.sort(key=lambda x: -float('inf') if x.get("净资产收益率") is None else float(x.get("净资产收益率")), reverse=True)
        elif "hot_spot" in strat:
            pass # Keep original sorting which is by turnover

    div_headers = ["股票代码", "股票简称", "PE", "PB", "估值公式值", "TTM股息率", "3年净利润CAGR", "入选日期", "入选价格", "仓位份数", "累计涨跌幅"]
    gro_headers = ["股票代码", "股票简称", "PE", "净资产收益率", "营业总收入同比增长率", "净利润同比增长率", "入选日期", "入选价格", "仓位份数", "累计涨跌幅"]
    hot_headers = ["股票代码", "股票简称", "最新价", "涨跌幅(%)", "成交额(亿)", "入选日期", "入选价格", "仓位份数", "累计涨跌幅", "入选理由"]

    # Pre-generate LLM batch review for all strategies at once to save tokens and threads
    llm_reviews = {}
    if call_llm:
        strategies_to_review = [
            "dividend_a_stock", "growth_a_stock", "growth_us_stock", "growth_hk_stock",
            "hot_spot_a_stock", "hot_spot_us_stock", "hot_spot_hk_stock"
        ]

        batch_input = {}
        for strat in strategies_to_review:
            if results.get(strat):
                batch_input[strat] = results[strat]

        if batch_input:
            llm_reviews = generate_batch_llm_reviews(batch_input)

    code_map = {}
    for strat, items in results.items():
        for item in items:
            code = item.get("股票代码")
            name = item.get("股票简称")
            if code and name:
                code_map[str(code)] = str(name)

    try:
        from screen_a_share import load_code_name_table
        a_share_df = load_code_name_table()
        for _, row in a_share_df.iterrows():
            code_map[str(row["股票代码"])] = str(row["股票简称"])
    except Exception as e:
        print(f"Warning: Could not load A-share code map: {e}")

    def get_cash_overview_md():
        try:
            rows = load_active_strategy_accounts()
            if not rows: return ""

            md = "## 🏦 全球多策略子基金台账概览 (Sandbox Benchmark Engine)\n\n"
            md += "| 策略沙盒 (Strategy) | 当前总净值 (NAV) | 当前可用现金 (Cash) | 资金利用率 |\n"
            md += "| --- | --- | --- | --- |\n"
            for row in rows:
                sid, cap, cash = row
                util = ((cap - cash) / cap) * 100 if cap > 0 else 0
                md += f"| `{sid}` | ¥{cap:,.2f} | ¥{cash:,.2f} | {util:.1f}% |\n"
            md += "\n---\n\n"
            return md
        except Exception as e:
            return f"<!-- Failed to load cash overview: {e} -->\n"

    def get_cash_overview_html():
        try:
            rows = load_active_strategy_accounts()
            if not rows: return ""

            html = "<h2>🏦 全球多策略子基金台账概览 (Sandbox Benchmark Engine)</h2>\n"
            html += "<table>\n<thead><tr><th>策略沙盒 (Strategy)</th><th>当前总净值 (NAV)</th><th>当前可用现金 (Cash)</th><th>资金利用率</th></tr></thead>\n<tbody>\n"
            for row in rows:
                sid, cap, cash = row
                util = ((cap - cash) / cap) * 100 if cap > 0 else 0
                html += f"<tr><td><code>{sid}</code></td><td>¥{cap:,.2f}</td><td>¥{cash:,.2f}</td><td>{util:.1f}%</td></tr>\n"
            html += "</tbody></table>\n<hr>\n"
            return html
        except Exception as e:
            return ""

    # ================= MARKDOWN GENERATION =================
    out = f"# 每日全球策略量化报告\n\n"
    out += get_cash_overview_md()

    out += "## 🟢 第一章：稳健红利策略 (基本面护城河)\n\n"
    out += generate_subsection_md("dividend_a_stock", results.get("dividend_a_stock", []), div_headers, diff, trade_history, base_dir, llm_reviews.get("dividend_a_stock", ""), code_map, appendix.get("dividend_a_stock", []))

    out += "---\n\n## 🔵 第二章：高增成长策略 (基本面护城河)\n\n"
    out += generate_subsection_md("growth_a_stock", results.get("growth_a_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_a_stock", ""), code_map, appendix.get("growth_a_stock", []))
    out += generate_subsection_md("growth_us_stock", results.get("growth_us_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_us_stock", ""), code_map, appendix.get("growth_us_stock", []))
    out += generate_subsection_md("growth_hk_stock", results.get("growth_hk_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_hk_stock", ""), code_map, appendix.get("growth_hk_stock", []))

    out += "---\n\n## 🔴 第三章：产业热点战法 (AI 宏观洞察与事件驱动)\n\n"
    out += generate_subsection_md("hot_spot_a_stock", results.get("hot_spot_a_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_a_stock", ""), code_map, appendix.get("hot_spot_a_stock", []))
    out += generate_subsection_md("hot_spot_us_stock", results.get("hot_spot_us_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_us_stock", ""), code_map, appendix.get("hot_spot_us_stock", []))
    out += generate_subsection_md("hot_spot_hk_stock", results.get("hot_spot_hk_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_hk_stock", ""), code_map, appendix.get("hot_spot_hk_stock", []))

    out += "---\n\n## 🌟 四、全策略综合对比总结 (Master Chart)\n\n"
    if os.path.exists(os.path.join(base_dir, "nav_chart_all.png")):
        out += get_chart_md("nav_chart_all.png", base_dir)
    out += get_chart_md("pnl_chart_all.png", base_dir)

    os.makedirs(os.path.dirname(output_md_file), exist_ok=True)
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
        body { font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 10px; }
        .container { max-width: 98%; margin: 0 auto; background: var(--card); padding: 20px; border-radius: 12px; }
        h1 { text-align: center; color: #4f46e5; }
        h2 { border-bottom: 2px solid var(--border); padding-bottom: 10px; margin-top: 40px; }
        h3 { color: #4b5563; border-left: 4px solid #4f46e5; padding-left: 10px; }
        h4 { margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; }
        th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
        td:last-child, th:last-child { white-space: normal; min-width: 150px; max-width: 400px; }
        .nowrap { white-space: nowrap; }
        th { background: #f3f4f6; position: sticky; top: 0; }
        th:nth-child(1), td:nth-child(1), th:nth-child(2), td:nth-child(2) { text-align: left; }
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

    html += get_cash_overview_html()

    html += "<h2>🟢 第一章：稳健红利策略 (基本面护城河)</h2>\n"
    html += generate_subsection_html("dividend_a_stock", results.get("dividend_a_stock", []), div_headers, diff, trade_history, base_dir, llm_reviews.get("dividend_a_stock", ""), code_map, appendix.get("dividend_a_stock", []))

    html += "<hr>\n<h2>🔵 第二章：高增成长策略 (基本面护城河)</h2>\n"
    html += generate_subsection_html("growth_a_stock", results.get("growth_a_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_a_stock", ""), code_map, appendix.get("growth_a_stock", []))
    html += generate_subsection_html("growth_us_stock", results.get("growth_us_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_us_stock", ""), code_map, appendix.get("growth_us_stock", []))
    html += generate_subsection_html("growth_hk_stock", results.get("growth_hk_stock", []), gro_headers, diff, trade_history, base_dir, llm_reviews.get("growth_hk_stock", ""), code_map, appendix.get("growth_hk_stock", []))

    html += "<h2>🔴 第三章：产业热点战法 (AI 宏观洞察与事件驱动)</h2>\n"
    html += generate_subsection_html("hot_spot_a_stock", results.get("hot_spot_a_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_a_stock", ""), code_map, appendix.get("hot_spot_a_stock", []))
    html += generate_subsection_html("hot_spot_us_stock", results.get("hot_spot_us_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_us_stock", ""), code_map, appendix.get("hot_spot_us_stock", []))
    html += generate_subsection_html("hot_spot_hk_stock", results.get("hot_spot_hk_stock", []), hot_headers, diff, trade_history, base_dir, llm_reviews.get("hot_spot_hk_stock", ""), code_map, appendix.get("hot_spot_hk_stock", []))

    html += "<h2>🌟 四、全策略综合对比总结 (Master Chart)</h2>\n"
    if os.path.exists(os.path.join(base_dir, "nav_chart_all.png")):
        html += get_chart_html("nav_chart_all.png", base_dir)
    html += get_chart_html("pnl_chart_all.png", base_dir)

    html += "    </div>\n</body>\n</html>\n"
    with open(output_html_file, "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
