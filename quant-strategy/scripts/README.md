# Quant Strategy Execution Scripts


[![License: CC BY-NC 4.0](https://img.shields.io/badge/License-CC_BY--NC_4.0-lightgrey.svg)](#)

Welcome to the **Quant Strategy Execution Scripts** directory. This directory is the engine room of the Quantitative Strategy system. It contains the primary execution scripts, backtesting frameworks, and multi-market screening logic.

## Usage

These scripts are utility tools designed to be executed via standard Python 3.9+. Ensure your virtual environment is active before executing them.

### Shadow acceptance and controlled live health probes

`shadow_runner.py` is offline by default. It opens the production ledger read-only,
creates an isolated SQLite backup for every iteration, disables real orders, and
blocks network access for all ordinary and custom stages.

```bash
python3 scripts/shadow_runner.py --iterations 20
```

`--allow-live-api` adds only two bounded, read-only health stages: the production
RSS ingest health contract and `DataGateway` market-source probes. It does not run
the news scorer, load an LLM client, submit an order, or write the production DB.
Only HTTPS GET/HEAD and read-only provider query adapters are permitted. Each
iteration has one shared deadline and logical source-request budget.

Recommended first authorized live sample:

```bash
python3 scripts/shadow_runner.py \
  --iterations 1 \
  --allow-live-api \
  --live-rss-feed https://openai.com/news/rss.xml \
  --live-symbol 600519 \
  --live-symbol AAPL \
  --live-request-limit 6 \
  --live-timeout-seconds 45 \
  --live-lookback-days 10
```

Use `--skip-live-rss` or `--skip-live-market` to isolate one family of probes.
Reports contain per-source freshness, latency, success rate, request counts, and
A-share Baostock/Sina close-price cross-checks. API keys are never printed and LLM
use remains disabled even if LLM variables exist in the parent environment.

## Content Index

| Item | Type | Description |
|---|---|---|
| `backtest.py` | **File** | Unit testing script for core functions. |
| `core` | **Directory** | Submodule or categorized directory for `core`. |
| `daily_runner.py` | **File** | Core logic or execution script. |
| `data_provider.py` | **File** | Core logic or execution script. |
| `db_utils.py` | **File** | Core logic or execution script. |
| `fetch_universe.py` | **File** | Core logic or execution script. |
| `generate_report.py` | **File** | Generates diagrams or HTML dashboard assets. |
| `llm_utils.py` | **File** | Core logic or execution script. |
| `migrate_to_sqlite.py` | **File** | Core logic or execution script. |
| `plot_pnl.py` | **File** | Core logic or execution script. |
| `screen_a_share.py` | **File** | Core logic or execution script. |
| `screen_global_quant.py` | **File** | Core logic or execution script. |
| `screen_global_quant_deps.py` | **File** | Core logic or execution script. |
| `screen_hot_spot.py` | **File** | Core logic or execution script. |
| `send_unified_email.py` | **File** | Core logic or execution script. |
| `test_em.py` | **File** | Unit testing script for core functions. |
| `test_em2.py` | **File** | Unit testing script for core functions. |
| `test_em_quote.py` | **File** | Unit testing script for core functions. |
| `test_em_quote2.py` | **File** | Unit testing script for core functions. |
| `test_fetch.py` | **File** | Unit testing script for core functions. |
| `test_fin.py` | **File** | Unit testing script for core functions. |
| `test_tz.py` | **File** | Unit testing script for core functions. |
| `test_yf_prices.py` | **File** | Unit testing script for core functions. |
| `us_hk_quant.py` | **File** | Core logic or execution script. |


---

## License & Copyright

> **开源协议声明 (License & Copyright)**
> 本仓库包含的架构文档、设计思路及配套代码均采用 **CC BY-NC 4.0 (知识共享-署名-非商业性使用)** 协议发布。
> 允许个人学习、学术研究及开源技术交流。**严格禁止任何企业或个人将其直接或间接用于任何商业目的**（包括但不限于商业芯片研发、企业内部培训、闭源软件开发等）。如需商业使用，请与作者联系获取单独授权。
