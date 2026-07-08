#!/usr/bin/env python3
import argparse
import json
from datetime import date, timedelta, datetime
import sys
import os
import time
from typing import Optional

import concurrent.futures
from data_provider import (
    fetch_quote_snapshot_cached,
    stock_yjbb_em_cached,
    stock_zcfz_em_cached,
    stock_dividend_cninfo_cached,
    stock_info_a_code_name_cached,
    clear_cache,
)
import pandas as pd

AUTO_REPORT_LOOKBACK = 6
QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))
ANNUAL_REPORT_LOOKBACK = 8
LONG_TERM_CAGR_YEARS = 3
DIVIDEND_TTM_DAYS = 365




def calculate_ttm_dividend_yield_for_code(
    symbol: str, as_of_date: date, latest_price: Optional[float]
) -> Optional[float]:
    if latest_price is None or pd.isna(latest_price):
        return None
    latest_price = float(latest_price)
    if latest_price <= 0:
        return None

    try:
        dividend_df = stock_dividend_cninfo_cached(symbol=symbol)
    except Exception:
        return None

    if dividend_df.empty:
        return None

    cutoff_date = as_of_date - timedelta(days=DIVIDEND_TTM_DAYS)
    dividend_df = dividend_df.copy()
    dividend_df["派息日"] = pd.to_datetime(dividend_df["派息日"], errors="coerce").dt.date
    dividend_df["除权日"] = pd.to_datetime(dividend_df["除权日"], errors="coerce").dt.date
    dividend_df["派息比例"] = pd.to_numeric(dividend_df["派息比例"], errors="coerce")

    effective_date = dividend_df["派息日"].where(dividend_df["派息日"].notna(), dividend_df["除权日"])
    dividend_df = dividend_df.assign(生效日期=effective_date)
    dividend_df = dividend_df[
        dividend_df["生效日期"].notna()
        & (dividend_df["生效日期"] <= as_of_date)
        & (dividend_df["生效日期"] > cutoff_date)
        & dividend_df["派息比例"].notna()
        & (dividend_df["派息比例"] > 0)
    ].copy()

    if dividend_df.empty:
        return None

    # `派息比例` is cash dividend per 10 shares in CNY.
    dividend_per_share = dividend_df["派息比例"].sum() / 10.0
    return dividend_per_share / latest_price * 100.0


def load_ttm_dividend_yield_table(
    as_of_date: date,
    quote_df: pd.DataFrame,
    target_codes: Optional[list[str]] = None,
) -> pd.DataFrame:
    if quote_df.empty:
        return pd.DataFrame(columns=["股票代码", "TTM股息率"])

    base = quote_df[["股票代码", "最新价"]].copy()
    if target_codes is not None:
        base = base[base["股票代码"].isin(target_codes)].copy()

    import concurrent.futures
    import time
    import random

    def fetch_dividend(row):
        time.sleep(random.uniform(0.01, 0.1))
        return {
            "股票代码": row["股票代码"],
            "TTM股息率": calculate_ttm_dividend_yield_for_code(
                symbol=row["股票代码"],
                as_of_date=as_of_date,
                latest_price=row["最新价"],
            ),
        }

    rows = []
    from tqdm import tqdm
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_dividend, row): row for _, row in base.iterrows()}
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc=f"Fetching dividend yield for {len(base)} candidates"):
            try:
                res = future.result()
                rows.append(res)
            except Exception as e:
                # Silently skip or log errors for individual stocks
                pass
                
    return pd.DataFrame(rows)


def load_code_name_table() -> pd.DataFrame:
    codes = stock_info_a_code_name_cached()[["code", "name"]].drop_duplicates().copy()
    codes["code"] = codes["code"].astype(str).str.zfill(6)
    codes["name"] = codes["name"].astype(str).str.strip()
    return codes.rename(columns={"code": "股票代码", "name": "股票简称"})


def build_quote_table(target_codes: Optional[list[str]] = None) -> pd.DataFrame:
    code_name_df = load_code_name_table()
    if target_codes is None:
        base = code_name_df.copy()
    else:
        base = code_name_df[code_name_df["股票代码"].isin(target_codes)].copy()

    quote = fetch_quote_snapshot_cached(base["股票代码"].tolist()).drop(
        columns=["股票简称"], errors="ignore"
    )
    merged = base.merge(quote, on="股票代码", how="left")
    merged["总市值(亿元)"] = pd.to_numeric(merged["总市值"], errors="coerce") / 1e8
    merged["估值公式值"] = merged["PE"] * (merged["PB"] - 1.0) / merged["PB"]
    merged["估值公式值"] = merged["估值公式值"].where(
        pd.notna(merged["PE"]) & pd.notna(merged["PB"]) & (merged["PB"] != 0)
    )
    return merged


def infer_candidate_report_dates(
    as_of_date: date, lookback: int = AUTO_REPORT_LOOKBACK
) -> list[str]:
    report_dates = []
    year = as_of_date.year

    while len(report_dates) < lookback:
        for month, day_in_month in reversed(QUARTER_ENDS):
            report_end = date(year, month, day_in_month)
            if report_end <= as_of_date:
                report_dates.append(report_end.strftime("%Y%m%d"))
                if len(report_dates) >= lookback:
                    break
        year -= 1

    return report_dates


def load_financial_tables(report_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    yjbb = stock_yjbb_em_cached(date=report_date)[
        [
            "股票代码",
            "股票简称",
            "营业总收入-营业总收入",
            "营业总收入-同比增长",
            "净利润-净利润",
            "净利润-同比增长",
            "所处行业",
            "最新公告日期",
        ]
    ].copy()
    yjbb["股票代码"] = yjbb["股票代码"].astype(str).str.zfill(6)
    yjbb["最新公告日期"] = pd.to_datetime(yjbb["最新公告日期"], errors="coerce").dt.date

    zcfz = stock_zcfz_em_cached(date=report_date)[
        ["股票代码", "股票简称", "资产负债率", "公告日期"]
    ].copy()
    zcfz["股票代码"] = zcfz["股票代码"].astype(str).str.zfill(6)
    zcfz["公告日期"] = pd.to_datetime(zcfz["公告日期"], errors="coerce").dt.date
    return yjbb, zcfz


def infer_candidate_annual_report_dates(
    as_of_date: date, lookback: int = ANNUAL_REPORT_LOOKBACK
) -> list[str]:
    dates = []
    year = as_of_date.year
    while len(dates) < lookback:
        annual_end = date(year, 12, 31)
        if annual_end <= as_of_date:
            dates.append(annual_end.strftime("%Y%m%d"))
        year -= 1
    return dates


def load_dynamic_cagr_table(
    as_of_date: date,
    target_codes: Optional[list[str]] = None,
) -> pd.DataFrame:
    annual_report_dates = infer_candidate_annual_report_dates(as_of_date)
    frames = []

    def process_annual(report_date):
        yjbb = stock_yjbb_em_cached(date=report_date)[
            [
                "股票代码",
                "股票简称",
                "营业总收入-营业总收入",
                "营业总收入-同比增长",
                "净利润-净利润",
                "净利润-同比增长",
                "净资产收益率",
                "每股经营现金流量",
                "最新公告日期",
            ]
        ].copy()
        yjbb["股票代码"] = yjbb["股票代码"].astype(str).str.zfill(6)
        yjbb["最新公告日期"] = pd.to_datetime(yjbb["最新公告日期"], errors="coerce").dt.date
        yjbb["营业总收入-营业总收入"] = pd.to_numeric(
            yjbb["营业总收入-营业总收入"], errors="coerce"
        )
        yjbb["营业总收入-同比增长"] = pd.to_numeric(
            yjbb["营业总收入-同比增长"], errors="coerce"
        )
        yjbb["净利润-净利润"] = pd.to_numeric(yjbb["净利润-净利润"], errors="coerce")
        yjbb["净利润-同比增长"] = pd.to_numeric(yjbb["净利润-同比增长"], errors="coerce")
        yjbb["净资产收益率"] = pd.to_numeric(yjbb["净资产收益率"], errors="coerce")
        yjbb["每股经营现金流量"] = pd.to_numeric(yjbb["每股经营现金流量"], errors="coerce")

        if target_codes is not None:
            yjbb = yjbb[yjbb["股票代码"].isin(target_codes)].copy()

        yjbb = yjbb[
            yjbb["最新公告日期"].notna() & (yjbb["最新公告日期"] <= as_of_date)
        ].copy()
        if yjbb.empty:
            return None

        yjbb["年报期末"] = report_date
        yjbb["年报年份"] = int(report_date[:4])
        return yjbb

    from tqdm import tqdm
    import concurrent.futures
    import random
    import time
    
    def process_annual_with_delay(d):
        time.sleep(random.uniform(0.1, 0.4))
        return process_annual(d)
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_annual_with_delay, d): d for d in annual_report_dates}
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="Loading A-share dynamic CAGR"):
            res = future.result()
            if res is not None:
                frames.append(res)
                
    if not frames:
        return pd.DataFrame(
            columns=[
                "股票代码",
                "CAGR终点年报",
                "CAGR起点年报",
                "3年连续双增长",
                "3年营收同比均为正",
                "3年净利润同比均为正",
                "3年平均净资产收益率",
                "3年平均净利率",
                "3年经营现金流平均增速",
                "3年营收CAGR",
                "3年净利润CAGR",
            ]
        )

    annual = pd.concat(frames, ignore_index=True)
    annual = annual.sort_values(["股票代码", "年报年份"], ascending=[True, False])

    rows = []
    for code, grp in annual.groupby("股票代码", sort=False):
        grp = grp.drop_duplicates("年报年份", keep="first").copy()
        grp = grp.sort_values("年报年份", ascending=False).reset_index(drop=True)
        if grp.empty:
            continue

        end_row = grp.iloc[0]
        target_start_year = int(end_row["年报年份"]) - LONG_TERM_CAGR_YEARS
        start_candidates = grp[grp["年报年份"] == target_start_year]
        if start_candidates.empty:
            continue
        start_row = start_candidates.iloc[0]

        revenue_cagr = None
        if (
            pd.notna(start_row["营业总收入-营业总收入"])
            and pd.notna(end_row["营业总收入-营业总收入"])
            and start_row["营业总收入-营业总收入"] > 0
            and end_row["营业总收入-营业总收入"] > 0
        ):
            revenue_cagr = (
                (
                    end_row["营业总收入-营业总收入"]
                    / start_row["营业总收入-营业总收入"]
                )
                ** (1.0 / LONG_TERM_CAGR_YEARS)
                - 1.0
            ) * 100.0

        profit_cagr = None
        if (
            pd.notna(start_row["净利润-净利润"])
            and pd.notna(end_row["净利润-净利润"])
            and start_row["净利润-净利润"] > 0
            and end_row["净利润-净利润"] > 0
        ):
            profit_cagr = (
                (end_row["净利润-净利润"] / start_row["净利润-净利润"])
                ** (1.0 / LONG_TERM_CAGR_YEARS)
                - 1.0
            ) * 100.0

        avg_net_margin = None
        avg_roe = None
        continuous_growth = False
        revenue_growth_positive = False
        profit_growth_positive = False
        target_years = [
            int(end_row["年报年份"]) - i for i in range(LONG_TERM_CAGR_YEARS)
        ]
        annual_window_grp = grp[grp["年报年份"].isin(target_years)]
        if len(annual_window_grp) == LONG_TERM_CAGR_YEARS:
            rev = annual_window_grp["营业总收入-营业总收入"]
            prof = annual_window_grp["净利润-净利润"]
            if (rev > 0).all() and prof.notna().all():
                margins = prof / rev * 100.0
                avg_net_margin = margins.mean()
            roe = annual_window_grp["净资产收益率"]
            if roe.notna().all():
                avg_roe = roe.mean()

            rev_yoy = annual_window_grp["营业总收入-同比增长"]
            prof_yoy = annual_window_grp["净利润-同比增长"]
            if rev_yoy.notna().all():
                revenue_growth_positive = (rev_yoy > 0).all()
            if prof_yoy.notna().all():
                profit_growth_positive = (prof_yoy > 0).all()
            if rev_yoy.notna().all() and prof_yoy.notna().all():
                if revenue_growth_positive and profit_growth_positive:
                    continuous_growth = True

            avg_cash_growth = None
            cash_flow = annual_window_grp["每股经营现金流量"]
            if cash_flow.notna().all():
                cash_flow_asc = cash_flow.iloc[::-1]
                cash_diff = cash_flow_asc.diff()
                prev_cash_abs = cash_flow_asc.shift().abs().replace(0, pd.NA)
                cash_yoy = cash_diff / prev_cash_abs
                if cash_yoy.notna().any():
                    avg_cash_growth = float(cash_yoy.dropna().mean() * 100.0)

        rows.append(
            {
                "股票代码": code,
                "CAGR终点年报": str(end_row["年报期末"]),
                "CAGR起点年报": str(start_row["年报期末"]),
                "3年连续双增长": continuous_growth,
                "3年营收同比均为正": revenue_growth_positive,
                "3年净利润同比均为正": profit_growth_positive,
                "3年平均净资产收益率": avg_roe,
                "3年平均净利率": avg_net_margin,
                "3年经营现金流平均增速": avg_cash_growth,
                "3年营收CAGR": revenue_cagr,
                "3年净利润CAGR": profit_cagr,
            }
        )

    return pd.DataFrame(rows)


def load_financial_table_as_of(
    as_of_date: date,
    report_date: Optional[str] = None,
    target_codes: Optional[list[str]] = None,
) -> tuple[pd.DataFrame, list[str], str]:
    candidate_report_dates = (
        [report_date] if report_date else infer_candidate_report_dates(as_of_date)
    )
    frames = []

    def process_candidate(candidate):
        yjbb, zcfz = load_financial_tables(candidate)
        if target_codes is not None:
            yjbb = yjbb[yjbb["股票代码"].isin(target_codes)].copy()
            zcfz = zcfz[zcfz["股票代码"].isin(target_codes)].copy()
        yjbb = yjbb[
            yjbb["最新公告日期"].notna() & (yjbb["最新公告日期"] <= as_of_date)
        ].copy()
        zcfz = zcfz[zcfz["公告日期"].notna() & (zcfz["公告日期"] <= as_of_date)].copy()
        merged = yjbb.merge(
            zcfz[["股票代码", "资产负债率", "公告日期"]], on="股票代码", how="inner"
        )
        if merged.empty:
            return None
        merged["财务报告期"] = candidate
        return merged[
            [
                "股票代码",
                "股票简称",
                "财务报告期",
                "营业总收入-营业总收入",
                "营业总收入-同比增长",
                "净利润-净利润",
                "净利润-同比增长",
                "资产负债率",
                "所处行业",
                "最新公告日期",
                "公告日期",
            ]
        ]

    from tqdm import tqdm
    import concurrent.futures
    import random
    import time

    def process_candidate_with_delay(c):
        time.sleep(random.uniform(0.1, 0.4))
        return process_candidate(c)
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_candidate_with_delay, c): c for c in candidate_report_dates}
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="Loading A-share financial tables"):
            res = future.result()
            if res is not None:
                frames.append(res)
                
    if not frames:
        empty = pd.DataFrame(
            columns=[
                "股票代码",
                "股票简称",
                "财务报告期",
                "营业总收入-营业总收入",
                "营业总收入-同比增长",
                "净利润-净利润",
                "净利润-同比增长",
                "资产负债率",
                "所处行业",
                "最新公告日期",
                "公告日期",
            ]
        )
        mode = "fixed_report_date" if report_date else "latest_disclosed_as_of_quote_date"
        return empty, candidate_report_dates, mode

    financial = pd.concat(frames, ignore_index=True)
    for col in ["营业总收入-营业总收入", "营业总收入-同比增长", "净利润-净利润", "净利润-同比增长", "资产负债率"]:
        financial[col] = pd.to_numeric(financial[col], errors="coerce")

    financial["财务报告期_dt"] = pd.to_datetime(
        financial["财务报告期"], format="%Y%m%d", errors="coerce"
    )
    financial = financial.sort_values(
        ["股票代码", "财务报告期_dt", "最新公告日期", "公告日期"],
        ascending=[True, False, False, False],
    )
    
    # Add 4-quarter YoY positive growth flag
    def check_acceleration(grp):
        import numpy as np
        if len(grp) < 3:
            return False
            
        grp_asc = grp.sort_values("财务报告期_dt", ascending=True)
        prof_yoy = pd.to_numeric(grp_asc["净利润-同比增长"].values[-3:], errors="coerce")
        rev_yoy = pd.to_numeric(grp_asc["营业总收入-同比增长"].values[-3:], errors="coerce")
        
        if np.isnan(prof_yoy).any() or np.isnan(rev_yoy).any():
            return False
            
        return (prof_yoy > 0).all() and (rev_yoy > 0).all()

    accel_flags = financial.groupby("股票代码").apply(check_acceleration).reset_index(name="3个季度连续加速增长")
    financial = financial.merge(accel_flags, on="股票代码", how="left")
    
    financial = financial.drop_duplicates("股票代码", keep="first")
    financial = financial.drop(columns=["财务报告期_dt"])

    mode = "fixed_report_date" if report_date else "latest_disclosed_as_of_quote_date"
    return financial, candidate_report_dates, mode


def attach_latest_financial_fields(
    df: pd.DataFrame,
    as_of_date: date,
    report_date: Optional[str] = None,
) -> tuple[pd.DataFrame, list[str], str]:
    candidate_report_dates = [report_date] if report_date else infer_candidate_report_dates(
        as_of_date
    )
    financial_mode = (
        "fixed_report_date" if report_date else "latest_disclosed_as_of_quote_date"
    )
    if df.empty:
        result = df.copy()
        for col in [
            "财务报告期",
            "营业总收入-营业总收入",
            "营业总收入-同比增长",
            "净利润-净利润",
            "净利润-同比增长",
            "资产负债率",
            "最新公告日期",
            "公告日期",
        ]:
            result[col] = pd.Series(dtype="object")
        return result, candidate_report_dates, financial_mode

    financial, candidate_report_dates, financial_mode = load_financial_table_as_of(
        as_of_date=as_of_date,
        report_date=report_date,
        target_codes=df["股票代码"].tolist(),
    )
    merged = df.merge(
        financial[
            [
                "股票代码",
                "财务报告期",
                "营业总收入-营业总收入",
                "营业总收入-同比增长",
                "净利润-净利润",
                "净利润-同比增长",
                "资产负债率",
                "3个季度连续加速增长",
                "所处行业",
                "最新公告日期",
                "公告日期",
            ]
        ],
        on="股票代码",
        how="left",
    )
    return merged, candidate_report_dates, financial_mode


def attach_dynamic_cagr_fields(df: pd.DataFrame, as_of_date: date) -> pd.DataFrame:
    if df.empty:
        result = df.copy()
        for col in [
            "CAGR终点年报",
            "CAGR起点年报",
            "3年连续双增长",
            "3年营收同比均为正",
            "3年净利润同比均为正",
            "3年平均净资产收益率",
            "3年平均净资产收益率",
            "3年平均净利率",
            "3年经营现金流平均增速",
            "3年营收CAGR",
            "3年净利润CAGR",
        ]:
            result[col] = pd.Series(dtype="object")
        return result

    cagr_table = load_dynamic_cagr_table(
        as_of_date=as_of_date,
        target_codes=df["股票代码"].tolist(),
    )
    return df.merge(
        cagr_table[
            [
                "股票代码",
                "CAGR终点年报",
                "CAGR起点年报",
                "3年连续双增长",
                "3年营收同比均为正",
                "3年净利润同比均为正",
                "3年平均净资产收益率",
                "3年平均净利率",
                "3年经营现金流平均增速",
                "3年营收CAGR",
                "3年净利润CAGR",
            ]
        ],
        on="股票代码",
        how="left",
    )


def parse_holding_inputs(raw_holdings: Optional[list[str]]) -> list[str]:
    holdings = []
    for raw in raw_holdings or []:
        for item in str(raw).split(","):
            item = item.strip()
            if item:
                holdings.append(item)
    return holdings


def resolve_holdings(
    holding_inputs: list[str], code_name_df: pd.DataFrame
) -> tuple[pd.DataFrame, list[dict]]:
    resolved_rows = []
    unresolved = []
    seen_codes = set()

    for item in holding_inputs:
        digits = "".join(ch for ch in item if ch.isdigit())
        if len(digits) == 6:
            matches = code_name_df[code_name_df["股票代码"] == digits]
        else:
            matches = code_name_df[code_name_df["股票简称"] == item]

        if len(matches) == 1:
            row = matches.iloc[0]
            code = row["股票代码"]
            if code in seen_codes:
                continue
            seen_codes.add(code)
            resolved_rows.append(
                {"输入": item, "股票代码": code, "股票简称": row["股票简称"]}
            )
        elif len(matches) == 0:
            unresolved.append(
                {
                    "输入": item,
                    "状态": "unresolved",
                    "原因": "未找到精确匹配的股票代码或简称",
                }
            )
        else:
            unresolved.append(
                {
                    "输入": item,
                    "状态": "ambiguous",
                    "原因": "匹配到多个同名股票，请改用6位股票代码",
                }
            )

    resolved = pd.DataFrame(resolved_rows)
    if resolved.empty:
        resolved = pd.DataFrame(columns=["输入", "股票代码", "股票简称"])
    return resolved, unresolved


def build_merged_table(
    quote_snapshot_date: date,
    report_date: Optional[str] = None,
    target_codes: Optional[list[str]] = None,
) -> tuple[pd.DataFrame, list[str], str]:
    quote_only = build_quote_table(target_codes=target_codes)
    with_financial, candidate_report_dates, financial_mode = attach_latest_financial_fields(
        quote_only,
        as_of_date=quote_snapshot_date,
        report_date=report_date,
    )
    merged = attach_dynamic_cagr_fields(with_financial, as_of_date=quote_snapshot_date)
    return merged, candidate_report_dates, financial_mode


def passes_valuation_formula(row: pd.Series, max_value: float) -> bool:
    value = row["估值公式值"]
    return pd.notna(value) and float(value) < max_value



def filter_dividend_strategy(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if df.empty:
        return df.copy()
        
    roe_series = df.get("3年平均净资产收益率", df.get("净资产收益率"))
    net_margin_series = df.get("3年平均净利率", df.get("销售净利率"))
    
    mask = (
        df["估值公式值"].notna() & (df["估值公式值"] < args.valuation_formula_max)
        & df["3年经营现金流平均增速"].notna() & (df["3年经营现金流平均增速"] > 0)
        & df["总市值"].notna() & (df["总市值"] > args.market_cap_min_yi * 1e8)
        & df["资产负债率"].notna() & (df["资产负债率"] < args.debt_ratio_max)
        & net_margin_series.notna() & (net_margin_series > args.avg_net_profit_margin_min)
        & df["3年净利润CAGR"].notna() & (df["3年净利润CAGR"] > args.profit_cagr_min)
        & roe_series.notna() & (roe_series > args.dividend_roe_min)
    )
    return df[mask].copy()

# 成长股科技制造底池
GROWTH_INDUSTRIES = [
    # 科技 (Tech)
    "半导体", "计算机设备", "软件开发", "通信设备", "通信服务",
    "光学光电子", "消费电子", "元件", "其他电子Ⅱ", "电子化学品Ⅱ",
    "IT服务Ⅱ", "数字媒体"
]

def filter_growth_strategy(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    """
    4-Quarter Continuous Improvement Strategy (连续4个季度业绩改善/减亏):
    1. Industry in GROWTH_INDUSTRIES
    2. Market Cap > args.market_cap_min_yi * 100 million
    3. Debt Ratio < args.debt_ratio_max
    4. Past 4 quarters YoY Profit & Revenue Growth must be > 0 (supports loss-reduction)
    5. PEG constraint: PE < min(Profit YoY, Revenue YoY)
    """
    if df.empty:
        return df.copy()

    roe_series = df.get("3年平均净资产收益率", df.get("净资产收益率"))
    net_margin_series = df.get("3年平均净利率", df.get("销售净利率"))
    profit_yoy_series = df.get("净利润-同比增长", df.get("净利润同比增长率"))
    revenue_yoy_series = df.get("营业总收入-同比增长", df.get("营业总收入同比增长率"))

    mask = (
        df["所处行业"].isin(GROWTH_INDUSTRIES)
        & df["总市值"].notna() & (df["总市值"] > args.market_cap_min_yi * 1e8)
        & (df["资产负债率"].isna() | (df["资产负债率"] < args.debt_ratio_max))
        & df["3个季度连续加速增长"].fillna(False)
        & (df["PE"].isna() | ((df["PE"] < profit_yoy_series) & (df["PE"] < revenue_yoy_series)))
    )
    return df[mask].copy()

def attach_ttm_dividend_yield(
    df: pd.DataFrame, as_of_date: date
) -> pd.DataFrame:
    if df.empty:
        result = df.copy()
        result["TTM股息率"] = pd.Series(dtype="float64")
        return result

    dividend_ttm = load_ttm_dividend_yield_table(
        as_of_date=as_of_date,
        quote_df=df[["股票代码", "最新价"]],
        target_codes=df["股票代码"].tolist(),
    )
    return df.merge(dividend_ttm, on="股票代码", how="left")


def threshold_payload(args: argparse.Namespace) -> dict:
    return {
        "valuation_formula_max": args.valuation_formula_max,
        "valuation_formula_definition": "(总市值 - 总市值 / PB) / (总市值 / PE)",
        "valuation_formula_equivalent": "PE * (PB - 1) / PB",
        "dividend_yield_min": args.dividend_yield_min,
        "dividend_yield_basis": "TTM_dividend_yield_from_last_12_months_cash_dividend",
        "market_cap_min_yi": args.market_cap_min_yi,
        "avg_net_profit_margin_min": args.avg_net_profit_margin_min,
        "revenue_yoy_positive_years": LONG_TERM_CAGR_YEARS,
        "profit_yoy_positive_years": LONG_TERM_CAGR_YEARS,
        "require_continuous_growth": args.require_continuous_growth,
        "profit_cagr_min": args.profit_cagr_min,
        "long_term_cagr_years": LONG_TERM_CAGR_YEARS,
        "debt_ratio_max": args.debt_ratio_max,
    }


def output_columns(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "股票代码",
        "股票简称",
        "最新价",
        "财务报告期",
        "营业总收入-营业总收入",
        "净利润-净利润",
        "PE",
        "PB",
        "估值公式值",
        "TTM股息率",
        "总市值(亿元)",
        "CAGR终点年报",
        "CAGR起点年报",
        "3年连续双增长",
        "3年营收同比均为正",
        "3年净利润同比均为正",
        "净资产收益率",
        "销售净利率",
        "净利润同比增长率",
        "营业总收入同比增长率",
        "3年经营现金流平均增速",
        "所处行业",
        "3年营收CAGR",
        "3年净利润CAGR",
        "资产负债率",
    ]
    # Add missing columns as NaN
    for col in cols:
        if col not in df.columns:
            df[col] = pd.NA
            
    return df[cols].sort_values(["PB", "TTM股息率"], ascending=[True, False])


def number_or_none(value) -> Optional[float]:
    if pd.isna(value):
        return None
    return round(float(value), 4)


def string_or_none(value) -> Optional[str]:
    if pd.isna(value):
        return None
    return str(value)


def evaluate_holding(row: pd.Series, args: argparse.Namespace, user_input: str) -> dict:
    failed_rules = []

    checks = [
        (
            "估值公式值",
            f"(总市值 - 总市值 / PB) / (总市值 / PE) < {args.valuation_formula_max}",
            row["估值公式值"],
            lambda x: x < args.valuation_formula_max,
        ),
        (
            "TTM股息率",
            f"TTM股息率 > {args.dividend_yield_min}%",
            row["TTM股息率"],
            lambda x: x > args.dividend_yield_min,
        ),
        (
            "总市值(亿元)",
            f"总市值 > {args.market_cap_min_yi}亿",
            row["总市值(亿元)"],
            lambda x: x > args.market_cap_min_yi,
        ),
        (
            "销售净利率",
            f"过去3年平均净利率 > {args.avg_net_profit_margin_min}%",
            row["销售净利率"],
            lambda x: x > args.avg_net_profit_margin_min,
        ),
        (
            "3年净利润CAGR",
            f"过去3年净利润年复合增长率 > {args.profit_cagr_min}%",
            row["3年净利润CAGR"],
            lambda x: x > args.profit_cagr_min,
        ),
        (
            "3年营收同比均为正",
            "最近3个年报的营业总收入同比增长率每年都 > 0",
            1.0 if row.get("3年营收同比均为正") else 0.0,
            lambda x: x == 1.0,
        ),
        (
            "3年净利润同比均为正",
            "最近3个年报的净利润同比增长率每年都 > 0",
            1.0 if row.get("3年净利润同比均为正") else 0.0,
            lambda x: x == 1.0,
        ),
        (
            "资产负债率",
            f"资产负债率 < {args.debt_ratio_max}%",
            row["资产负债率"],
            lambda x: x < args.debt_ratio_max,
        ),
    ]

    if args.require_continuous_growth:
        checks.append(
            (
                "3年连续双增长",
                "过去3年每一年的营收和净利润同比增长 > 0",
                1.0 if row.get("3年连续双增长") else 0.0,
                lambda x: x == 1.0,
            )
        )

    for field, rule_text, value, rule_fn in checks:
        if pd.isna(value):
            failed_rules.append(
                {"字段": field, "规则": rule_text, "当前值": None, "原因": "缺少最新数据"}
            )
        elif not rule_fn(float(value)):
            failed_rules.append(
                {"字段": field, "规则": rule_text, "当前值": round(float(value), 4)}
            )

    passed = not failed_rules
    return {
        "输入": user_input,
        "股票代码": row["股票代码"],
        "股票简称": row["股票简称"],
        "财务报告期": string_or_none(row.get("财务报告期")),
        "满足规则": passed,
        "操作建议": "继续持有" if passed else "卖出提醒",
        "当前指标": {
            "财务报告期": string_or_none(row.get("财务报告期")),
            "营业总收入": number_or_none(row["营业总收入-营业总收入"]),
            "净利润": number_or_none(row["净利润-净利润"]),
            "PE": number_or_none(row["PE"]),
            "PB": number_or_none(row["PB"]),
            "估值公式值": number_or_none(row["估值公式值"]),
            "TTM股息率": number_or_none(row["TTM股息率"]),
            "总市值(亿元)": number_or_none(row["总市值(亿元)"]),
            "CAGR终点年报": string_or_none(row.get("CAGR终点年报")),
            "CAGR起点年报": string_or_none(row.get("CAGR起点年报")),
            "3年连续双增长": row.get("3年连续双增长"),
            "3年营收同比均为正": row.get("3年营收同比均为正"),
            "3年净利润同比均为正": row.get("3年净利润同比均为正"),
            "销售净利率": number_or_none(row["销售净利率"]),
            "3年营收CAGR": number_or_none(row["3年营收CAGR"]),
            "3年净利润CAGR": number_or_none(row["3年净利润CAGR"]),
            "资产负债率": number_or_none(row["资产负债率"]),
        },
        "不满足条件": failed_rules,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Screen A-share stocks by factor rules.")
    parser.add_argument(
        "--report-date",
        help=(
            "Optional fixed report period end, e.g. 20260331. "
            "If omitted, auto-pick each stock's latest disclosed common financial report "
            "as of today's quote snapshot."
        ),
    )
    parser.add_argument("--valuation-formula-max", type=float, default=10.0)
    parser.add_argument("--dividend-yield-min", type=float, default=3.0)
    parser.add_argument("--market-cap-min-yi", type=float, default=100.0)
    parser.add_argument("--avg-net-profit-margin-min", type=float, default=10.0)
    parser.add_argument("--require-continuous-growth", action="store_true", default=False)
    parser.add_argument("--profit-cagr-min", type=float, default=5.0)
    # 1-year explosive growth thresholds
    parser.add_argument("--growth-yoy-min", type=float, default=30.0)
    parser.add_argument("--growth-roe-min", type=float, default=10.0)
    # Dividend thresholds
    parser.add_argument("--debt-ratio-max", type=float, default=50.0)
    parser.add_argument("--dividend-roe-min", type=float, default=10.0,
                        help="Minimum 3-year avg ROE for dividend strategy (default: 10%%)")
    parser.add_argument(
        "--output-file",
        help="Optional JSON output file path. If omitted, print to stdout.",
    )
    parser.add_argument("--force-refresh", action="store_true", help="Force clear cache")
    parser.add_argument(
        "--holding",
        action="append",
        help="Holding stock code or short name; repeat or pass comma-separated values",
    )
    return parser




def compute_portfolio_diff(strategy, old_port, new_results_list, current_prices, snapshot_date):
    diff = {"added": [], "removed": []}
    new_port = {}
    trade_history = []
    
    old_keys = set(old_port.keys())
    
    # We use stock code as key. If old portfolio uses names, we try to match them.
    # But wait, in new_results_list we have both 股票代码 and 股票简称.
    new_keys = {r["股票代码"] for r in new_results_list}
    
    # Actually, to make it backwards compatible, if old_keys look like names (not digits), we can map them.
    # We will just assume new keys are codes, and we will try to migrate old keys if they match.
    old_port_by_code = {}
    for k, v in old_port.items():
        if not k.isdigit():
            # It's a name. Try to find its code in new_results_list or current_prices (if we stored codes).
            # This is hard. If it's a name, we just keep it as name and let it be removed.
            old_port_by_code[k] = v
        else:
            old_port_by_code[k] = v
            
    old_keys = set(old_port_by_code.keys())
    # Actually, to be safe, let's keep using names for now, or just use code for new added and keep name for old.
    # Let's use code.
    added = new_keys - old_keys
    removed = old_keys - new_keys
    
    for key in added:
        price = current_prices.get(key, 0)
        diff["added"].append({"name": key, "entry_price": price})
        new_port[key] = {"entry_date": snapshot_date, "entry_price": price}
        
    for key in removed:
        ep = old_port_by_code.get(key, {}).get("entry_price", 0)
        entry_date = old_port_by_code.get(key, {}).get("entry_date", "未知")
        if entry_date == snapshot_date:
            continue  # T+1 rule
        cp = current_prices.get(key, 0)
        fee = 0.001
        pnl = (cp / ep - 1) - fee if ep > 0 else 0
        diff["removed"].append({"name": key, "entry_price": ep, "exit_price": cp, "pnl": pnl})
        trade_history.append({
            "strategy": strategy, "name": key, "entry_date": entry_date, 
            "entry_price": ep, "exit_date": snapshot_date, "exit_price": cp, "pnl": pnl
        })
        
    for key in new_keys & old_keys:
        new_port[key] = old_port_by_code[key]
        
    return diff, new_port, trade_history


def annotate_pnl(result_list, portfolio, current_prices):
    for r in result_list:
        # Use code if present, otherwise name
        key = r.get("股票代码", r.get("股票简称"))
        if key in portfolio:
            ep = portfolio[key].get("entry_price", 0)
            if ep > 0:
                cp = current_prices.get(key, 0)
                r["持仓盈亏(%)"] = round((cp / ep - 1) * 100, 2)
            else:
                r["持仓盈亏(%)"] = 0.0
    return result_list

def normalize_columns(df):
    col_rename_map = {
        "净利润-同比增长": "净利润同比增长率",
        "营业总收入-同比增长": "营业总收入同比增长率",
        "3年平均净资产收益率": "净资产收益率",
        "3年平均净利率": "销售净利率"
    }
    return df.rename(columns={k: v for k, v in col_rename_map.items() if k in df.columns})

def main() -> int:
    args = build_parser().parse_args()

    if args.force_refresh:
        clear_cache()
    from core.clock import clock
    quote_snapshot_date = clock.today()
    snapshot_date = clock.now().strftime("%Y-%m-%d %H:%M")
    holdings = parse_holding_inputs(args.holding)

    if holdings:
        code_name_df = load_code_name_table()
        resolved_holdings, unresolved_holdings = resolve_holdings(holdings, code_name_df)
        quote_stage = build_quote_table(
            target_codes=resolved_holdings["股票代码"].tolist(),
        )
        with_financial, candidate_report_dates, financial_mode = attach_latest_financial_fields(
            quote_stage,
            as_of_date=quote_snapshot_date,
            report_date=args.report_date,
        )
        with_cagr = attach_dynamic_cagr_fields(with_financial, as_of_date=quote_snapshot_date)
        merged = attach_ttm_dividend_yield(with_cagr, quote_snapshot_date)
        holding_lookup = {
            row["股票代码"]: row["输入"]
            for _, row in resolved_holdings.iterrows()
        }

        merged = normalize_columns(merged)

        checks = []
        for _, row in merged.sort_values("股票代码").iterrows():
            checks.append(evaluate_holding(row, args, holding_lookup[row["股票代码"]]))

        payload = {
            "mode": "holding_check",
            "snapshot_date": snapshot_date,
            "financial_as_of_date": snapshot_date,
            "financial_selection_mode": financial_mode,
            "candidate_report_dates": candidate_report_dates,
            "report_date": args.report_date,
            "thresholds": threshold_payload(args),
            "check_count": int(len(checks)),
            "checks": checks,
            "unresolved_holdings": unresolved_holdings,
        }
    else:
        quote_stage = build_quote_table()
        with_financial, candidate_report_dates, financial_mode = attach_latest_financial_fields(
            quote_stage,
            as_of_date=quote_snapshot_date,
            report_date=args.report_date,
        )
        with_cagr = attach_dynamic_cagr_fields(with_financial, as_of_date=quote_snapshot_date)
        
        # Fork strategies
        dividend_candidates = filter_dividend_strategy(with_cagr, args)
        growth_candidates = filter_growth_strategy(with_cagr, args)
        
        # Combine unique codes to fetch dividend yields
        combined_codes = pd.concat([dividend_candidates["股票代码"], growth_candidates["股票代码"]]).drop_duplicates().tolist()
        combined_df = with_cagr[with_cagr["股票代码"].isin(combined_codes)].copy()
        with_dividend = attach_ttm_dividend_yield(combined_df, quote_snapshot_date)
        
        with_dividend = normalize_columns(with_dividend)
        
        # Apply dividend filter for dividend strategy
        final_dividend = with_dividend[with_dividend["股票代码"].isin(dividend_candidates["股票代码"])].copy()
        if not final_dividend.empty:
            final_dividend = final_dividend[final_dividend["TTM股息率"].notna() & (final_dividend["TTM股息率"] > args.dividend_yield_min)].copy()
            
        final_growth = with_dividend[with_dividend["股票代码"].isin(growth_candidates["股票代码"])].copy()
        
        div_result = output_columns(final_dividend)
        gro_result = output_columns(final_growth)
        
        div_result_list = div_result.to_dict(orient="records")
        gro_result_list = gro_result.to_dict(orient="records")
        
        current_prices = {row["股票代码"]: row.get("最新价", 0) for row in quote_stage.to_dict(orient="records")}
        # also fall back to name for backwards compatibility
        current_prices.update({row["股票简称"]: row.get("最新价", 0) for row in quote_stage.to_dict(orient="records")})
        
        hot_spot_list = []
        from config import PROJECT_ROOT
        hot_spot_file = os.path.join(PROJECT_ROOT, "hot_spot_today.json")
        if os.path.exists(hot_spot_file):
            try:
                with open(hot_spot_file, "r", encoding="utf-8") as f:
                    hot_spot_data = json.load(f)
                    if isinstance(hot_spot_data, dict):
                        hot_spot_list = hot_spot_data.get("hot_spot_a_stock", [])
                    elif isinstance(hot_spot_data, list):
                        hot_spot_list = hot_spot_data
                for row in hot_spot_list:
                    current_prices[row["股票代码"]] = row.get("最新价", 0)
                    current_prices[row["股票简称"]] = row.get("最新价", 0)
            except Exception as e:
                print(f"Error loading hot_spot_today.json: {e}")
                
        diff = {
            "dividend": {"added": [], "removed": []},
            "growth": {"added": [], "removed": []},
            "hot_spot": {"added": [], "removed": []}
        }
        portfolio = {
            "dividend": {},
            "growth": {},
            "hot_spot": {}
        }
        trade_history = []
        
        trade_history = []
        
        if args.output_file and os.path.exists(args.output_file):
            try:
                    with open(args.output_file, "r", encoding="utf-8") as f:
                        old_payload = json.load(f)
                        
                    old_portfolio = old_payload.get("portfolio", {})
                    trade_history = old_payload.get("trade_history", [])
                    old_div_port = old_portfolio.get("dividend", {})
                    old_gro_port = old_portfolio.get("growth", {})
                    
                    # Backwards compatibility: if no portfolio in old json, construct from results
                    if not old_div_port:
                        old_div_port = {r.get("股票代码", r["股票简称"]): {"entry_date": "未知", "entry_price": r.get("最新价", 0)} for r in old_payload.get("results", {}).get("dividend", [])}
                    if not old_gro_port:
                        old_gro_port = {r.get("股票代码", r["股票简称"]): {"entry_date": "未知", "entry_price": r.get("最新价", 0)} for r in old_payload.get("results", {}).get("growth", [])}
                        
                    diff_div, port_div, trades_div = compute_portfolio_diff("dividend", old_div_port, div_result_list, current_prices, snapshot_date)
                    diff_gro, port_gro, trades_gro = compute_portfolio_diff("growth", old_gro_port, gro_result_list, current_prices, snapshot_date)
                    diff_hot, port_hot, trades_hot = compute_portfolio_diff("hot_spot", old_portfolio.get("hot_spot", {}), hot_spot_list, current_prices, snapshot_date)
                    
                    diff["dividend"] = diff_div
                    diff["growth"] = diff_gro
                    diff["hot_spot"] = diff_hot
                    
                    portfolio["dividend"] = port_div
                    portfolio["growth"] = port_gro
                    portfolio["hot_spot"] = port_hot
                    
                    trade_history.extend(trades_div)
                    trade_history.extend(trades_gro)
                    trade_history.extend(trades_hot)
                    
            except Exception as e:
                print(f"Error diffing portfolio: {e}")
                # On error, just init new portfolio
                portfolio["dividend"] = {r["股票代码"]: {"entry_date": snapshot_date, "entry_price": r.get("最新价", 0)} for r in div_result_list}
                portfolio["growth"] = {r["股票代码"]: {"entry_date": snapshot_date, "entry_price": r.get("最新价", 0)} for r in gro_result_list}
                portfolio["hot_spot"] = {r["股票代码"]: {"entry_date": snapshot_date, "entry_price": r.get("最新价", 0)} for r in hot_spot_list}
        else:
            portfolio["dividend"] = {r["股票代码"]: {"entry_date": snapshot_date, "entry_price": r.get("最新价", 0)} for r in div_result_list}
            portfolio["growth"] = {r["股票代码"]: {"entry_date": snapshot_date, "entry_price": r.get("最新价", 0)} for r in gro_result_list}
            portfolio["hot_spot"] = {r["股票代码"]: {"entry_date": snapshot_date, "entry_price": r.get("最新价", 0)} for r in hot_spot_list}

        # Annotate PnL
        div_result_list = annotate_pnl(div_result_list, portfolio["dividend"], current_prices)
        gro_result_list = annotate_pnl(gro_result_list, portfolio["growth"], current_prices)
        hot_spot_list = annotate_pnl(hot_spot_list, portfolio["hot_spot"], current_prices)
        
        payload = {
            "mode": "screen_all",
            "snapshot_date": snapshot_date,
            "financial_as_of_date": snapshot_date,
            "financial_selection_mode": financial_mode,
            "candidate_report_dates": candidate_report_dates,
            "report_date": args.report_date,
            "thresholds": threshold_payload(args),
            "counts": {
                "dividend": len(div_result_list),
                "growth": len(gro_result_list),
                "hot_spot": len(hot_spot_list)
            },
            "results": {
                "dividend": div_result_list,
                "growth": gro_result_list,
                "hot_spot": hot_spot_list
            },
            "portfolio": portfolio,
            "diff": diff,
            "trade_history": trade_history
        }

    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"Results saved to {args.output_file}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0

if __name__ == "__main__":
    sys.exit(main())
