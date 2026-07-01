---
name: a-share-factor-screen
description: Use when the user wants to screen A-share stocks using the dual-core strategy (Dividend Strategy & Growth Strategy) with live valuation, dividend yield, market cap, revenue/profit growth, debt ratio, cash flow, and industry constraints.
---

# A-Share Factor Screen

Use this skill when the user asks for A-share stock screening with our predefined dual-core strategy. The script now automatically runs both strategies and outputs a combined result.

## Dual-Core Strategies

### 1. 稳健红利策略 (Dividend Strategy)
Designed to find cash-cow companies with stable dividends and undervalued prices.
- **Valuation**: `(总市值 - 总市值 / PB) / (总市值 / PE) < 10` (equivalent to `PE * (PB - 1) / PB < 10`)
- **Dividend**: `TTM dividend yield > 3.0%`
- **Market Cap**: `总市值 > 100亿元`
- **Debt Ratio**: `资产负债率 < 50%`
- **Profitability & Growth**:
  - `3年净利润CAGR > 5%`
  - `3年连续双增长` (Revenue & Profit YoY > 0 for 3 consecutive years)
  - `3年平均净利率 > 10%`
- **Cash Flow Moat**:
  - `3年经营现金流平均增速 > 0%` (Average of the last 3 years' operating cash flow YoY growth)

### 2. 高增成长策略 (Growth Strategy)
Designed to find high-growth tech companies with outstanding fundamentals.
- **Industry**: Must belong to tech-related sectors (e.g., `半导体`, `计算机设备`, `软件开发`, `通信设备`, `通信服务`, `光学光电子`, `消费电子`, `元件`, `其他电子Ⅱ`, `电子化学品Ⅱ`, `IT服务Ⅱ`, `数字媒体`).
- **Valuation**: `PE < min(3-year Revenue CAGR, 3-year Profit CAGR)` (PEG < 1)
- **Market Cap**: `总市值 > 100亿元`
- **Debt Ratio**: `资产负债率 < 50%`
- **Profitability & Growth**:
  - `3年平均净资产收益率 (ROE) > 10%`
  - `3年营收CAGR > 20%`
  - `3年净利润CAGR > 20%`
  - `3年连续双增长` (Revenue & Profit YoY > 0 for 3 consecutive years)
  - `3年平均净利率 > 10%`

## Data Policy
- Treat `current quote fields` as the primary basis for market screening.
- Always use the latest quote snapshot for: `PB`, `PE(TTM)`, `latest price`, `market cap`.
- Treat `TTM dividend yield` as a derived field: latest quote-snapshot price as denominator, cash dividends implemented in the last 12 months as numerator.
- `3-year` metrics (CAGR, Margins, Cash Flow) are computed from each stock's latest disclosed year-end reports over a 3-year window.
- Always state the report date used for financial growth and debt ratio.

## Workflow

1. Run `scripts/screen_a_share.py` to fetch data and run the core dual-strategy logic. This script automatically saves state and calculates the `diff` (added/removed stocks) compared to the previous run.
2. Run `scripts/generate_report.py` to parse the output JSON into a final Markdown report containing formatted tables and highlighted watchlist changes.
3. Check the output Markdown (e.g., `screening_results.md`) and present it to the user.

## Script Usage

First, run the data engine:
```bash
python3 /Users/zouzhengting/.codex/skills/a-share-factor-screen/scripts/screen_a_share.py \
  --require-continuous-growth \
  --output-file dual_screen.json
```

Then, generate the Markdown report with the persistent watchlist diff prompts:
```bash
python3 /Users/zouzhengting/.codex/skills/a-share-factor-screen/scripts/generate_report.py \
  dual_screen.json \
  /Users/zouzhengting/.gemini/antigravity/brain/cb368359-75c4-4195-b42f-77230af3485d/screening_results.md
```
