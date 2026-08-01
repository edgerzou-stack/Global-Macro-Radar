# Quant Strategy Scripts

本目录包含统一日流程、数据适配器、账本、验收、生产迁移和 PIT 回测入口。运行环境固定为 Python 3.11.9；依赖从仓库根 `requirements.txt` 安装。

## 四类入口

### 1. Unified daily flow

生产和端到端验收只从根目录 `run_all.sh` 启动。`daily_runner.py` 顺序执行股票源检查、DB 检查、新闻、筛选、NAV、账本检查、图表和交付，并使用 run identity、writer fence、Online Backup、checkpoint/resume 与 durable manifest。

```bash
./run_all.sh --mode live-shadow \
  --database /absolute/path/to/test-copy.db \
  --expected-source-sha256 SHA256_RECORDED_WHEN_COPY_WAS_PREPARED \
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-run-id \
  --delivery-mode sink
```

历史日期运行必须再传 `--rss-fixture /absolute/path/radar_rss.json`。该单文件
fixture 只固定新闻输入，不会把行情或财报源切换为离线 fixture。

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

`production_release.py` 与 daily flow 分离。默认 copy-only：只读打开源库，通过 SQLite Online Backup 生成副本，在副本应用 v6/v7/v8/quarantine 并验证恢复。生产写入还要求 canonical path、确认 token、fresh audit、writer fence 和指定维护窗口。

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
| 筛选 | `screen_hot_spot.py`, `screen_global_quant.py`, `us_hk_quant.py` | 热点与七策略筛选 |
| 账本/NAV | `db_utils.py`, `calc_nav.py`, `check_ledger_sanity.py` | legacy 账本、精确会话 NAV 与检查 |
| 交割 | `execute_pending_intents.py` | test/backtest 副本按市场精确会话原始开盘价执行到期 intent |
| 报告 | `plot_pnl.py`, `generate_report.py`, `validate_report_html.py`, `send_unified_email.py`, `repair_stock_name_cache.py` | 图表、HTML/Markdown、数据库一致性闸门、outbox 与可验证名称缓存恢复 |
| 验收 | `shadow_runner.py` | 隔离 shadow 与只读 live probes |
| 财报审计 | `audit_hkex_financials.py` | 只读审计 HKEX 官方公告的可用序列与源故障 |
| 迁移 | `production_release.py`, `migrations/` | v6、v7 trade intents、v8 execution evidence、quarantine、copy/restore 演练 |
| 审计恢复 | `rebuild_dividend_ledger.py`, `rebuild_growth_ledgers.py` | 从可审计事件或最新筛选结果重建红利/成长账本的新数据库副本 |
| 回测 | `backtest_engine.py`, `core/backtest.py` | PIT 执行和审计 |

`repair_db_prices.py` 是永久停用的兼容入口。成交价必须保留原始价格，任何历史恢复都
不得以前/后复权收盘价覆盖成交字段。

目录中的 `test_*.py` 诊断脚本不是专有回归套件；正式测试位于 private Core 的 `quant-strategy/tests`、`industry-radar/tests` 和根 `tests`，public 仓库不发布这些专有 tests/CI。

## 运行安全摘要

- mode 和 database 必须显式指定。
- production 日历查询失败时 fail closed。
- `live-shadow` 不等于 `shadow_runner --allow-live-api`。
- `live-shadow` 必须携带准备隔离副本时记录的 `--expected-source-sha256`；手工只改 `database_environment=test` 的副本会被拒绝。
- HTML 中实际持仓、研究候选、v7 待交割、legacy 平仓和 v6 成交必须分开，校验器失败时禁止进入邮件阶段。
- 报告阶段生成两份绑定产物：完整审计 HTML 保留全部账本历史、行情来源和证据
  SHA，并由数据库校验器逐项复核；收件人 HTML 只展示报告日期、账户/NAV、
  本次交割、产业事件、当前持仓、策略候选、待交割和每策略最近 10 笔交割。
  账户台账与认证组合 NAV 曲线位于策略章节之前，便于先看总览再看策略；禁止把
  单笔交易收益率之和标记为组合回测收益。
  运行 ID、UTC 调试时间、内部表版本、provider 状态和证据哈希不得出现在邮件正文。
  两份文件及各自 SHA-256 同时记录在 prepared manifest 中，SMTP 只消费已锁定的
  收件人 HTML。
- `execute_pending_intents.py` 把报告日期作为截止日，逐条读取 intent 原始
  `eligible_session` 的 raw open，并按 run ID 持久化各市场的成交、待交割、尚未
  到期和阻断数量；周末或延迟数日运行不会改变 intent 的成交会话；
  休市或精确 raw open 缺失是可披露的延期状态。`calc_nav.py` 会按策略记录 fresh、
  certified carry-forward 或 unavailable，报告和校验器必须显示并反查同一记录。
- 非 production 强制禁真实订单；所有模式 delivery 默认 sink。
- 完整流水线中的真实 SMTP 需要 production、canonical 数据库写入确认以及独立的
  live-delivery 确认；非 production 的统一流程只能使用 sink。
- 审核后的 sink HTML 可以由邮件 CLI 配合 `--html-file`、
  `--expected-html-sha256` 和 `--confirm-live-delivery` 单独发送。这是只读 artifact
  投递，不重跑策略、不触发数据库写入，也不等同于允许 live-shadow 直接使用 live
  delivery。独立邮件必须使用新的 run ID 和 artifact 目录。
- live journal 为 `accepted_by_smtp` 时只表示出站 SMTP 没有立即拒收，不证明邮件
  已进入收件箱，禁止自动重发；`failed_pre_send` 表示连接/认证阶段失败，
  `rejected_by_smtp` 表示收件人被明确拒绝；在 `sending` 状态断开时结果不确定，
  也禁止自动重试。收件人确认收到后，使用
  `send_unified_email.py --reconcile-confirmed-delivery --confirm-recipient-received`
  并同时提供原 run ID、artifact dir、HTML SHA-256 和收件人，将 journal
  审计核销为 `confirmed_received`；该操作不连接 SMTP。旧版本 `delivered` journal
  继续作为防重发终态读取，但不再用于新发送。
- prepared manifest 必须绑定 `effective_date`，邮件主题以 ` | YYYY-MM-DD` 结尾。
  不同 run ID 对同一收件人和报告日只允许一次非歧义 live 投递；确有独立授权的
  例外重发只能显式使用 `--allow-duplicate-effective-date-delivery`，该参数不会
  绕过同一 run ID 的幂等和 `sending` 歧义保护。
  完整生产重跑应通过顶层
  `scripts/run_production_full_flow.sh --authorized-resend` 入口，使 override
  同时进入 run manifest 和 delivery journal 审计证据。
- scheduler 默认关闭，持久运行需 `--enable-scheduler`。
- 成功以 durable run manifest 和 delivery journal 为准，不只看进程退出码。
- `us_hk_quant.py` 对美股优先读取 SEC Company Facts、对港股优先读取 HKEX 官方业绩公告 PDF；Yahoo 仅作为当日非 PIT 备用源。季度型使用最近5期报表（4期用于验证3次连续营收/利润双增长，第5期用于最新同比），半年型使用最近3期报表验证2次连续双增长并计算同比。决策窗口外的历史标准化异常会隔离并保留审计记录；窗口内异常仍然 fail closed。公司缺表、缺字段、缺期、格式不支持或数据过期会被明确过滤。
- `screen_hot_spot.py` 必须从当前 run-scoped 数据库读取既有持仓。发现模型返回异常、
  行情覆盖率低于 80% 或候选验证失败时会 fail closed，绝不把上游故障解释为空仓信号。
  排序模型必须返回 `{"selected_codes": [...]}`；模型持续异常时使用确定性、低换手率
  fallback，并将 `ranking_provenance` 写入当次热点 artifact。含真实持仓的 LLM 调用
  需要单独的数据外发授权；日常安全验收使用版本化 `HOT_SPOT_FIXTURE`。
- `screen_global_quant.py` 的二次 LLM 过滤默认关闭；只有显式传入 `--enable-llm` 才会
  向外部模型提供候选上下文。生产/验收流程推荐保留默认值，并结合
  `PIPELINE_DISABLE_LLM=1` 关闭报告生成阶段的 LLM。
- SEC loader 会从进程环境读取 `SEC_USER_AGENT`，未显式导出时再读取仓库根 `.env`。值必须包含应用标识和真实联系地址。
- HKEX 官方源可独立执行只读审计：

```bash
PYTHONPATH=quant-strategy/scripts python3 \
  quant-strategy/scripts/audit_hkex_financials.py \
  --effective-date YYYY-MM-DD --workers 4 --max-documents 8 \
  --output-dir artifacts/hkex-audit-YYYYMMDD
```

- 美港股真实源阶段默认使用 4 并发。900 秒是阶段预算下限，实际预算会按市场和股票数
  扩展（美股每 worker-batch 15 秒、港股 120 秒），避免大股票池被固定墙钟预算系统性截断。
  `source_error_reasons` 会区分 `stage_timeout` 与真实 provider 异常。
- 健康度拆分为 `transport_coverage`、`financial_usable_coverage`、
  `decision_coverage` 和 `financial_conflict_rate`。旧 `coverage` 字段仅作为
  `transport_coverage` 的兼容别名；`financial_unavailable` 和
  `financial_conflict` 不再算作有效策略判断。传输闸门默认 80%。依据
  2026-07-27 完整 US/HK 审计，财报可用率默认下限为 US 80%、HK 45%，
  财报冲突率默认上限均为 10%。可通过
  `US_HK_{US|HK}_MIN_FINANCIAL_USABLE_COVERAGE` 和
  `US_HK_{US|HK}_MAX_FINANCIAL_CONFLICT_RATE` 覆盖，但必须有新的完整审计依据，
  禁止为了放行单次运行而调低。
- HKEX PDF 默认保留前10页，同时最多扫描60页寻找正式损益表，仅保留命中页及相邻页，
  避免整份 PDF 文本常驻内存；活动股票 stockId 映射会在进程内复用，公告搜索达到文档预算
  后提前结束。
- HKEX 结果公告使用独立的版本化文档缓存：成功解析保留30天，确定性解析失败
  保留7天；URL、公告身份、PDF SHA-256、解析器版本和缓存记录 SHA-256 必须
  一致。网络/下载失败不缓存，缓存损坏或解析器升级会强制重新获取。
- 可选报告 LLM 点评使用内容寻址缓存，身份同时绑定输入 payload SHA-256、
  prompt 版本、provider 和 model，记录本身另有 SHA-256 完整性校验并以原子写入
  保存30天。输入、prompt、模型变化，记录损坏或 TTL 到期都会重新调用；缓存不可用
  只影响展示增强，不改变筛选、行情、账本或交割。
- 报告点评的 provider DTO、prompt、重试/退避、provider 身份和缓存策略集中在
  `report_review_service.py`；`generate_report.py` 只把返回的结构化点评转成经过
  HTML escaping 的展示内容。内部策略字段不会因 renderer 增加字段而自动外发。
- durable run manifest 的 `telemetry` 汇总每个命令/阶段耗时和
  `hkex_document_cache`、`a_share_close_pair_cache`、`deep_dive_cache`、
  `report_review_cache` 命中指标；`outcome` 明确区分正常完成与降级完成，
  `status=completed` 保留为恢复/幂等兼容字段。成功和失败 manifest 都保留可用的
  pre-run backup 路径与 SHA-256。
- manifest 的 `stage_inputs` 只记录已知输入产物的路径、大小、SHA-256 和 JSON
  顶层形状，并为筛选、热点和 prepared manifest 记录版本化语义摘要（策略数、
  候选数、新闻数和状态），用于解释 A/B 性能与输入漂移；不复制新闻、候选、持仓
  或账户正文。
- 缓存遥测同时记录命中次数、`saved_external_calls` 和 parser/policy/prompt 版本。
  至少积累10个可比 completed manifest 后，runner 才建立 phase p50/p95 基线；
  样本不足明确为 warming-up，超过有界相对及绝对阈值才生成 performance alert。
- 报告数据库读取统一经过 `report_repository.py` 的 SQLite `mode=ro`、
  `query_only=ON` 入口。portfolio、trade history、daily results、账户、NAV/交割状态
  与执行计数、pending/filled intents、v8 evidence 和 legacy link 在一个读事务中
  冻结成 snapshot；renderer 只能修改 daily-results 独立副本。SHA-256、eligible
  session、raw open、SELL_ALL/history 精确关联全部在 repository 暴露快照前 fail closed。
- Markdown 与 HTML 报告统一消费不可变 `ReportViewModel` 和同一章节声明；完整文档
  golden 会同时验证热点新闻、运行身份和成交证据，防止邮件编排改动只让一种输出丢段。
- 股票名称缓存禁止 `symbol -> symbol` 自映射。报告持仓、pending、filled、legacy
  与 unified 路径必须统一走 `display_stock_name()`；没有可信名称时显示
  “名称待同步”，不得让代码同时充当名称。可从已完成且哈希绑定的历史收件人报告
  恢复本地缓存，默认仅预览：

```bash
PYTHONPATH=quant-strategy/scripts python3 \
  quant-strategy/scripts/repair_stock_name_cache.py \
  /absolute/path/to/completed-run/run-manifest.json
```

  人工核对 dry-run 后才可增加 `--apply`。该工具只更新
  `quant-strategy/ticker_names.json`，不会写数据库；manifest、delivery journal、
  run identity、report path 或 HTML SHA-256 任一不一致都会 fail closed。
- `core/data_gateway.py` 只保留缓存、fallback 与兼容 API 编排；BaoStock/Sina/
  YFinance 和 Tencent/Sina exact-session transport 位于
  `core/market_data_adapters.py`，symbol routing、market contract 与 OHLC/close
  normalization 分别独立。所有 provider 边界异常使用版本化 error envelope，
  同时保留既有 breaker、timeout、HFQ 和 fail-closed 语义。
- BaoStock socket adapter 会把服务端在协议 terminator 之前返回的 EOF
  (`recv() == b""`) 转为可重试的 provider failure。不得移除该 guard；上游 SDK
  会在 `CLOSE_WAIT` 上永久空读，造成 NAV 阶段无上限挂死。
- 账本安全检查会对低于 -35% 的真实已实现亏损告警，但不会仅因为超过经验阈值而判定账本损坏；非有限 PnL、数学上不可能的亏损和非正执行价仍会熔断。
- v8 成交必须在更新账本的同一事务写入 provider、原始报价 payload 与 SHA-256 证据；
  等价 pending intent 跨 run 保留原 eligible session，缺少 raw open 时只延期，不撤销重建。
- 同一 run identity 下重启会跳过 checkpoint 中已完成的命令；失败会留下
  `interrupted` checkpoint 和 `failed` run manifest。run-id、日期、数据库或配置身份
  不匹配时不得复用。
- HTML 强校验与报告生成共用 quarantine 和 test-strategy 过滤口径，持仓、账户与 legacy 平仓记录必须逐项一致。
