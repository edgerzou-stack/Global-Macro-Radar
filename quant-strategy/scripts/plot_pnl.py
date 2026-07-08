import json
import os
import shutil
import datetime
import pandas as pd
import matplotlib
matplotlib.use('Agg') # For headless environments
import matplotlib.pyplot as plt
from collections import defaultdict

# ==========================================
# Bright & Clean Theme (Original layout style)
# ==========================================
plt.style.use('default')
plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'PingFang SC', 'Heiti TC']
plt.rcParams['axes.unicode_minus'] = False

STRAT_NAMES = {
    "dividend_a_stock": "A股红利",
    "growth_a_stock": "A股高增",
    "hot_spot_a_stock": "A股热点",
    "dividend_us_stock": "美股红利",
    "growth_us_stock": "美股高增",
    "hot_spot_us_stock": "美股热点",
    "dividend_hk_stock": "港股红利",
    "growth_hk_stock": "港股高增",
    "hot_spot_hk_stock": "港股热点"
}

def build_timeseries_pnl(trades: list) -> pd.Series:
    """
    Convert a list of trades into a true cumulative PNL time series.
    """
    if not trades:
        return pd.Series(dtype=float)
        
    records = []
    for t in trades:
        if "exit_date" in t and "pnl" in t:
            try:
                date = pd.to_datetime(t["exit_date"].split()[0])
                pnl_percent = t["pnl"] * 100
                records.append({"date": date, "pnl": pnl_percent})
            except Exception:
                pass
                
    if not records:
        return pd.Series(dtype=float)
        
    # Sort by date and calculate running sum
    df = pd.DataFrame(records).sort_values("date")
    
    # We aggregate PNL per day in case multiple trades exit on the same day
    daily_pnl = df.groupby("date")["pnl"].sum()
    cum_pnl = daily_pnl.cumsum()
    
    # Prepend 0 to start the curve nicely
    start_date = cum_pnl.index[0] - pd.Timedelta(days=1)
    cum_pnl.loc[start_date] = 0.0
    cum_pnl = cum_pnl.sort_index()
    
    return cum_pnl

def plot_strategy(strat_id, name, trades, output_file, color, artifact_dir, nav_records=None):
    # Fallback to trade naive sum if no nav_records (for backward compatibility during transition)
    total = len(trades)
    
    if nav_records:
        df = pd.DataFrame(nav_records)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
        df['nav_pct'] = (df['nav'] / 1000000.0 - 1) * 100
        ts = df.set_index('date')['nav_pct']
        cum_pnl_val = df['nav_pct'].iloc[-1] if not df.empty else 0.0
    else:
        ts = build_timeseries_pnl(trades)
        cum_pnl_val = sum([t["pnl"] * 100 for t in trades])
    
    fig, (ax_table, ax_curve) = plt.subplots(1, 2, figsize=(12, 5), gridspec_kw={'width_ratios': [1, 2.5]})
    
    if not ts.empty:
        # If too many points, hide markers to prevent clutter
        marker = 'o' if len(ts) <= 30 else None
        markersize = 4 if len(ts) <= 30 else 0
        
        ax_curve.plot(ts.index, ts.values, color=color, linewidth=2, 
                      marker=marker, markersize=markersize, alpha=0.8, label=f"{name} ({cum_pnl_val:+.2f}%)")
                      
    ax_curve.axhline(0, color='gray', linestyle='dashed', linewidth=1)
    ax_curve.set_title(f'{name} - 累计净收益曲线', fontsize=14)
    ax_curve.set_xlabel('日期', fontsize=12)
    ax_curve.set_ylabel('累计净收益率 (%)', fontsize=12)
    ax_curve.tick_params(axis='x', rotation=45)
    ax_curve.legend(loc='upper left')
    ax_curve.grid(True, alpha=0.3)
    
    # Table subplot
    ax_table.axis('tight')
    ax_table.axis('off')
    ax_table.set_title(f'{name} - 核心指标统计', fontsize=14, pad=20)
    
    cell_text = [[f"{total}", f"{cum_pnl_val:+.2f}%"]]
    row_labels = [name]
    col_labels = ['总交易(笔)', '总净收益(%)']
    
    table = ax_table.table(cellText=cell_text,
                           rowLabels=row_labels,
                           rowColours=[color],
                           colLabels=col_labels,
                           loc='center',
                           cellLoc='center')
                           
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1, 2.5)
    
    plt.tight_layout()
    plt.savefig(output_file, dpi=300, bbox_inches="tight")
    plt.close()
    
    if os.path.exists(artifact_dir):
        artifact_path = os.path.join(artifact_dir, f"pnl_chart_{strat_id}.png")
        shutil.copy2(output_file, artifact_path)

def plot_all(strategy_trades, output_file, strat_colors, artifact_dir):
    fig, (ax_table, ax_curve) = plt.subplots(1, 2, figsize=(18, 8), gridspec_kw={'width_ratios': [1, 2.5]})
    
    cell_text = []
    row_labels = []
    row_colors = []
    
    # Sort strategies by cum PNL
    strat_metrics = []
    for strat, trades in strategy_trades.items():
        if strat not in STRAT_NAMES:
            continue
        cum_pnl = sum([t["pnl"] * 100 for t in trades])
        strat_metrics.append({"strat": strat, "cum_pnl": cum_pnl, "total": len(trades), "trades": trades})
    strat_metrics.sort(key=lambda x: x["cum_pnl"], reverse=True)
    
    for m in strat_metrics:
        strat = m["strat"]
        trades = m["trades"]
        cum_pnl_val = m["cum_pnl"]
        total = m["total"]
        name = STRAT_NAMES.get(strat, strat)
        color = strat_colors.get(strat, '#999999')  # fallback for deprecated strategies
        
        ts = build_timeseries_pnl(trades)
        if not ts.empty:
            marker = 'o' if len(ts) <= 30 else None
            markersize = 3 if len(ts) <= 30 else 0
            
            ax_curve.plot(ts.index, ts.values, color=color, linewidth=1.5, 
                          linestyle='-', marker=marker, markersize=markersize, 
                          alpha=0.8, label=f"{name} ({cum_pnl_val:+.2f}%)")
                          
            row_labels.append(name)
            cell_text.append([f"{total}", f"{cum_pnl_val:+.2f}%"])
            row_colors.append(color)

    ax_curve.axhline(0, color='gray', linestyle='dashed', linewidth=1)
    ax_curve.set_title('各策略等权累计净收益曲线综合对比 (Master Chart)', fontsize=16)
    ax_curve.set_xlabel('平仓日期', fontsize=12)
    ax_curve.set_ylabel('累计净收益率 (%)', fontsize=12)
    ax_curve.tick_params(axis='x', rotation=45)
    
    ax_curve.legend(loc='upper left', bbox_to_anchor=(1.02, 1), borderaxespad=0.)
    ax_curve.grid(True, alpha=0.3)
    
    ax_table.axis('tight')
    ax_table.axis('off')
    ax_table.set_title('综合指标统计', fontsize=14, pad=20)
    
    if cell_text:
        col_labels = ['总交易(笔)', '总净收益(%)']
        table = ax_table.table(cellText=cell_text, rowLabels=row_labels, rowColours=row_colors, colLabels=col_labels, loc='center', cellLoc='center')
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 2.0)
    
    plt.tight_layout()
    plt.savefig(output_file, dpi=300, bbox_inches="tight")
    plt.close()
    
    if os.path.exists(artifact_dir):
        artifact_path = os.path.join(artifact_dir, "pnl_chart_all.png")
        shutil.copy2(output_file, artifact_path)

def plot_nav_all(nav_records, output_file, strat_colors, artifact_dir):
    """
    Plots the true Net Asset Value (NAV) curves from the database.
    nav_records is a list of dicts: {"date": "...", "strategy_id": "...", "nav": ...}
    """
    if not nav_records:
        return
        
    df = pd.DataFrame(nav_records)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    for strat, group in df.groupby('strategy_id'):
        name = STRAT_NAMES.get(strat, strat)
        color = strat_colors.get(strat, '#999999')
        
        # Calculate percentage return relative to initial capital (1,000,000)
        group['nav_pct'] = (group['nav'] / 1000000.0 - 1) * 100
        
        ax.plot(group['date'], group['nav_pct'], color=color, linewidth=2, 
                linestyle='-', marker='o' if len(group) <= 30 else None, markersize=4,
                alpha=0.9, label=f"{name} ({group['nav_pct'].iloc[-1]:+.2f}%)")
                
    ax.axhline(0, color='gray', linestyle='dashed', linewidth=1)
    ax.set_title('全球多策略沙盒 - 真实净值收益率走势 (True NAV Curve)', fontsize=16)
    ax.set_xlabel('日期', fontsize=12)
    ax.set_ylabel('净值收益率 (%)', fontsize=12)
    ax.tick_params(axis='x', rotation=45)
    ax.legend(loc='upper left', bbox_to_anchor=(1.02, 1), borderaxespad=0.)
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_file, dpi=300, bbox_inches="tight")
    plt.close()
    
    if os.path.exists(artifact_dir):
        artifact_path = os.path.join(artifact_dir, "nav_chart_all.png")
        shutil.copy2(output_file, artifact_path)

def main():
    from config import PROJECT_ROOT
    flow_dir = PROJECT_ROOT
    reports_dir = os.path.join(flow_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    
    for f in os.listdir(reports_dir):
        if f.startswith("pnl_chart") and f.endswith(".png"):
            os.remove(os.path.join(reports_dir, f))
    
    import sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import db_utils
    
    _, trade_history = db_utils.load_portfolio_and_trades()
    if not trade_history:
        print("No trade history available to plot.")
        return
        
    valid_trades = [t for t in trade_history if "exit_date" in t and "pnl" in t]
    if not valid_trades:
        print("No valid completed trades found.")
        return
        
    strategy_trades = defaultdict(list)
    for t in valid_trades:
        strat = t.get("strategy", "unknown")
        strategy_trades[strat].append(t)
    
    colors_cycle = [
        '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', 
        '#e377c2', '#7f7f7f', '#bcbd22', '#17becf', '#000080', '#FF8C00'
    ]
    
    strat_colors = {}
    c_idx = 0
    for strat in STRAT_NAMES.keys():
        strat_colors[strat] = colors_cycle[c_idx % len(colors_cycle)]
        c_idx += 1
        
    artifact_dir = os.environ.get("ARTIFACT_DIR", "")
    
    # Fetch true NAV records for each strategy
    import sqlite3
    from core.cash_manager import get_db_path
    
    all_nav_records = []
    strategy_navs = defaultdict(list)
    try:
        conn = sqlite3.connect(get_db_path())
        c = conn.cursor()
        c.execute("SELECT date, strategy_id, nav FROM strategy_nav_history")
        nav_rows = c.fetchall()
        for r in nav_rows:
            rec = {"date": r[0], "strategy_id": r[1], "nav": r[2]}
            all_nav_records.append(rec)
            strategy_navs[r[1]].append(rec)
    except Exception as e:
        print(f"Failed to fetch NAV records: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

    for strat in STRAT_NAMES.keys():
        if strat in strategy_trades and strategy_trades[strat]:
            out_file = os.path.join(reports_dir, f"pnl_chart_{strat}.png")
            # Pass the nav records for this specific strategy
            plot_strategy(strat, STRAT_NAMES.get(strat), strategy_trades[strat], out_file, strat_colors[strat], artifact_dir, nav_records=strategy_navs.get(strat, None))
            
    out_file_all = os.path.join(reports_dir, "pnl_chart_all.png")
    plot_all(strategy_trades, out_file_all, strat_colors, artifact_dir)
    
    # Plot combined NAV chart
    if all_nav_records:
        out_nav_file = os.path.join(reports_dir, "nav_chart_all.png")
        plot_nav_all(all_nav_records, out_nav_file, strat_colors, artifact_dir)
        print("NAV chart generated.")
        
    print("All charts generated (Original Bright Theme with Dense Point Fix).")

if __name__ == "__main__":
    main()

