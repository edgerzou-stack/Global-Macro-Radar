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

## RSS 可靠性

`ingest.py` 并发拉取配置中的全部 feed，并检查：

- HTTP/content-type 和 feed 解析结果
- UTC aware 发布时间与 freshness
- healthy source ratio、fresh article count
- 单一来源的新内容占比与入选新闻来源集中度
- 命名 critical source groups 的 transport availability 与独立内容时效 SLA
- fixture 中声明的健康统计与真实文章数一致

零条新文章的可达 feed 会标为 degraded，但不会被误报为网络不可用。关键源组可以用
`content_max_age_hours` 为低频高质量媒体设置明确时效 SLA；聚合健康、可访问源数量或
内容时效不达标时整轮停止。源数量和成员以本机 `config.yaml` 为准；确认长期陈旧的
feed 应由官方一手技术源替换，而不是通过降低 freshness 门槛掩盖。

## LLM 路由与缓存

Provider 顺序由配置决定，只有具备凭证且被启用的 provider 才参与。不是固定的 Gemini → OpenAI → DeepSeek 链。缓存键绑定正文、prompt、模型和评分配置版本，使用原子替换；缓存命中不会重新计费，内容或评分合同改变会自动失效。缓存结果和最终报告仍会再次经过确定性的产业边界闸门，旧的错误高分不能绕过报告准入。

当前项目策略显式设置 `gemini.enabled: false`，并从 provider 顺序中移除 Gemini；即使本机仍存在 `GEMINI_API_KEY`，也不会创建 Gemini 客户端或发送请求。新闻阶段优先使用 DeepSeek，量化模块使用同一禁用策略。

去重分两层：本地字符串/事件分组和 LLM 高分事件合并。Deep Dive 只在阈值触发且文章中能定位并读取独立一手来源时运行；仅有二手报道、模型返回未出现在原文链接中的 URL 或一手正文不可读时一律跳过。成功结果只复用 verified-primary 证据；失败尝试按 policy 版本负缓存24小时，避免同一文章在短时间内重复执行 403/Jina/一手来源探测，policy 变化或 TTL 到期后自动重试。

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
