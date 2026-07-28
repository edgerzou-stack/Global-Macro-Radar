# Global Quant Strategy

该模块把 A 股、港股和美股的基本面/行情筛选、模拟持仓、现金、NAV 与报告连接为可审计流程。当前运行七个策略桶；美股、港股红利策略已经退役，不再创建账户、NAV、信号或交割意图：

| 策略族 | A 股 | 美股 | 港股 |
| --- | --- | --- | --- |
| Dividend | `dividend_a_stock` | — | — |
| Growth | `growth_a_stock` | `growth_us_stock` | `growth_hk_stock` |
| Hot Spot | `hot_spot_a_stock` | `hot_spot_us_stock` | `hot_spot_hk_stock` |

具体阈值以代码、版本化配置和报告中的 threshold payload 为准，不把 README 中的数字当成永久策略合同。

### 当前 A 股策略边界

- 红利策略要求正 PE/PB、估值公式值 `< 10`、总市值 `> 100亿元`、三年净利润
  CAGR `> 5%`、三年平均 ROE `> 5%`、三年营收与净利润连续双增长、三年经营
  现金流平均增速 `> 0`、现金流利润覆盖 `>= 0.8`，并通过股息率与分红连续性质量
  闸门。三年平均净利率和资产负债率只展示、审计，不参与筛选；结果按股息率排序，
  每行业最多 3 只、研究候选最多 50 只。
- 成长策略要求总市值 `> 100亿元`、PE 存在且 `> 0`、最近三个连续季度营收和
  净利润环比均为正，且最新一期营收同比与净利润同比均严格大于 PE。行业、三年
  CAGR、三年连续双增长、ROE、净利率和负债率均不是成长策略硬门槛。

## 数据和筛选闸门

- Universe 刷新采用原子替换；大幅漂移或 stale fallback 会停止发布。
- A 股历史行情优先使用 Baostock/Sina。若主源返回了非空但缺少请求终点的
  部分序列，仍视为失败，不能把上一交易日价格冒充当日收盘。仅在交易所收盘后，
  才允许 Tencent 日 K 与 Sina 实时报价按代码、日期、前收盘和收盘价交叉确认后
  补齐精确会话收盘；除权除息日还必须取得 Tencent corporate-action 标识与双源
  一致的除权参考前收盘，才能从上一期后复权锚点推导当日后复权值。
- US/HK 批量数据分别报告 transport coverage、financial usable coverage、
  decision coverage 与 financial conflict rate。财报缺失或冲突不再被计入有效策略判断；
  transport 低于阈值时 fail closed；基于 2026-07-27 完整审计，财报可用率
  默认下限为 US 80%、HK 45%，财报冲突率默认上限均为 10%。
- 财务数据按披露时间做 point-in-time 过滤，缺字段不能被默认为通过。
- 热点输入绑定新闻报告日期、run identity 和内容哈希，拒绝旧报告复用。
- 热点策略从当前 run-scoped 数据库读取现有持仓，不从旧 JSON 推测账本状态；
  任一市场的候选行情覆盖率低于 80% 时 fail closed。LLM 发现失败不能解释成三个
  空卖出信号；排序返回值必须满足对象合同，连续失败时按“仍在候选中的既有持仓优先、
  再按已验证候选顺序”确定性降级，并把排序 provenance 写入 artifact。
- 筛选只生成 v7/v8 `trade_intents`，不再用报告生成时的“最新价”冒充成交价。每个市场
  在下一 eligible session 使用该日未复权原始开盘价独立交割；缺价保持 `PENDING`，
  等价信号跨 run 保留原 eligible session，不得通过撤单重建无期限顺延。

## 账本边界

legacy `portfolio`、`trade_history`、`strategy_accounts` 仍是模拟组合的权威状态。v7
`trade_intents` 是筛选与 legacy 状态之间的交割队列；只有执行器取得精确会话原始
开盘价后，才在一个事务中更新意图、持仓、现金、平仓历史与快照。v8
`trade_execution_evidence` 同事务保存数据提供方、原始 payload 及 SHA-256；历史
v7 成交只能标记为低保证 legacy 证据，不能伪装成已验证原始开盘价。v6 `orders`、
`fills`、`journal_transactions`、`journal_entries` 尚未切换成 tranche 主路径。

关键保护包括：

- SQLite Online Backup 与 writer fence
- strategy cash allocation/release 同事务提交
- 卖出先于买入；延期退出继续占用持仓槽位；A 股 T+1 自动顺延下一交易日
- 无退出价时保持持仓，不用猜测价格强行成交
- 研究候选可以超过 10 只，但每个策略按候选原始排名最多持有 10 只；延期退出继续占用名额，超过上限的数据库写入整笔回滚
- quarantine 只隔离审计指定的原始行，不删除或推测修复
- NAV 使用各市场最近已完成交易日的精确收盘；异常 open 字段可被隔离，但 close 仍必须位于 high/low 内
- 只有与已认证活跃会话完全匹配的当日新仓可暂按权威成本估值；会话不明或未来日期继续 fail closed
- NAV 按策略隔离：行情暂不可得时不写伪造的当日快照，只能沿用最近一笔未隔离且
  满足账本恒等式的认证快照；没有合格快照的策略明确标为“不可估值”。现金、
  持仓数量、成交价或历史 NAV 恒等式损坏仍会使整次运行失败。
- 休市、盘前、跨市场尚未开盘或权威 raw open 缺失只会延期对应
  `trade_intents`，不会阻止生成经过数据库反查的正式报告或邮件 artifact。

### 红利旧账恢复

`dividend_a_stock` 的 2026-07 历史账本曾被后复权收盘价污染。禁止用复权价回写
`portfolio.entry_price` 或 `trade_history` 的成交字段；`repair_db_prices.py` 已永久停用。
恢复必须使用版本化事件清单和只写新副本的工具：

```bash
PYTHONPATH=quant-strategy/scripts python3 \
  quant-strategy/scripts/rebuild_dividend_ledger.py \
  --source-db /absolute/path/to/quant_system.db \
  --output-db /absolute/path/to/new-dividend-ledger.db \
  --report /absolute/path/to/reconciliation.json
```

该命令校验双源原始 OHLC、事件顺序、现金/NAV 闭合、其他策略逐行摘要以及
`strategy_daily_results` 整表摘要，并把输出固定为无 WAL 依赖的独立数据库。它不会
原地修改源库；生产替换仍受 writer fence、备份和维护窗口约束。

### 成长旧账恢复

成长 A/美/港策略的旧账如果无法逐笔证明真实 eligible session 与原始开盘价，禁止
直接沿用成本或用当前行情倒推修补。使用只写新副本的重建工具归档旧状态、重置三个
成长账户，并从最新已保存筛选结果重新建立待成交意图：

```bash
PYTHONPATH=quant-strategy/scripts python3 \
  quant-strategy/scripts/rebuild_growth_ledgers.py \
  --source-db /absolute/path/to/quant_system.db \
  --output-db /absolute/path/to/new-growth-ledger.db \
  --report /absolute/path/to/growth-reconciliation.json
```

新意图只能在下一合法交易日以精确 raw open 交割；缺价保持 pending。工具会验证其他
策略逐行摘要、SQLite 完整性与外键，并启用成长策略现金重放检查。生产库替换仍是独立
放行动作，不能由重建或 live-shadow 自动完成。

## 回测正确性

PIT 回测合同要求：

- 信号在下一交易日 open 执行，close 估值
- fee、slippage、逐日 FX 进入现金流
- fundamentals、指数成分和 corporate action 必须带公告/生效时间
- split、dividend、delisting recovery 显式处理
- 数据集 provenance、manifest 和结果写入独立 audit DB
- 修改未来数据不能改变过去 NAV

缺失真实 PIT 数据集时，fixture 结果只证明引擎合同，不代表策略收益。

## 正确运行入口

不要手动串联 `screen_a_share.py`、`plot_pnl.py` 和 `generate_report.py` 作为生产流程。统一入口在仓库根目录：

```bash
./run_all.sh --mode live-shadow \
  --database /absolute/path/to/test-copy.db \
  --expected-source-sha256 SHA256_RECORDED_IN_RELEASE_MANIFEST \
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-run-id \
  --delivery-mode sink
```

如果 effective date 早于运行日，必须增加
`--rss-fixture /absolute/path/to/radar_rss.json`；否则流程会在备份和 Phase 1 前
fail closed，避免用今天的新闻伪装成历史点时输入。

成长策略的二次 LLM 过滤默认关闭，因为候选与持仓上下文不应在未授权时离开本机；
只有完成数据授权与供应商审查后，才可对 `screen_global_quant.py` 显式使用
`--enable-llm`。`PIPELINE_DISABLE_LLM=1` 会同时关闭报告阶段的 LLM。热点策略的
真实 LLM 发现也会携带新闻与既有持仓；安全回归应设置版本化 `HOT_SPOT_FIXTURE`，
而不是把内部持仓发送给外部模型。fixture 仅用于流程和账本验收，不代表当日投资信号。

### 审核后真实发送邮件

`live-shadow` 必须保持 sink，不能用真实发信作为绕过 production 写保护的理由。
完整流水线的 live delivery 只能运行在 canonical production 数据库与 production 模式。
如果只需要发送一份已经通过 `validate_report_html.py` 和 sink 验收的报告，应锁定
artifact 中 HTML 的 SHA-256，并使用新的邮件 run ID 单独发送；该操作不运行选股、
不结算账本，也不写数据库：

```bash
HTML_FILE=/absolute/path/to/artifacts/run-id/delivery/run-id.html
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

发送前必须确认 run manifest 为 `completed`、HTML validator 为 `status: ok`、sink
journal 正常且生产数据库哈希未变化。live journal 为 `accepted_by_smtp` 时只证明
出站服务器未立即拒绝，并不证明进入收件箱；禁止自动重发。收件人确认后才核销为
`confirmed_received`。`sending` 表示结果不确定；`failed_pre_send` 表示 SMTP
接受前失败；`rejected_by_smtp` 表示 SMTP 明确拒绝收件人。旧 `delivered` journal
仅作为防重发兼容记录。完整恢复与核销流程见 [../OPERATIONS.md](../OPERATIONS.md)。

八阶段顺序、数据库副本准备、production release、scheduler 与恢复步骤见 [../OPERATIONS.md](../OPERATIONS.md)。脚本级用途见 [scripts/README.md](scripts/README.md)。

## 输出

- `global_screen.json`：带 run 上下文的当前筛选结果
- `strategy_daily_results`：按日期/策略保存的结果与 diff
- `strategy_nav_history`：七个在役策略的 NAV/cash/holdings
- artifact root：checkpoint、durable manifest、图表、delivery journal/HTML

阶段失败时 checkpoint 状态为 `interrupted`，并生成 `status=failed` 的
`run-manifest.json`，其中记录失败命令、退出码及已完成步骤；它仍可在完全相同的
run identity 下显式恢复。

`live-shadow` 的数据库必须由受控 copy/retag 流程生成，并用
`--expected-source-sha256` 锁定来源。HTML 将实际持仓、研究候选、legacy 平仓记录和
v6 成交、v7 待交割指令分开展示；邮件阶段之前会自动执行数据库一致性校验。

### 独立交割执行

统一流程会在筛选前处理截至报告日期已经到期的 v7 意图。报告日期只是执行截止日；
每条 intent 始终读取自身原始 `eligible_session` 的 raw open。因此隔多日或在周末
运行也不会把报告日期、当前价或前收盘价当成成交价。缺价时 intent 原样保持
`PENDING`，后续筛选也不能取消这个承诺。运维排障可在隔离 test/backtest 副本上
单独执行；脚本明确拒绝 production 标记的数据库：

```bash
python3 quant-strategy/scripts/execute_pending_intents.py \
  --database /absolute/path/to/test-copy.db \
  --session-date YYYY-MM-DD
```

重复运行同一意图不会重复成交。研究候选可超过 10 只，但执行目标只取原始排名
前 10 只；未成交候选不会出现在“实际持仓”中。

这些是运行产物，不应提交到 Git。

## Disclaimer

系统输出仅供量化研究与工程验证，不构成投资建议。
