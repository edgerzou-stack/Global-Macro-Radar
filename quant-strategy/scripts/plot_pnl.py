import json
import matplotlib.pyplot as plt
import os
import matplotlib
import numpy as np
from collections import defaultdict
import shutil

matplotlib.use('Agg') # For headless environments
plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'PingFang SC', 'Heiti TC']
plt.rcParams['axes.unicode_minus'] = False

def main():
    flow_dir = "/Users/zouzhengting/Workplace/a_share_factor_flow"
    input_file = os.path.join(flow_dir, "dual_screen.json")
    output_file = os.path.join(flow_dir, "reports/pnl_chart.png")
    
    if not os.path.exists(input_file):
        print(f"File not found: {input_file}")
        return
        
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    trade_history = data.get("trade_history", [])
    if not trade_history:
        print("No trade history available to plot.")
        return
        
    valid_trades = [t for t in trade_history if "exit_date" in t and "pnl" in t]
    if not valid_trades:
        print("No valid completed trades found.")
        return
        
    # Sort trades globally by exit_date
    valid_trades.sort(key=lambda x: x["exit_date"])
    
    # Group by strategy
    strategy_trades = defaultdict(list)
    for t in valid_trades:
        strat = t.get("strategy", "unknown")
        strategy_trades[strat].append(t)
        
    # Add a pseudo-strategy for "ALL"
    strategy_trades["ALL"] = valid_trades
    
    # Mapping for display names
    strat_names = {
        "dividend": "稳健红利",
        "growth": "高增成长",
        "hot_spot": "热点战法",
        "ALL": "全策略综合"
    }
    
    # Calculate metrics per strategy
    metrics_data = []
    
    # Create figure with 2 subplots (Table on left, Curves on right)
    fig, (ax_table, ax_curve) = plt.subplots(1, 2, figsize=(15, 6), gridspec_kw={'width_ratios': [1, 2]})
    
    colors_cycle = ['royalblue', 'orange', 'purple', 'cyan', 'magenta']
    strat_colors = {"ALL": "red"}
    c_idx = 0
    
    # Data for the table
    cell_text = []
    row_labels = []
    row_colors = []
    
    # We want ALL at the bottom of the table, so sort others first
    strats_to_process = [k for k in strategy_trades.keys() if k != "ALL"]
    strats_to_process.append("ALL")
    
    for strat in strats_to_process:
        trades = strategy_trades[strat]
        name = strat_names.get(strat, strat)
        
        pnls = [t["pnl"] * 100 for t in trades]
        wins = len([p for p in pnls if p > 0])
        total = len(pnls)
        win_rate = (wins / total) * 100 if total > 0 else 0
        cum_pnl = sum(pnls)
        
        # Color assigning
        if strat not in strat_colors:
            strat_colors[strat] = colors_cycle[c_idx % len(colors_cycle)]
            c_idx += 1
            
        color = strat_colors[strat]
        
        # Build Equity Curve Data
        dates = []
        cum_returns = []
        current_cum = 0.0
        
        # Initial point
        if trades:
            dates.append(trades[0].get("entry_date", "2026-05-01"))
            cum_returns.append(0.0)
            
        for t in trades:
            current_cum += (t["pnl"] * 100)
            dates.append(t["exit_date"])
            cum_returns.append(current_cum)
            
        # Plot curve
        linewidth = 3 if strat == "ALL" else 2
        linestyle = '-' if strat == "ALL" else '--'
        alpha = 1.0 if strat == "ALL" else 0.7
        
        ax_curve.plot(dates, cum_returns, marker='o', color=color, linewidth=linewidth, 
                      linestyle=linestyle, alpha=alpha, markersize=4, label=f"{name} ({cum_pnl:+.2f}%)")
        
        # Table row
        row_labels.append(name)
        cell_text.append([f"{total}", f"{win_rate:.1f}%", f"{cum_pnl:+.2f}%"])
        row_colors.append(color)

    # Configure Equity Curve Plot
    ax_curve.axhline(0, color='gray', linestyle='dashed', linewidth=1)
    ax_curve.set_title('各策略等权累计净收益曲线 (Equity Curves)', fontsize=14)
    ax_curve.set_xlabel('平仓日期', fontsize=12)
    ax_curve.set_ylabel('累计净收益率 (%)', fontsize=12)
    ax_curve.tick_params(axis='x', rotation=45)
    ax_curve.legend(loc='upper left')
    ax_curve.grid(True, alpha=0.3)
    
    # Configure Table Plot
    ax_table.axis('tight')
    ax_table.axis('off')
    ax_table.set_title('核心策略指标统计', fontsize=14, pad=20)
    
    col_labels = ['总交易(笔)', '胜率(%)', '总净收益(%)']
    
    table = ax_table.table(cellText=cell_text,
                           rowLabels=row_labels,
                           rowColours=row_colors,
                           colLabels=col_labels,
                           loc='center',
                           cellLoc='center')
                           
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1, 2.5) # Scale for better padding
    
    plt.tight_layout()
    plt.savefig(output_file, dpi=300)
    print(f"Chart saved to {output_file}")
    
    # Also copy to artifacts directory
    artifact_dir = "/Users/zouzhengting/.gemini/antigravity/brain/cb368359-75c4-4195-b42f-77230af3485d"
    if os.path.exists(artifact_dir):
        artifact_path = os.path.join(artifact_dir, "pnl_chart.png")
        shutil.copy2(output_file, artifact_path)
        print(f"Chart copied to {artifact_path}")

if __name__ == "__main__":
    main()
