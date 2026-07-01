import json
import sys
import os
import base64

def render_table_md(title, items, headers):
    res = f"## {title} (入选 {len(items)} 只)\n\n"
    if len(items) == 0:
        return res + "暂无符合条件的标的。\n\n"
    
    res += "| " + " | ".join(headers) + " |\n"
    res += "|" + "|".join(["---"] * len(headers)) + "|\n"
    for row in items:
        cells = []
        for h in headers:
            val = row.get(h)
            if val is None:
                cells.append("")
            elif isinstance(val, float):
                cells.append(f"{val:.2f}")
            else:
                cells.append(str(val))
        res += "| " + " | ".join(cells) + " |\n"
    return res + "\n\n"

def render_table_html(title, items, headers):
    res = f"<h2>{title} (入选 {len(items)} 只)</h2>\n"
    if len(items) == 0:
        return res + "<p>暂无符合条件的标的。</p>\n"
        
    res += "<table>\n"
    res += "  <thead>\n    <tr>\n"
    for h in headers:
        res += f"      <th>{h}</th>\n"
    res += "    </tr>\n  </thead>\n  <tbody>\n"
    
    for row in items:
        res += "    <tr>\n"
        for h in headers:
            val = row.get(h)
            if val is None:
                cell = ""
            elif isinstance(val, float):
                cell = f"{val:.2f}"
            else:
                cell = str(val)
                
            # Highlight pnl
            if h == "累计涨跌幅":
                if cell.startswith("-"):
                    cell = f"<span class='loss'>{cell}</span>"
                elif cell != "0.00%" and cell != "":
                    cell = f"<span class='win'>+{cell}</span>"
            res += f"      <td>{cell}</td>\n"
        res += "    </tr>\n"
    res += "  </tbody>\n</table>\n"
    return res

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 generate_report.py <input_json> <output_md>")
        sys.exit(1)
        
    input_file = sys.argv[1]
    output_md_file = sys.argv[2]
    output_html_file = os.path.splitext(output_md_file)[0] + ".html"
    
    if not os.path.exists(input_file):
        print(f"File not found: {input_file}")
        sys.exit(1)
        
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    results = data.get("results", {})
    div_results = results.get("dividend", [])
    gro_results = results.get("growth", [])
    diff = data.get("diff", {})
    trade_history = data.get("trade_history", [])

    # ================= MARKDOWN GENERATION =================
    out = f"# A 股全市场双核心策略筛选结果\n\n"
    out += f"本次运行基于当前最新行情快照，自动同步计算出以下两大策略标的池。\n\n"

    # 1. 红利策略
    div_headers = [
        "股票代码", "股票简称", "PE", "PB", "估值公式值", "TTM股息率",
        "总市值(亿元)", "3年连续双增长", "3年平均净利率", "3年净利润CAGR", "3年经营现金流平均增速", "资产负债率",
        "入选价格", "累计涨跌幅"
    ]
    out += render_table_md("一、稳健红利策略 (Dividend Strategy)", div_results, div_headers)

    div_diff = diff.get("dividend", {})
    if div_diff.get("added") or div_diff.get("removed"):
        out += "> **红利策略调仓提示**：\n"
        if div_diff.get("added"):
            added_strs = [f"{item['name']} (入选价: {item.get('entry_price', 0):.2f})" if isinstance(item, dict) else str(item) for item in div_diff["added"]]
            out += f"> 🟢 **新增入池**：{', '.join(added_strs)}\n"
        if div_diff.get("removed"):
            removed_strs = []
            for item in div_diff["removed"]:
                if isinstance(item, dict):
                    ep = item.get("entry_price", 0)
                    cp = item.get("exit_price", 0)
                    pnl = item.get("pnl", 0) * 100
                    removed_strs.append(f"{item['name']} (入选价: {ep:.2f}, 剔除价: {cp:.2f}, 盈亏: {pnl:.2f}%)")
                else:
                    removed_strs.append(str(item))
            out += f"> 🔴 **掉出观测**：{', '.join(removed_strs)}\n"
        out += "\n\n"

    # 2. 成长策略
    gro_headers = [
        "股票代码", "股票简称", "所处行业", "PE", "PB", "TTM股息率",
        "总市值(亿元)", "3年连续双增长", "3年平均净资产收益率", "3年平均净利率", "3年净利润CAGR", "3年营收CAGR", "资产负债率",
        "入选价格", "累计涨跌幅"
    ]
    out += render_table_md("二、高增成长策略 (Growth Strategy)", gro_results, gro_headers)

    gro_diff = diff.get("growth", {})
    if gro_diff.get("added") or gro_diff.get("removed"):
        out += "> **成长策略调仓提示**：\n"
        if gro_diff.get("added"):
            added_strs = [f"{item['name']} (入选价: {item.get('entry_price', 0):.2f})" if isinstance(item, dict) else str(item) for item in gro_diff["added"]]
            out += f"> 🟢 **新增入池**：{', '.join(added_strs)}\n"
        if gro_diff.get("removed"):
            removed_strs = []
            for item in gro_diff["removed"]:
                if isinstance(item, dict):
                    ep = item.get("entry_price", 0)
                    cp = item.get("exit_price", 0)
                    pnl = item.get("pnl", 0) * 100
                    removed_strs.append(f"{item['name']} (入选价: {ep:.2f}, 剔除价: {cp:.2f}, 盈亏: {pnl:.2f}%)")
                else:
                    removed_strs.append(str(item))
            out += f"> 🔴 **掉出观测**：{', '.join(removed_strs)}\n"
        out += "\n\n"

    # 3. 历史交割记录
    if trade_history:
        out += "## 三、历史交割记录 (Closed Trades)\n\n"
        out += "| 策略类型 | 股票简称 | 买入日期 | 买入价格 | 卖出日期 | 卖出价格 | 最终盈亏率 |\n"
        out += "|---|---|---|---|---|---|---|\n"
        for trade in reversed(trade_history):
            strat = "稳健红利" if trade.get("strategy") == "dividend" else "高增成长"
            name = trade.get("name", "")
            in_d = trade.get("entry_date", "")
            in_p = trade.get("entry_price", 0)
            out_d = trade.get("exit_date", "")
            out_p = trade.get("exit_price", 0)
            pnl = trade.get("pnl", 0) * 100
            
            pnl_str = f"<span style='color:red'>+{pnl:.2f}%</span>" if pnl > 0 else f"<span style='color:green'>{pnl:.2f}%</span>"
            out += f"| {strat} | {name} | {in_d} | {in_p:.2f} | {out_d} | {out_p:.2f} | {pnl_str} |\n"
        out += "\n\n"
        out += "### 胜率分析 (Win Rate Analysis)\n\n"
        out += "![历史交割胜率分析](/Users/zouzhengting/Workplace/a_share_factor_flow/reports/pnl_chart.png)\n\n"
        
    with open(output_md_file, "w", encoding="utf-8") as f:
        f.write(out)
        
    # ================= HTML GENERATION =================
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>A股全市场双核心策略筛选结果</title>
    <style>
        :root {
            --bg-color: #f9fafb;
            --container-bg: #ffffff;
            --text-main: #1f2937;
            --text-muted: #6b7280;
            --border: #e5e7eb;
            --primary: #4f46e5;
            --win: #dc2626; /* Red for gains in CN */
            --loss: #16a34a; /* Green for losses in CN */
            --header-bg: #f3f4f6;
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            line-height: 1.6;
            padding: 20px;
            margin: 0;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: var(--container-bg);
            padding: 40px;
            border-radius: 12px;
            box-shadow: var(--shadow);
        }
        h1, h2, h3 {
            color: var(--text-main);
            border-bottom: 2px solid var(--border);
            padding-bottom: 10px;
            margin-top: 40px;
        }
        h1 { margin-top: 0; text-align: center; color: var(--primary); border-bottom: none; }
        .subtitle { text-align: center; color: var(--text-muted); margin-bottom: 40px; }
        
        table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            font-size: 0.95em;
        }
        th, td {
            padding: 12px 15px;
            text-align: right;
            border-bottom: 1px solid var(--border);
        }
        th {
            background-color: var(--header-bg);
            font-weight: 600;
            color: var(--text-main);
            position: sticky;
            top: 0;
        }
        th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {
            text-align: left;
        }
        tr:hover {
            background-color: #f8fafc;
        }
        .win { color: var(--win); font-weight: bold; }
        .loss { color: var(--loss); font-weight: bold; }
        
        .alert {
            background-color: #eff6ff;
            border-left: 4px solid var(--primary);
            padding: 15px 20px;
            margin: 20px 0;
            border-radius: 4px;
        }
        .alert p { margin: 5px 0; }
        
        .chart-container {
            text-align: center;
            margin-top: 30px;
            padding: 20px;
            background: #fff;
            border: 1px solid var(--border);
            border-radius: 8px;
        }
        .chart-container img {
            max-width: 100%;
            height: auto;
            border-radius: 4px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>A股全市场双核心策略筛选结果</h1>
        <p class="subtitle">本次运行基于当前最新行情快照，自动同步计算出以下两大策略标的池。</p>
"""
    
    html += render_table_html("一、稳健红利策略 (Dividend Strategy)", div_results, div_headers)
    
    if div_diff.get("added") or div_diff.get("removed"):
        html += "<div class='alert'>\n  <p><strong>红利策略调仓提示：</strong></p>\n"
        if div_diff.get("added"):
            added_strs = [f"{item['name']} (入选价: {item.get('entry_price', 0):.2f})" if isinstance(item, dict) else str(item) for item in div_diff["added"]]
            html += f"  <p>🟢 <strong>新增入池</strong>：{', '.join(added_strs)}</p>\n"
        if div_diff.get("removed"):
            removed_strs = []
            for item in div_diff["removed"]:
                if isinstance(item, dict):
                    ep = item.get("entry_price", 0)
                    cp = item.get("exit_price", 0)
                    pnl = item.get("pnl", 0) * 100
                    removed_strs.append(f"{item['name']} (入选价: {ep:.2f}, 剔除价: {cp:.2f}, 盈亏: {pnl:.2f}%)")
                else:
                    removed_strs.append(str(item))
            html += f"  <p>🔴 <strong>掉出观测</strong>：{', '.join(removed_strs)}</p>\n"
        html += "</div>\n"
        
    html += render_table_html("二、高增成长策略 (Growth Strategy)", gro_results, gro_headers)
    
    if gro_diff.get("added") or gro_diff.get("removed"):
        html += "<div class='alert'>\n  <p><strong>成长策略调仓提示：</strong></p>\n"
        if gro_diff.get("added"):
            added_strs = [f"{item['name']} (入选价: {item.get('entry_price', 0):.2f})" if isinstance(item, dict) else str(item) for item in gro_diff["added"]]
            html += f"  <p>🟢 <strong>新增入池</strong>：{', '.join(added_strs)}</p>\n"
        if gro_diff.get("removed"):
            removed_strs = []
            for item in gro_diff["removed"]:
                if isinstance(item, dict):
                    ep = item.get("entry_price", 0)
                    cp = item.get("exit_price", 0)
                    pnl = item.get("pnl", 0) * 100
                    removed_strs.append(f"{item['name']} (入选价: {ep:.2f}, 剔除价: {cp:.2f}, 盈亏: {pnl:.2f}%)")
                else:
                    removed_strs.append(str(item))
            html += f"  <p>🔴 <strong>掉出观测</strong>：{', '.join(removed_strs)}</p>\n"
        html += "</div>\n"
        
    if trade_history:
        html += "<h2>三、历史交割记录 (Closed Trades)</h2>\n"
        html += "<table>\n  <thead>\n    <tr>\n"
        for h in ["策略类型", "股票简称", "买入日期", "买入价格", "卖出日期", "卖出价格", "最终盈亏率"]:
            html += f"      <th>{h}</th>\n"
        html += "    </tr>\n  </thead>\n  <tbody>\n"
        
        for trade in reversed(trade_history):
            strat = "稳健红利" if trade.get("strategy") == "dividend" else "高增成长"
            name = trade.get("name", "")
            in_d = trade.get("entry_date", "")
            in_p = trade.get("entry_price", 0)
            out_d = trade.get("exit_date", "")
            out_p = trade.get("exit_price", 0)
            pnl = trade.get("pnl", 0) * 100
            
            pnl_cls = "win" if pnl > 0 else "loss" if pnl < 0 else ""
            pnl_sign = "+" if pnl > 0 else ""
            pnl_str = f"<span class='{pnl_cls}'>{pnl_sign}{pnl:.2f}%</span>"
            
            html += f"    <tr>\n"
            html += f"      <td style='text-align:left'>{strat}</td>\n"
            html += f"      <td style='text-align:left'>{name}</td>\n"
            html += f"      <td>{in_d}</td>\n"
            html += f"      <td>{in_p:.2f}</td>\n"
            html += f"      <td>{out_d}</td>\n"
            html += f"      <td>{out_p:.2f}</td>\n"
            html += f"      <td>{pnl_str}</td>\n"
            html += f"    </tr>\n"
        html += "  </tbody>\n</table>\n"
        
        chart_path = os.path.join(os.path.dirname(input_file), "reports/pnl_chart.png")
        if os.path.exists(chart_path):
            with open(chart_path, "rb") as img:
                b64 = base64.b64encode(img.read()).decode("utf-8")
                html += "<h3>胜率分析 (Win Rate Analysis)</h3>\n"
                html += f"<div class='chart-container'><img src='data:image/png;base64,{b64}' alt='PnL Chart'></div>\n"
                
    html += """
    </div>
</body>
</html>
"""
    with open(output_html_file, "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
