# Global Macro Radar — Industry Radar

Global Macro Radar 的公开版本提供 Industry Radar：一个以来源证据、事件成熟度和
可审计双轨评分为核心的产业新闻研究流水线。它能够抓取配置化 RSS、执行来源健康
检查、去重、评分，并生成 Markdown 报告及只读热点证据 artifact。

> 本项目仅用于研究与工程验证，不构成投资建议。

## 公开范围

公开发行版只包含 Industry Radar 运行时和通用 provider 错误合同。量化选股策略、
交易意图、账本、生产操作、内部测试、fixture、运行报告和 agent 配置均不属于公开
发行版。公开 checkout 因此不能运行私有的端到端量化流程。

## 安装

要求 Python 3.11：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r industry-radar/requirements.txt
cp industry-radar/.env.example industry-radar/.env
cp industry-radar/config.example.yaml industry-radar/config.yaml
```

在本机 `.env` 中配置 provider 的凭证，并在本机 `config.yaml` 中只启用经过审核的
provider。示例配置默认关闭所有外部 LLM 与邮件交付；没有显式启用的 provider 不会
被调用。不要提交 `.env`、`config.yaml`、缓存或生成报告。

## 运行

```bash
cd industry-radar
python main.py
```

输出目录和 RSS/评分策略由本机 `config.yaml` 控制。历史日期不能使用当前实时 RSS
重建，应由调用方提供当日快照。

## 证据边界

- 主列表继续使用配置的 Innovation/Traffic 阈值。
- Research Watch 由确定性的来源证据、产业里程碑和成熟度决定，不是分数救援桶。
- 来源权威性不会自动提高创新分。
- 只有符合一手证据政策的事件才能写入热点证据 artifact；研究线索不能直接驱动交易。

更多说明见 [Industry Radar 文档](industry-radar/README.md)。安全问题请参阅
[SECURITY.md](SECURITY.md)。

## License

代码与文档采用 CC BY-NC 4.0；商业使用请另行取得授权。
