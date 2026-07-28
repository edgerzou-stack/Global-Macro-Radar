---
name: a-share-factor-screen
description: Use when the user wants to screen A-share stocks using the dual-core Dividend and Growth strategies with point-in-time quotes and disclosed financials.
---

# A-Share Factor Screen

Use this skill when the user asks for A-share stock screening with our predefined dual-core strategy. The script now automatically runs both strategies and outputs a combined result.

## Dual-Core Strategies

### 1. 稳健红利策略 (Dividend Strategy)
Designed to find cash-cow companies with stable dividends and undervalued prices.
- **Valuation**: `(总市值 - 总市值 / PB) / (总市值 / PE) < 10` (equivalent to `PE * (PB - 1) / PB < 10`)
- **Dividend**: `TTM dividend yield > 3.0%`
- **Market Cap**: `总市值 > 100亿元`
- **Debt Ratio**: retained for display/audit only; it is not a hard filter.
- **Profitability & Growth**:
  - `3年净利润CAGR > 5%`
  - `3年连续双增长` (Revenue & Profit YoY > 0 for 3 consecutive years)
  - `3年平均ROE > 5%`
  - `3年平均净利率`仅展示和审计，不作为硬门槛（避免系统性排斥零售、贸易等低净利率高周转公司）
- **Cash Flow Moat**:
  - `3年经营现金流平均增速 > 0%` (Average of the last 3 years' operating cash flow YoY growth)
  - `3年平均现金流利润覆盖 >= 0.8`
- **Dividend quality**: authoritative status `ok`, TTM yield `> 3%`, at least 4 dividend years in the latest 5, three consecutive dividend years, and latest annual dividend cut no worse than 30%.
- **Diversification/result cap**: at most 3 names per industry and at most 50 research candidates.

### 2. 高增成长策略 (Growth Strategy)
Designed to find companies whose most recent operating trend is improving across the full A-share market.
- **Industry**: No industry restriction.
- **Valuation and latest growth**: positive, non-missing `PE < min(latest Revenue YoY, latest Profit YoY)`.
- **Market Cap**: `总市值 > 100亿元`
- **Quarterly trend**: Revenue QoQ and Profit QoQ must both be positive in each of the latest three consecutive disclosed quarter ends.
- **Not hard filters**: three-year CAGR, three-year continuous double growth, debt ratio, ROE and net margin.
- **Result count**: no Top-10 truncation for A-share growth candidates.

## Data Policy
- Treat `current quote fields` as the primary basis for market screening.
- Always use the latest quote snapshot for: `PB`, `PE(TTM)`, `latest price`, `market cap`.
- Treat `TTM dividend yield` as a derived field: latest quote-snapshot price as denominator, cash dividends implemented in the last 12 months as numerator.
- Growth QoQ/YoY fields use only reports disclosed by the effective date and require three consecutive quarter ends.
- Always state the effective quote session and the report periods used for growth.
- US statements come primarily from SEC Company Facts; HK statements come primarily from official HKEX result-announcement PDFs. Yahoo Finance is only a current-date fallback and is never accepted as point-in-time historical evidence.
- Every accepted statement frame must contain `Total Revenue`, `Net Income`, reporting cadence, filing dates and source-document provenance. Missing or unparseable disclosures are filtered out.
- A quarterly US/HK reporter needs four consecutive statements to prove three consecutive revenue-and-profit increases.
- A semiannual US/HK reporter needs three consecutive statements to prove two consecutive revenue-and-profit increases.
- Empty, stale, incomplete, nonconsecutive or unsupported statements are rejected. Provider timeouts and parse failures are source errors, not company rejections.

## Workflow

1. Run `scripts/screen_a_share.py` to fetch data and run the core dual-strategy logic. This script automatically saves state and calculates the `diff` (added/removed stocks) compared to the previous run.
2. Run `scripts/generate_report.py` to parse the output JSON into a final Markdown report containing formatted tables and highlighted watchlist changes.
3. Check the output Markdown (e.g., `screening_results.md`) and present it to the user.

## Script Usage

First, run the data engine:
```bash
python3 quant-strategy/scripts/screen_a_share.py \
  --require-continuous-growth \
  --output-file dual_screen.json
```

Then, generate the Markdown report with the persistent watchlist diff prompts:
```bash
python3 quant-strategy/scripts/generate_report.py \
  dual_screen.json \
  /Users/zouzhengting/.gemini/antigravity/brain/cb368359-75c4-4195-b42f-77230af3485d/screening_results.md
```
