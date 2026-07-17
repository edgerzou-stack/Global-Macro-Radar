# Industry Radar

Industry Radar 负责采集产业新闻、执行 RSS 健康闸门、去重、LLM 评分并生成 Markdown 报告。它是统一流程的第三阶段；standalone `main.py` 只生成报告，最终交付由根流程的 delivery outbox 处理。

## 数据与评分合同

每篇通过预筛的新闻由模型返回四个 0–100 整数子分：

- `tech_breakthrough`：技术突破强度
- `commercial_impact`：中长期商业影响
- `market_attention`：市场与资本关注
- `time_sensitivity`：时效与短期催化

配置权重把子分聚合成两条 0–10 composite：Innovation 与 Traffic。分值必须有限、在范围内且每个输入恰好对应一个输出；缺失、重复或结构错误会重试并最终 fail closed。

`source_confidence` 用于记录来源可信度，不会自动给内容分加分。`heuristics.yaml` 中未连接到运行路径的 fatigue 参数不应视为有效规则。当前默认回看窗口由配置决定，不能据此保证所有文章都在 24 小时内。

## RSS 可靠性

`ingest.py` 并发拉取配置中的全部 feed，并检查：

- HTTP/content-type 和 feed 解析结果
- UTC aware 发布时间与 freshness
- healthy source ratio、fresh article count
- 命名 critical source groups
- fixture 中声明的健康统计与真实文章数一致

零条新文章的可达 feed 会标为 degraded；聚合健康或关键源组不达标时整轮停止。当前真实配置包含 22 个 feed，但数量和成员以本机 `config.yaml` 为准。

## LLM 路由与缓存

Provider 顺序由配置决定，只有具备凭证且被启用的 provider 才参与。不是固定的 Gemini → OpenAI → DeepSeek 链。缓存键绑定正文、prompt、模型和评分配置版本，使用原子替换；缓存命中不会重新计费，内容或评分合同改变会自动失效。

去重分两层：本地字符串/事件分组和 LLM 高分事件合并。Deep Dive 只在阈值触发时运行；网页正文不可得时会明确降级，不能声称总能定位独立原始来源。

## 运行

推荐从仓库根目录运行完整流程：

```bash
./run_all.sh --mode live-shadow \
  --database /absolute/path/to/test-copy.db \
  --artifact-root /absolute/path/to/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-run-id \
  --delivery-mode sink
```

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
| `main.py` | 新闻阶段编排与报告生成 |
| `ingest.py` | RSS 抓取、freshness 与健康统计 |
| `score.py` | 评分 schema、prompt 与批处理 |
| `llm_router.py` | 配置驱动的 provider 路由 |
| `cache_manager.py` | 版本化增量缓存 |
| `deep_dive.py` | 高阈值事件深挖 |
| `config.example.yaml` | 可提交的配置模板 |

## License

代码与文档采用 CC BY-NC 4.0；商业使用请另行取得授权。
