# Quant Strategy Scripts

本目录包含统一日流程、数据适配器、账本、验收、生产迁移和 PIT 回测入口。运行环境固定为 Python 3.11.9；依赖从仓库根 `requirements.txt` 安装。

## 四类入口

### 1. Unified daily flow

生产和端到端验收只从根目录 `run_all.sh` 启动。`daily_runner.py` 顺序执行股票源检查、DB 检查、新闻、筛选、NAV、账本检查、图表和交付，并使用 run identity、writer fence、Online Backup、checkpoint/resume 与 durable manifest。

```bash
./run_all.sh --mode live-shadow \
  --database /absolute/path/to/test-copy.db \
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-run-id \
  --delivery-mode sink
```

### 2. Shadow acceptance

`shadow_runner.py` 是有界健康探针，不是完整业务流程。默认对生产库只读并为每轮创建隔离备份；`--allow-live-api` 只增加 RSS/行情只读探针，强制禁用 LLM，不执行筛选交易或报告交付。

```bash
python3 quant-strategy/scripts/shadow_runner.py --iterations 1 \
  --allow-live-api --live-profile full
```

20/20 稳定性样本应分散在独立时间窗口中，每次 `--iterations 1`，避免一次突发请求掩盖源漂移。

### 3. PIT backtest

`backtest_engine.py` 消费版本化历史数据合同，生成 manifest、结果和隔离 audit DB。只有具备 point-in-time fundamentals、membership、FX、corporate actions 和 delisting 数据的运行才能用于策略结论。

### 4. Production release

`production_release.py` 与 daily flow 分离。默认 copy-only：只读打开源库，通过 SQLite Online Backup 生成副本，在副本应用 v6/quarantine 并验证恢复。生产写入还要求 canonical path、确认 token、fresh audit、writer fence 和指定维护窗口。

```bash
python3 quant-strategy/scripts/production_release.py \
  --source-db /absolute/path/to/quant_system.db \
  --audit /absolute/path/to/audit.json \
  --output-dir /absolute/path/to/new-release-dir
```

日常验收禁止加入 `--apply-production`。详见 [PRODUCTION_RELEASE.md](PRODUCTION_RELEASE.md)。

## Offline fixture 边界

单独设置 `GLOBAL_SCREEN_FIXTURE` 只验证全球筛选阶段，不等于完整 E2E。完整 `offline` 模式要求固定 bundle 中所有 fixture 文件齐全，任一缺失都 fail closed，并且只能使用标记为 `backtest` 的隔离数据库。

## 关键脚本

| 分类 | 脚本 | 作用 |
| --- | --- | --- |
| 编排 | `daily_runner.py`, `scheduler.py` | 八阶段流程、恢复和显式调度 |
| 数据健康 | `check_stock_apis.py`, `fetch_universe.py`, `data_provider.py` | 源交叉验证、Universe、异步批量适配 |
| 筛选 | `screen_hot_spot.py`, `screen_global_quant.py`, `us_hk_quant.py` | 热点与九策略筛选 |
| 账本/NAV | `db_utils.py`, `calc_nav.py`, `check_ledger_sanity.py` | legacy 账本、精确会话 NAV 与检查 |
| 报告 | `plot_pnl.py`, `generate_report.py`, `send_unified_email.py` | 图表、HTML/Markdown 和 outbox |
| 验收 | `shadow_runner.py` | 隔离 shadow 与只读 live probes |
| 迁移 | `production_release.py`, `migrations/` | v6、quarantine、copy/restore 演练 |
| 审计恢复 | `rebuild_dividend_ledger.py` | 从版本化事件清单重建红利账本的新数据库副本 |
| 回测 | `backtest_engine.py`, `core/backtest.py` | PIT 执行和审计 |

`repair_db_prices.py` 是永久停用的兼容入口。成交价必须保留原始价格，任何历史恢复都
不得以前/后复权收盘价覆盖成交字段。

目录中的 `test_*.py` 诊断脚本不是专有回归套件；正式测试位于 private Core 的 `quant-strategy/tests`、`industry-radar/tests` 和根 `tests`，public 仓库不发布这些专有 tests/CI。

## 运行安全摘要

- mode 和 database 必须显式指定。
- production 日历查询失败时 fail closed。
- `live-shadow` 不等于 `shadow_runner --allow-live-api`。
- 非 production 强制禁真实订单；所有模式 delivery 默认 sink。
- 真实 SMTP 需要 production + 双重显式确认。
- 邮件 CLI 自身再次要求 `--confirm-live-delivery`；审核后的 HTML 可配合
  `--html-file` 和 `--expected-html-sha256` 单独发送，不触发数据库写入。
- scheduler 默认关闭，持久运行需 `--enable-scheduler`。
- 成功以 durable run manifest 和 delivery journal 为准，不只看进程退出码。
