#!/usr/bin/env python3
import argparse
from datetime import date, timedelta
import json
import sys
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

AUTO_REPORT_LOOKBACK = 4
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

    rows = []
    for _, row in base.iterrows():
        rows.append(
            {
                "股票代码": row["股票代码"],
                "TTM股息率": calculate_ttm_dividend_yield_for_code(
                    symbol=row["股票代码"],
                    as_of_date=as_of_date,
                    latest_price=row["最新价"],
                ),
            }
        )
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(process_annual, d) for d in annual_report_dates]
        for future in concurrent.futures.as_completed(futures):
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(process_candidate, c) for c in candidate_report_dates]
        for future in concurrent.futures.as_completed(futures):
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
    ).drop_duplicates("股票代码", keep="first")
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
    mask = (
        df["估值公式值"].notna() & (df["估值公式值"] < args.valuation_formula_max)
        & df["3年经营现金流平均增速"].notna() & (df["3年经营现金流平均增速"] > 0)
        & df["总市值"].notna() & (df["总市值"] > args.market_cap_min_yi * 1e8)
        & df["资产负债率"].notna() & (df["资产负债率"] < args.debt_ratio_max)
        & df["3年平均净利率"].notna() & (df["3年平均净利率"] > args.avg_net_profit_margin_min)
        & df["3年净利润CAGR"].notna() & (df["3年净利润CAGR"] > args.profit_cagr_min)
        & (df["3年营收同比均为正"] == True)
        & (df["3年净利润同比均为正"] == True)
    )
    if args.require_continuous_growth:
        mask = mask & (df["3年连续双增长"] == True)
    return df[mask].copy()

TECH_INDUSTRIES = ["半导体", "计算机设备", "软件开发", "通信设备", "通信服务", "光学光电子", "消费电子", "元件", "其他电子Ⅱ", "电子化学品Ⅱ", "IT服务Ⅱ", "数字媒体"]

def filter_growth_strategy(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    mask = (
        df["总市值"].notna() & (df["总市值"] > args.market_cap_min_yi * 1e8)
        & df["所处行业"].isin(TECH_INDUSTRIES)
        & df["资产负债率"].notna() & (df["资产负债率"] < args.debt_ratio_max)
        & df["3年平均净资产收益率"].notna() & (df["3年平均净资产收益率"] > args.growth_roe_min)
        & df["3年平均净利率"].notna() & (df["3年平均净利率"] > args.avg_net_profit_margin_min)
        & df["3年净利润CAGR"].notna() & (df["3年净利润CAGR"] > args.growth_profit_cagr_min)
        & df["3年营收CAGR"].notna() & (df["3年营收CAGR"] > args.growth_revenue_cagr_min)
        & (df["3年营收同比均为正"] == True)
        & (df["3年净利润同比均为正"] == True)
        & df["PE"].notna() & (df["PE"] < df["3年营收CAGR"]) & (df["PE"] < df["3年净利润CAGR"])
    )
    if args.require_continuous_growth:
        mask = mask & (df["3年连续双增长"] == True)
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
    return df[
        [
            "股票代码",
            "股票简称",
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
            "3年平均净资产收益率",
            "3年平均净利率",
            "3年经营现金流平均增速",
            "所处行业",
            "3年营收CAGR",
            "3年净利润CAGR",
            "资产负债率",
        ]
    ].sort_values(["PB", "TTM股息率"], ascending=[True, False])


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
            "3年平均净利率",
            f"过去3年平均净利率 > {args.avg_net_profit_margin_min}%",
            row["3年平均净利率"],
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
            "3年平均净利率": number_or_none(row["3年平均净利率"]),
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
    parser.add_argument("--require-continuous-growth", action="store_true")
    parser.add_argument("--profit-cagr-min", type=float, default=5.0)
    parser.add_argument("--growth-profit-cagr-min", type=float, default=20.0)
    parser.add_argument("--growth-revenue-cagr-min", type=float, default=20.0)
    parser.add_argument("--growth-roe-min", type=float, default=10.0)
    parser.add_argument("--debt-ratio-max", type=float, default=50.0)
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


def main() -> int:
    args = build_parser().parse_args()

    if args.force_refresh:
        clear_cache()
    quote_snapshot_date = date.today()
    snapshot_date = quote_snapshot_date.isoformat()
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
        
        # Apply dividend filter for dividend strategy
        final_dividend = with_dividend[with_dividend["股票代码"].isin(dividend_candidates["股票代码"])].copy()
        if not final_dividend.empty:
            final_dividend = final_dividend[final_dividend["TTM股息率"].notna() & (final_dividend["TTM股息率"] > args.dividend_yield_min)].copy()
            
        final_growth = with_dividend[with_dividend["股票代码"].isin(growth_candidates["股票代码"])].copy()
        
        div_result = output_columns(final_dividend)
        gro_result = output_columns(final_growth)
        
        div_result_list = div_result.to_dict(orient="records")
        gro_result_list = gro_result.to_dict(orient="records")
        
        current_prices = {row["股票简称"]: row.get("最新价", 0) for row in quote_stage.to_dict(orient="records")}
        
        diff = {
            "dividend": {"added": [], "removed": []},
            "growth": {"added": [], "removed": []}
        }
        portfolio = {
            "dividend": {},
            "growth": {}
        }
        
        if args.output_file:
            import os
            if os.path.exists(args.output_file):
                try:
                    with open(args.output_file, "r", encoding="utf-8") as f:
                        old_payload = json.load(f)
                        
                    old_portfolio = old_payload.get("portfolio", {})
                    trade_history = old_payload.get("trade_history", [])
                    old_div_port = old_portfolio.get("dividend", {})
                    old_gro_port = old_portfolio.get("growth", {})
                    
                    # Backwards compatibility: if no portfolio in old json, construct from results
                    if not old_div_port:
                        old_div_port = {r["股票简称"]: {"entry_date": "未知", "entry_price": r.get("最新价", 0)} for r in old_payload.get("results", {}).get("dividend", [])}
                    if not old_gro_port:
                        old_gro_port = {r["股票简称"]: {"entry_date": "未知", "entry_price": r.get("最新价", 0)} for r in old_payload.get("results", {}).get("growth", [])}
                        
                    old_div = set(old_div_port.keys())
                    old_gro = set(old_gro_port.keys())
                    new_div = {r["股票简称"] for r in div_result_list}
                    new_gro = {r["股票简称"] for r in gro_result_list}
                    
                    added_div = new_div - old_div
                    removed_div = old_div - new_div
                    added_gro = new_gro - old_gro
                    removed_gro = old_gro - new_gro
                    
                    for name in added_div:
                        price = current_prices.get(name, 0)
                        diff["dividend"]["added"].append({"name": name, "entry_price": price})
                    for name in removed_div:
                        ep = old_div_port.get(name, {}).get("entry_price", 0)
                        cp = current_prices.get(name, 0)
                        pnl = (cp / ep - 1) if ep > 0 else 0
                        diff["dividend"]["removed"].append({"name": name, "entry_price": ep, "exit_price": cp, "pnl": pnl})
                        trade_history.append({"strategy": "dividend", "name": name, "entry_date": old_div_port.get(name, {}).get("entry_date", "未知"), "entry_price": ep, "exit_date": snapshot_date, "exit_price": cp, "pnl": pnl})
                        
                    for name in added_gro:
                        price = current_prices.get(name, 0)
                        diff["growth"]["added"].append({"name": name, "entry_price": price})
                    for name in removed_gro:
                        ep = old_gro_port.get(name, {}).get("entry_price", 0)
                        cp = current_prices.get(name, 0)
                        pnl = (cp / ep - 1) if ep > 0 else 0
                        diff["growth"]["removed"].append({"name": name, "entry_price": ep, "exit_price": cp, "pnl": pnl})
                        trade_history.append({"strategy": "growth", "name": name, "entry_date": old_gro_port.get(name, {}).get("entry_date", "未知"), "entry_price": ep, "exit_date": snapshot_date, "exit_price": cp, "pnl": pnl})
                        
                    for name in new_div:
                        if name in old_div_port:
                            portfolio["dividend"][name] = old_div_port[name]
                        else:
                            portfolio["dividend"][name] = {"entry_date": snapshot_date, "entry_price": current_prices.get(name, 0)}
                    for name in new_gro:
                        if name in old_gro_port:
                            portfolio["growth"][name] = old_gro_port[name]
                        else:
                            portfolio["growth"][name] = {"entry_date": snapshot_date, "entry_price": current_prices.get(name, 0)}
                        
                except Exception as e:
                    print(f"Error processing portfolio tracking: {e}")
                    trade_history = []
                    for name in {r["股票简称"] for r in div_result_list}:
                        portfolio["dividend"][name] = {"entry_date": snapshot_date, "entry_price": current_prices.get(name, 0)}
                    for name in {r["股票简称"] for r in gro_result_list}:
                        portfolio["growth"][name] = {"entry_date": snapshot_date, "entry_price": current_prices.get(name, 0)}
            else:
                trade_history = []
                for name in {r["股票简称"] for r in div_result_list}:
                    portfolio["dividend"][name] = {"entry_date": snapshot_date, "entry_price": current_prices.get(name, 0)}
                    diff["dividend"]["added"].append({"name": name, "entry_price": current_prices.get(name, 0)})
                for name in {r["股票简称"] for r in gro_result_list}:
                    portfolio["growth"][name] = {"entry_date": snapshot_date, "entry_price": current_prices.get(name, 0)}
                    diff["growth"]["added"].append({"name": name, "entry_price": current_prices.get(name, 0)})
        else:
            trade_history = []
            for name in {r["股票简称"] for r in div_result_list}:
                portfolio["dividend"][name] = {"entry_date": snapshot_date, "entry_price": current_prices.get(name, 0)}
            for name in {r["股票简称"] for r in gro_result_list}:
                portfolio["growth"][name] = {"entry_date": snapshot_date, "entry_price": current_prices.get(name, 0)}
                
        for row in div_result_list:
            ep = portfolio["dividend"].get(row["股票简称"], {}).get("entry_price", 0)
            cp = current_prices.get(row["股票简称"], 0)
            row["入选价格"] = ep
            row["累计涨跌幅"] = f"{(cp / ep - 1) * 100:.2f}%" if ep > 0 else "0.00%"
            
        for row in gro_result_list:
            ep = portfolio["growth"].get(row["股票简称"], {}).get("entry_price", 0)
            cp = current_prices.get(row["股票简称"], 0)
            row["入选价格"] = ep
            row["累计涨跌幅"] = f"{(cp / ep - 1) * 100:.2f}%" if ep > 0 else "0.00%"

        payload = {
            "mode": "screen_all",
            "snapshot_date": snapshot_date,
            "financial_as_of_date": snapshot_date,
            "financial_selection_mode": financial_mode,
            "candidate_report_dates": candidate_report_dates,
            "report_date": args.report_date,
            "thresholds": threshold_payload(args),
            "stage_counts": {
                "quote_stage": int(len(quote_stage)),
                "dividend_final": int(len(div_result_list)),
                "growth_final": int(len(gro_result_list)),
            },
            "portfolio": portfolio,
            "trade_history": trade_history,
            "results": {
                "dividend": div_result_list,
                "growth": gro_result_list,
            },
            "diff": diff
        }

    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
