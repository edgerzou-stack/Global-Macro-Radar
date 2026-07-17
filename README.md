# Global Macro Radar

Global Macro Radar 是一个把实时产业新闻、全球多市场筛选、模拟账本、NAV、图表和报告交付串成单一可审计流程的研究系统。它覆盖 A 股、港股与美股，默认不提交真实订单，默认把交付写入本地 sink。

> 本项目仅用于研究与工程验证，不构成投资建议。

## 系统做什么

`run_all.sh` 顺序执行八个阶段：

1. 股票 API 健康检查与 A 股双源交叉验证
2. SQLite 物理完整性与账户约束检查
3. RSS 拉取、健康闸门、新闻去重与 LLM 双轨评分
4. Universe 刷新、热点筛选和 A/HK/US 全球量化筛选
5. 模拟账本结算与九策略 NAV
6. 账本 sanity check
7. NAV/PnL 图表
8. 报告生成与 delivery outbox

量化层持久化 Dividend、Growth、Hot Spot × A/HK/US 共九个策略桶。新闻层先输出四个 0–100 整数子分，再按配置聚合为 Innovation/Traffic 两条 0–10 composite；来源可信度作为独立元数据，不会自动给内容分加分。

## 安全模型

| Mode | 数据库 | 真实订单 | Delivery | Fixture |
| --- | --- | --- | --- | --- |
| `offline` | 隔离 backtest DB | 禁用 | sink | 完整 bundle 必填 |
| `shadow` | 隔离 test DB | 禁用 | sink | 可选 |
| `live-shadow` | 隔离 test DB | 禁用 | sink | 不提供时使用真实源 |
| `production` | canonical production DB | 受显式确认与 fence 保护 | 默认 sink | 可选 |

所有入口都要求显式 mode/database。production 还要求 canonical path 和二次写入确认；真实 SMTP 需要额外的 `--delivery-mode live --confirm-live-delivery`，邮件发送器本身也会再次校验该确认。scheduler 默认关闭。

运行时使用 SQLite Online Backup、writer fence、run identity、原子 checkpoint/resume 和 durable manifest。异常 legacy 数据只允许 quarantine，不猜测价格、成本或 PnL。

## 安装

固定环境为 Python 3.11.9：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r industry-radar/requirements.txt
```

创建本机配置；这些文件已被忽略，永远不要提交真实凭证或数据库：

```bash
cp .env.example .env
cp industry-radar/config.example.yaml industry-radar/config.yaml
chmod 600 .env industry-radar/config.yaml
```

## 正确运行

一个干净的隔离 test DB 可以直接用于 shadow：

```bash
./run_all.sh \
  --mode shadow \
  --database /absolute/path/to/test.db \
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-shadow-run \
  --delivery-mode sink
```

真实源验收使用 `live-shadow`。如果种子来自包含 legacy 异常的生产库，先用 `production_release.py` 默认 copy-only 模式生成带 v6/quarantine 的 `working_copy.db`，不要加入 `--apply-production`：

```bash
python3 quant-strategy/scripts/production_release.py \
  --source-db /absolute/path/to/quant_system.db \
  --audit /absolute/path/to/production_db_audit.json \
  --output-dir /absolute/path/to/new-release-dir

SQLITE_DB_PATH=/absolute/path/to/new-release-dir/working_copy.db \
QUANT_DB_ENV=test PYTHONPATH=quant-strategy/scripts \
python3 -c 'import db_utils; connection=db_utils.init_db(); connection.close()'

./run_all.sh \
  --mode live-shadow \
  --database /absolute/path/to/new-release-dir/working_copy.db \
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-live-shadow-run \
  --delivery-mode sink
```

成功不能只看退出码：同时核对 release/run manifest、delivery journal、数据库完整性和生产库前后哈希。完整的恢复、调度、Docker 与维护窗口协议见 [OPERATIONS.md](OPERATIONS.md)。

已审核的 sink HTML 可以通过邮件 CLI 的 `--html-file`、
`--expected-html-sha256` 与 `--confirm-live-delivery` 参数单独投递，不需要重跑
策略或改写数据库。发送器默认加载仓库根目录 `.env`，并将 data URI 图表转换
为 CID inline parts；具体 canary 和失败恢复流程见 [OPERATIONS.md](OPERATIONS.md)。

## 可靠性边界

- 新建仓必须取得权威、有限正数且处于真实交易时段的价格，否则 defer。
- 无退出价时保留持仓，不用估算价强行成交。
- NAV 使用各市场最近正式完成交易日的精确 close；尚未收盘的 session 不会被当成已有 daily close。
- 严格 OHLC 失败时，non-A NAV 只允许隔离与估值无关的坏 open 字段；close 仍必须为有限正数且位于 high/low 内，降级数据不写入 OHLC cache。
- LLM 结构化响应失败时，新闻评分和量化二次筛选各自按明确合同重试、停止或回退，并在日志中披露。

## 模块导航

- [Industry Radar](industry-radar/README.md)：RSS、评分、去重和新闻报告。
- [Quant Strategy](quant-strategy/README.md)：九策略筛选、账本、NAV 与 PIT 回测边界。
- [Scripts](quant-strategy/scripts/README.md)：统一流程、shadow、release 和 backtest 入口。
- [Core](quant-strategy/scripts/core/README.md)：核心安全与金融原语。

完整专有测试、CI、内部审计和验收证据由 private Core 仓库维护；public 仓库不包含这些内部目录，不能把 public checkout 当成完整验证资产。

## License

代码与文档采用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)；商业使用请另行取得授权。
