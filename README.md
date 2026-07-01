# Global Macro Radar

这个仓库是我们整合后的“宏观量化体系”单体仓库 (Monorepo)。它无损合并了先前的两个子项目：

## 目录结构
- `quant-strategy/`: 包含了原先的 QF_strategy 量化选股、因子测试等所有代码。
- `industry-radar/`: 包含了原先基于 LLM 和 RSS 的前沿科技与产业新闻雷达系统。

## 一键启动
您可以直接在根目录运行自动化跑批（这等同于之前用 `daily_runner.py` 触发的全链路跑批）：

```bash
bash run_all.sh
```

## 注意事项
该仓库已经完整保留了合并前两边所有的 Git Commit 历史。
如果您在本地开发调试，请基于本仓库修改代码，然后推送到新的远程 GitHub 仓库中（例如命名为 `Global-Macro-Radar`）。
旧的单独仓库可以安全地去 GitHub 网页端点击 Archive (归档) 啦。
