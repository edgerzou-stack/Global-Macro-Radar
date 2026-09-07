# Industry Radar

Industry Radar 负责采集产业新闻、执行 RSS 健康闸门、去重、LLM 评分并生成 Markdown 报告。它是统一流程的第三阶段；standalone `main.py` 只生成报告，最终交付由根流程的 delivery outbox 处理。

## 数据与评分合同

每篇通过预筛的新闻由模型返回四个 0–100 整数子分：

- `tech_breakthrough`：技术突破强度
- `commercial_impact`：中长期商业影响
- `market_attention`：市场与资本关注
- `time_sensitivity`：时效与短期催化

配置权重把子分聚合成两条 0–10 composite：Innovation 与 Traffic。分值必须有限、在范围内且每个输入恰好对应一个输出；缺失、重复或结构错误会重试并最终 fail closed。

评分同时输出事件类型、可独立成立的 `industrial_claims` 和应剥离的
`market_only_claims`。纯股价、成交额、市值、指数、券商评级或资金流向不能仅凭
Traffic 分进入报告；公司属于科技行业并不等于该篇文章构成产业事件。明确的技术、
产品、产能/资本开支、供应链、产业政策、商业部署及说明资金用途的融资事件会进入
详细评分。对同时包含股价和产业事实的文章，只保留可验证的产业事实。

`source_confidence` 用于记录来源可信度，不会自动给内容分加分。`heuristics.yaml` 中未连接到运行路径的 fatigue 参数不应视为有效规则。当前默认回看窗口由配置决定，不能据此保证所有文章都在 24 小时内。

主新闻列表继续使用配置的 Innovation/Traffic 分数阈值。`Research Watch` 是独立的
证据成熟度通道，不是“差一点到 8 分”的救援桶：它只接纳具备具体产业事实、但尚处
工程测试、未来里程碑或 discovery-only 阶段的研究线索，并将
`research_watch_decision`、证据状态、产业里程碑和生产状态写入文章结果供审计。
融资消息、政策预告和缺少一手支持的常规二手商业部署不能仅凭分数进入该通道。

## RSS 可靠性

`source_registry.yaml` 是 RSS 元数据的唯一事实源。每个 feed 必须声明 T0-T3
证据等级、evidence/discovery/research 通道、权威事件类型和交易证据资格；
`config.yaml`/`config.example.yaml` 的 `rss_feeds` 必须与注册表严格一一对应，
否则采集在启动时直接失败。

报告会同时生成 `hotspot_evidence_YYYY-MM-DD.json`。该文件绑定 Markdown
报告的绝对路径和 SHA-256，仅包含满足确定性 T0 一手证据政策的事件。T1
公司自述和 T2/T3 媒体或社交线索仍可进入报告，但默认仅供研究；没有合格
事件时会明确发布 `no_change`，量化热点策略不得据此清仓或换仓。

`ingest.py` 并发拉取配置中的全部 feed，并检查：

- HTTP/content-type 和 feed 解析结果
- UTC aware 发布时间与 freshness
- transport/parse availability ratio、fresh source count 与 fresh article count
- 单一来源的新内容占比与入选新闻来源集中度
- 命名 critical source groups 的 transport availability 与独立内容时效 SLA
- `ai`、`semiconductor`、`biotech`、`energy`、`space` 等必需行业域是否各自拥有可用的一手源
- fixture 中声明的健康统计与真实文章数一致

零条新文章的可达 feed 会标为 degraded/quiet，但仍计入 `available_sources`，不会被
误报为网络不可用；`healthy_sources` 只保留为更严格的内容窗口诊断，不再承担 transport
闸门。关键源组可以用
`content_max_age_hours` 为低频高质量媒体设置明确时效 SLA；聚合健康、可访问源数量或
内容时效不达标时整轮停止。源数量和成员以本机 `config.yaml` 为准；确认长期陈旧的
feed 应由官方一手技术源替换，而不是通过降低 freshness 门槛掩盖。

当前一手目录还包含 NIST、SEC 与 FDA 的官方 RSS。一手源并不因为“官方”就自动
获得高分：注册表的 `authority_for` 必须与评分事件词表一致，且内容仍需通过产业事件
和具体事实闸门。跨行业确定性里程碑（量产/出货、监管批准、客户认证、商业部署、
扩产、临床读出、流片和原型等）由统一规则识别，不再依赖 CPO 专用关键词。
主榜还会硬性满足 `report_min_primary_supported_ratio`：一手证据供给不足时，移除证据
最弱的二手条目，而不是用高分掩盖证据缺口；被移除数量仍写入 health artifact 并保持
降级告警。RSS 正文中明确引用的 HTTP(S) 链接会在清洗 HTML 前保留；只有同一批次确实
抓到 URL、事件类型和发布时间均匹配的一手原文时，才建立精确佐证。未知链接不会被
自动升级，也不会凭相似关键词猜测。

## LLM 路由与缓存

默认模式为 `offline`，不会读取 API key。四种显式模式为：

- `offline`：冻结 fixture 或严格 cache-only，普通回归费用为零；
- `interactive`：确定性过滤和缓存后，仅把新增疑难文章写入
  `llm-review-request.json`，供 Gemini/Codex 等文件夹工具审阅，项目不发 API 请求；
- `unattended`：只对缓存未命中的少量疑难文章使用已启用 provider，并受文章数、
  API 调用数和每日人民币预算三重限制；
- `deepseek`：兼容原 DeepSeek 工作流，但只构造 DeepSeek 客户端且仍受相同预算。

Provider 顺序由配置决定，只有具备凭证且被启用的 provider 才参与。评分缓存使用
provider 无关的语义身份，绑定规范化 URL、正文、发布时间、来源证据、稳定 prompt、
评分配置及确定性规则哈希；provider/model 只作为审计 provenance。相同身份永久复用，
不再因 30 天 TTL、容量裁剪或更换 provider 重复计费；正文、prompt、来源或规则变化只
使对应文章失效。缓存结果和最终报告仍会再次经过确定性的产业边界闸门。

当前项目策略显式设置 `gemini.enabled: false`，并从 provider 顺序中移除 Gemini；即使本机仍存在 `GEMINI_API_KEY`，也不会创建 Gemini 客户端或发送请求。新闻阶段优先使用 DeepSeek，量化模块使用同一禁用策略。

隔离验收设置 `PIPELINE_DISABLE_LLM=1` 时等同强制 `offline`：只复用
身份和内容哈希均匹配的既有评分；未命中缓存的文章记录为 `unscored`，不进入候选，且
不得合成替代分数。Deep Dive 和 LLM 去重同样跳过。未评分数量写入
`radar_selection_health.json` 的 `llm_disabled_unscored_count`，并由全流程 manifest
升级为 `rss_llm_disabled_unscored_warning`，因此这种受控运行可能安全完成但不能宣称
fully healthy。

市场行情、摘要/合集、广告和没有独立产业事实的明确负例先由确定性规则排除；其余新增
或正文变化的疑难文章直接进入一次详细评分，不再先付费做 LLM pre-filter。报告去重只
使用确定性 URL/正文相似度，不再隐式调用 LLM 合成。Deep Dive 默认关闭，只有显式设置
`RADAR_ENABLE_DEEP_DIVE=1` 且本轮存在新 AI 评分文章时才运行，并继续受统一预算约束。

每轮写出 `radar_llm_usage.json`，并把同一份摘要嵌入 selection health 和 run snapshot。
字段包括 `cache_hit_count`、`cache_miss_count`、`deterministic_count`、
`ai_review_count`、`api_call_count`、`estimated_cost_cny`、
`budget_blocked_count` 和 `manual_review_count`。每日预算按北京时间写入带锁账本，
并发请求不能超支。

交互式审阅分两步，外部 AI 不能直接修改缓存、报告或数据库：

```bash
RADAR_LLM_MODE=interactive RADAR_CONFIG=/absolute/config.yaml python3 main.py
python3 import_manual_review.py \
  --request /run/llm-review-request.json \
  --response /review/response.json \
  --output /run/radar_scored_articles.json
```

导入器严格校验 request hash、文章集合、响应 schema 和 reviewer 身份；输出复用既有
`RADAR_SCORED_ARTICLES_FIXTURE` 离线渲染契约。付费 `llm_eval` 只能通过
`run_llm_eval.py` 显式运行，必须同时给出确认 token、最大文章数、最大 API 调用数和
每日人民币预算；普通 pytest、CI 和 release checks 都不运行它。

## 运行

推荐从仓库根目录运行完整流程：

```bash
./run_all.sh --mode live-shadow \
  --database /absolute/path/to/test-copy.db \
  --expected-source-sha256 SHA256_RECORDED_WHEN_COPY_WAS_PREPARED \
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-run-id \
  --delivery-mode sink
```

历史 effective date 不允许读取当前 RSS；必须额外传入
`--rss-fixture /absolute/path/to/radar_rss.json`。fixture 中晚于 effective date 的
文章或健康时间戳会被拒绝。

仅调试新闻模块时：

```bash
RADAR_CONFIG=/absolute/path/to/config.yaml python3 main.py
```

这条命令不会执行量化、账本、NAV 或统一交付。离线测试应使用版本化 `RADAR_RSS_FIXTURE` 与 `RADAR_SCORED_ARTICLES_FIXTURE`，不得回退到网络。

## 评测边界

synthetic golden set 只用于结构和边界回归，不能证明现实新闻上的排序质量。生产校准应至少使用 100 条真实、时间分层且覆盖正负样本的新闻，由两名标注者独立评分并对分歧仲裁，同时报告误拒、误收、排序相关性和 provider 漂移。

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `main.py` | 兼容入口；只编排 ingestion → scoring/deep-dive → rendering |
| `ingest.py` | RSS 抓取、freshness 与健康统计 |
| `pipeline_ingestion.py` | fixture/live RSS 输入选择、健康闸门与预去重 |
| `pipeline_health.py` | RSS 与 critical source group 健康合同 |
| `pipeline_selection.py` | 报告准入、分类、排序和 top-N 限制 |
| `evidence_policy.py` | 确定性的来源证据、成熟度与 Research Watch 决策 |
| `pipeline_scoring.py` | 增量缓存、预筛、批量评分与结果数 fail-closed |
| `pipeline_deep_dive.py` | verified-primary 深挖并行编排及负缓存 |
| `pipeline_rendering.py` | 无 provider 依赖的 Markdown 报告渲染 |
| `pipeline_delivery.py` | standalone legacy SMTP 边界；统一流程不调用 |
| `score.py` | 评分 schema、prompt 与批处理 |
| `llm_router.py` | 配置驱动的 provider 路由 |
| `cache_manager.py` | 版本化增量缓存 |
| `deep_dive.py` | 基于可核验一手来源的高阈值事件深挖 |
| `provider_errors.py` | 共享结构化 provider error contract 的 Radar 门面 |
| `config.example.yaml` | 可提交的配置模板 |

## License

代码与文档采用 CC BY-NC 4.0；商业使用请另行取得授权。
