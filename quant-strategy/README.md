# Global Quant Strategy

该模块把 A 股、港股和美股的基本面/行情筛选、模拟持仓、现金、NAV 与报告连接为可审计流程。它不是只面向 A 股的“双策略脚本”，当前持久化九个策略桶：

| 策略族 | A 股 | 美股 | 港股 |
| --- | --- | --- | --- |
| Dividend | `dividend_a_stock` | `dividend_us_stock` | `dividend_hk_stock` |
| Growth | `growth_a_stock` | `growth_us_stock` | `growth_hk_stock` |
| Hot Spot | `hot_spot_a_stock` | `hot_spot_us_stock` | `hot_spot_hk_stock` |

具体阈值以代码、版本化配置和报告中的 threshold payload 为准，不把 README 中的数字当成永久策略合同。

## 数据和筛选闸门

- Universe 刷新采用原子替换；大幅漂移或 stale fallback 会停止发布。
- A 股行情执行 Baostock/Sina 交叉验证并保留有界 fallback。
- US/HK 批量数据报告 attempted/evaluated/source_errors/coverage，低于覆盖阈值 fail closed。
- 财务数据按披露时间做 point-in-time 过滤，缺字段不能被默认为通过。
- 热点输入绑定新闻报告日期、run identity 和内容哈希，拒绝旧报告复用。
- 新建仓只有在交易时段取得权威、有限正数价格后才占用现金；否则 defer。

## 账本边界

legacy `portfolio`、`trade_history`、`strategy_accounts` 仍是统一日流程的主读写路径。v6 `orders`、`fills`、`journal_transactions`、`journal_entries` 已实现幂等订单、部分成交、整数最小货币单位、复式平衡和故障原子回滚，但尚未取代 legacy 主账本。

关键保护包括：

- SQLite Online Backup 与 writer fence
- strategy cash allocation/release 同事务提交
- A 股 T+1、跨市场交易时段和 pending exit 保护
- 无退出价时保持持仓，不用猜测价格强行成交
- quarantine 只隔离审计指定的原始行，不删除或推测修复
- NAV 使用各市场最近已完成交易日的精确收盘；异常 open 字段可被隔离，但 close 仍必须位于 high/low 内
- 只有与已认证活跃会话完全匹配的当日新仓可暂按权威成本估值；会话不明或未来日期继续 fail closed
- NAV 任一仓位不可权威估值时整笔事务回滚

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
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-run-id \
  --delivery-mode sink
```

八阶段顺序、数据库副本准备、production release、scheduler 与恢复步骤见 [../OPERATIONS.md](../OPERATIONS.md)。脚本级用途见 [scripts/README.md](scripts/README.md)。

## 输出

- `global_screen.json`：带 run 上下文的当前筛选结果
- `strategy_daily_results`：按日期/策略保存的结果与 diff
- `strategy_nav_history`：九策略 NAV/cash/holdings
- artifact root：checkpoint、durable manifest、图表、delivery journal/HTML

这些是运行产物，不应提交到 Git。

## Disclaimer

系统输出仅供量化研究与工程验证，不构成投资建议。
