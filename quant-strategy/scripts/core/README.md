# Quant Strategy Core

`core` 提供市场时间、数据完整性、持仓现金、执行账本、quarantine、run identity 和 PIT 回测原语。模块由统一流程或测试调用，不建议直接执行。

## 组件

| 模块 | 责任 |
| --- | --- |
| `run_context.py` | mode/database/run-id/config-hash、artifact envelope、checkpoint、fixture bundle |
| `writer_lock.py` | 同一数据库 writer fence 与 release/runner 互斥 |
| `clock.py`, `market.py` | 可测试时钟、交易日/时段、最近已完成交易日 |
| `data_gateway.py` | A 股多源 fallback、US/HK 适配、严格 OHLC/close 校验、cache、circuit breaker |
| `data_anomaly.py` | 数据异常类型与 fail-closed 语义 |
| `db_manager.py`, `db_backup.py` | 显式数据库连接和 SQLite Online Backup |
| `cash_manager.py` | strategy 账户、tranche allocate/release |
| `portfolio.py` | legacy 持仓 diff、T+1、止损/加仓、pending exit、新建仓价格闸门 |
| `position_math.py` | harmonic cost、PnL 与复权完整性 |
| `execution_ledger.py` | v6 order/fill/double-entry journal 原子事务 |
| `quarantine.py` | audit 指定行的读路径排除和主键校验 |
| `backtest.py` | next-session-open、close valuation、fee/slippage/FX/action/delist |
| `ttl_cache.py` | 进程内有界 TTL cache |
| `strategy.py`, `market.py` | 策略与市场抽象 |

## 两套账本的现实边界

统一日流程仍使用 legacy `portfolio`、`trade_history` 和 `strategy_accounts`。v6 execution ledger 已具备更强的订单状态机、幂等键、部分成交、整数最小货币单位和复式分录，但尚未成为主账本。不能把“v6 测试通过”描述成生产流程已完全切换。

## 关键不变量

- 缺失、非有限、非正数或非交易时段价格不能创建仓位或占用现金。
- 无权威退出价时保留持仓为 pending，不猜测成交。
- NAV 只使用精确会话 close；当前尚未收盘时取最近已完成 session。
- 只隔离与 NAV 无关的坏 open 字段；close 必须仍在 high/low 范围内。
- 任一 NAV 不可估值时整轮回滚。
- quarantine 行不可被 pending repair、报告或现金路径重新引入。
- order、fill 和 journal 在故障注入时共同回滚，复式分录必须按币种平衡。
- fixture 和 test DB 不得回退到生产路径或网络。

专有回归测试位于 private Core 的 `quant-strategy/tests/core` 及相邻测试目录；public 运行核心不包含这些测试和 CI。
