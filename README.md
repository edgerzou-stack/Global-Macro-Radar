# Global Macro Radar

Global Macro Radar 是一个把实时产业新闻、全球多市场筛选、模拟账本、NAV、图表和报告交付串成单一可审计流程的研究系统。它覆盖 A 股、港股与美股，默认不提交真实订单，默认把交付写入本地 sink。

> 本项目仅用于研究与工程验证，不构成投资建议。

## 系统做什么

`run_all.sh` 顺序执行八个阶段：

1. 股票 API 健康检查与 A 股双源交叉验证
2. SQLite 物理完整性与账户约束检查
3. RSS 拉取、健康闸门、新闻去重与 LLM 双轨评分
4. Universe 刷新、热点筛选和 A/HK/US 全球量化筛选
5. 模拟账本结算与七策略 NAV
6. 账本 sanity check
7. NAV/PnL 图表
8. 报告生成与 delivery outbox

量化层运行七个策略桶：A 股红利、A/HK/US 成长与 A/HK/US 热点。
`dividend_hk_stock`、`dividend_us_stock` 已退役，其历史空壳与重复日结果仅通过
quarantine 排除，不删除原始审计证据。新闻层先输出四个 0–100 整数子分，再按配置
聚合为 Innovation/Traffic 两条 0–10 composite；来源可信度作为独立元数据，不会
自动给内容分加分。每篇新闻还必须提供可验证的产业事件类型与产业事实；纯股价、
市值、成交额、评级和资金流向不能被高 Traffic 分单独救活。

## 安全模型

| Mode | 数据库 | 真实订单 | Delivery | Fixture |
| --- | --- | --- | --- | --- |
| `offline` | 隔离 backtest DB | 禁用 | sink | 完整 bundle 必填 |
| `shadow` | 隔离 test DB | 禁用 | sink | 可选 |
| `live-shadow` | 隔离 test DB | 禁用 | sink | 当日可用真实源；历史日期必须提供 RSS 快照 |
| `production` | canonical production DB | 受显式确认与 fence 保护 | 默认 sink | 可选 |

所有入口都要求显式 mode/database。production 还要求 canonical path 和二次写入确认。
完整流水线中的真实 SMTP 只能由 production 模式配合
`--delivery-mode live --confirm-live-delivery` 启用；已经通过 sink 与数据库一致性审核的
HTML，也可以由邮件 CLI 在不重跑策略、不写生产库的情况下单独发送。完整审计 HTML
与精简收件人 HTML 分开保存：前者保留数据库交叉校验所需的历史和证据，后者优先
展示本次交割、NAV、持仓、候选和产业事件，不包含运行日志及证据哈希。两条路径都由
邮件发送器再次校验显式确认。scheduler 默认关闭。

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

真实源验收使用 `live-shadow`。如果种子来自包含 legacy 异常的生产库，先用 `production_release.py` 默认 copy-only 模式生成带 v6/v7/quarantine 的 `working_copy.db`，不要加入 `--apply-production`：

```bash
python3 quant-strategy/scripts/production_release.py \
  --source-db /absolute/path/to/quant_system.db \
  --audit /absolute/path/to/production_db_audit.json \
  --output-dir /absolute/path/to/new-release-dir

./run_all.sh \
  --mode live-shadow \
  --database /absolute/path/to/new-release-dir/working_copy.db \
  --expected-source-sha256 SHA256_RECORDED_IN_RELEASE_MANIFEST \
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-live-shadow-run \
  --delivery-mode sink
```

copy-only release 会自动把 `working_copy.db` 标记为 `test`，同时在
`meta_data` 中保留来源环境与生产库 SHA-256；不要手工改写该标签，也不要把
`pre_release_backup.db` 作为 live-shadow 的可写数据库。
`live-shadow` 必须显式传入 release manifest 中记录的 source SHA-256；运行器会在
备份和任何业务阶段之前核对数据库 provenance，不匹配时立即停止。
历史日期的 `live-shadow` 还必须用 `--rss-fixture /absolute/path/radar_rss.json`
绑定当日 RSS 快照；文件路径和 SHA-256 会进入 run configuration identity。实时 feed
无法重建过去的新闻集合，因此缺少快照时会在 Phase 1 前停止。

成功不能只看退出码：同时核对 release/run manifest、delivery journal、数据库完整性和生产库前后哈希。完整的恢复、调度、Docker 与维护窗口协议见 [OPERATIONS.md](OPERATIONS.md)。

报告中的“实际持仓”只来自 SQLite `portfolio`，“本次筛选候选”仅是研究池，
“待交割指令”来自 v7 `trade_intents`。筛选不会即时成交；每条 intent 在生成时
固定下一市场交易日为 `eligible_session`。以后无论隔 1、2 或 7 天才运行报告，
执行器都只使用该原始交易日的 raw open；缺价继续 pending，后续筛选不能取消承诺。
同一交易日尚未到官方开盘时，执行器将其归类为“尚未到期”，不会提前请求开盘价，
也不会误报为行情缺失。执行仍遵循 SELL-first、A 股 T+1 和每策略最多 10 只规则。
既有持仓仍在本次目标列表中且具备可验证持有期收益时，固定份额加仓规则允许第 1 份
在累计回撤达到 -10% 后增加第 2 份，第 2 份在累计回撤达到 -15.5% 后增加第 3 份；
最多 3 份。缺少收益证据、当日新仓或已满 3 份均不生成加仓意图。加仓也必须先生成
`ADD_TRANCHE`，再以最早交割日的 raw open 原子更新现金、持仓均价和 v8 证据。
`trade_history` 明确标为 legacy 内部账本，不再冒充券商交割。发送前
`validate_report_html.py` 会把 NAV、现金、持仓、待交割意图、历史平仓和 v6 orders/fills 与同一
数据库逐项核对，任何不一致都会阻断 delivery。

已审核的 sink HTML 可以通过邮件 CLI 的 `--html-file`、
`--expected-html-sha256` 与 `--confirm-live-delivery` 参数单独投递，不需要重跑
策略或改写数据库。发送器默认加载仓库根目录 `.env`，并将 data URI 图表转换
为 CID inline parts；具体 canary 和失败恢复流程见 [OPERATIONS.md](OPERATIONS.md)。

推荐流程是先以 sink 完成整条流水线，验证 manifest、数据库和 HTML，再把该次
artifact 中的同一份 HTML 用 SHA-256 锁定后发送。必须给独立邮件使用新的 run ID
和 artifact 目录，避免与 sink journal 冲突：

```bash
HTML_FILE=/absolute/path/to/artifacts/pipeline-run/delivery/pipeline-run.html
HTML_SHA="$(shasum -a 256 "$HTML_FILE" | awk '{print $1}')"

python3 quant-strategy/scripts/send_unified_email.py \
  --mode live \
  --confirm-live-delivery \
  --run-id unique-reviewed-html-mail-run \
  --artifact-dir /absolute/path/to/artifacts/reviewed-html-mail \
  --html-file "$HTML_FILE" \
  --expected-html-sha256 "$HTML_SHA" \
  --effective-date YYYY-MM-DD
```

邮件 journal 必须按以下语义处理：`accepted_by_smtp` 只表示出站服务器没有立即
拒收，不证明邮件已进入收件箱，禁止自动重发；只有收件人确认后才能核销为
`confirmed_received`。`failed_pre_send` 表示发送前失败；`rejected_by_smtp` 表示
SMTP 明确拒绝收件人；`sending` 表示结果不确定，禁止自动重试，必须先向收件人或
SMTP 服务商核实。旧版本的 `delivered` 只作为防重发的兼容终态保留，不能再解释为
最终送达。不能只根据进程退出码宣称收件人已收到。

正式邮件采用 `icloud_safe_v1` / MIME v3：主题默认使用 ASCII，正文同时包含可用的
纯文本版本；图表以最大宽度 1600 像素、每张不超过 128 KiB 的 CID PNG 投递；
序列化邮件超过 512 KiB 时在连接 SMTP 前失败。纯文本 canary 只验证投递路由，
不能代替正式日报的实际收件确认，也不能用于核销生产投递 journal。

## 可靠性边界

- 新建仓必须取得权威、有限正数且处于真实交易时段的价格，否则 defer。
- 无退出价时保留持仓，不用估算价强行成交。
- 研究候选可以超过 10 只，但每个策略最多持有 10 只有效股票；pending exit 继续
  占用名额，超限的新建仓整体 defer，不允许部分写入后留下不一致账本。
- NAV 使用各市场最近正式完成交易日的精确 close；尚未收盘的 session 不会被当成已有 daily close。
- 严格 OHLC 失败时，non-A NAV 只允许隔离与估值无关的坏 open 字段；close 仍必须为有限正数且位于 high/low 内，降级数据不写入 OHLC cache。
- 报告生成与市场交割解耦：休市、盘前或精确开盘价暂不可得时，交割意图保持
  `PENDING`，正式报告仍可生成并如实披露本次成交/延期数量。NAV 按策略独立处理：
  本次可权威重估则写入新快照；否则仅沿用最近一笔未隔离且满足
  `nav = cash + holdings_value` 的认证快照；没有认证快照则明确显示“不可估值”，
  不伪造当日 NAV。
- 报告顶部只保留使用认证 NAV 快照的账户净值图。旧版
  `pnl_chart_all.png` 将每笔交易收益率直接相加，不能表示组合收益，已停止生成和
  展示。“本次目标变化（尚未成交）”仅列当前 run 新产生的
  `PENDING` 意图，不把历史遗留意图伪装成今日变化，也不展示尚不存在的成交价或 PnL。
- 热点 LLM 的结构化响应失败不能解释为空选股或清仓信号；热点行情覆盖不足时 fail
  closed，排序降级必须保留 provenance，并优先降低无必要换手。
- 成长策略二次 LLM 默认关闭。只有明确完成数据外发授权后才允许
  `--enable-llm`；安全验收使用 `PIPELINE_DISABLE_LLM=1` 和版本化 fixture，避免把
  内部候选、持仓或 NAV 发送给外部模型。

## 模块导航

- [Industry Radar](industry-radar/README.md)：RSS、评分、去重和新闻报告。
- [Quant Strategy](quant-strategy/README.md)：七策略筛选、账本、NAV 与 PIT 回测边界。
- [Scripts](quant-strategy/scripts/README.md)：统一流程、shadow、release 和 backtest 入口。
- [Core](quant-strategy/scripts/core/README.md)：核心安全与金融原语。

完整专有测试、CI、内部审计和验收证据由 private Core 仓库维护；public 仓库不包含这些内部目录，不能把 public checkout 当成完整验证资产。

## License

代码与文档采用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)；商业使用请另行取得授权。
