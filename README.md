# Global Macro Radar

[![License: CC BY-NC 4.0](https://img.shields.io/badge/License-CC_BY--NC_4.0-lightgrey.svg)](#)




> **开源协议声明 (License & Copyright)**
> 本项目采用 **CC BY-NC 4.0 (知识共享署名-非商业性使用 4.0)** 协议开源。
> 允许您自由学习、研究及个人日常投资使用。**严格禁止任何未经授权的商业化变现行为**（包括但不限于：基于此代码包装付费投研服务、SaaS产品、将其作为黑盒信号源发行金融私募产品等）。如需商业合作，请单独联系作者获取授权。

**Global Macro Radar** 是一个全自动化的“投研一体化”智能中枢系统。它通过整合**硬核科技产业新闻监控（LLM 驱动）**与**全球多资产量化选股（A股/港股/美股）**，每天为您生成一份结构化、数据驱动且极具洞察力的统一研报。

本仓库是一个 Monorepo（单体仓库），无损整合了先前的 `QF_strategy` 量化策略库和 `industry-radar` 产业雷达库。

---

## 核心特性 (Key Features)

### 1. 智能产业雷达 (Industry Radar)
基于大语言模型（支持 DeepSeek / Gemini / OpenAI）的自动新闻筛选与研报生成系统：
- **双轨评分机制 (Dual-Track Scoring)**：以 `0.1` 的高精度浮点数对新闻的“硬核创新分”和“流量舆情分”进行独立评估，严格锚定打分基准。
- **顶级信源白名单提权 (Source Whitelist)**：对来自 `Nature`, `Science`, `The Information` 等顶级信源的突破性事件自动给予分数加成与特殊标记。
- **审美疲劳降权 (Hype Fatigue Penalty)**：自动识别并压低近期反复炒作的口水战和无实质性技术进展的 PR 软文权重。
- **智能去重与溯源 (Smart Deduplication)**：基于超长上下文理解能力，精准将同一事件的多篇报道合并，并自动抓取外媒的原始出处（Primary Source）。
- **深潜研报触发 (Deep Dive Generation)**：当某事件的“创新分 + 流量分 >= 18”时，自动触发深潜逻辑，生成万字长文级别的深度产业梳理。

### 2. 全球量化策略 (Global Quant Strategy)
基于 `akshare` 和 `yfinance` 的自动化量化多因子初筛体系：
- **高股息策略 (Dividend Yield)**：自动计算 A 股历史派息率与最新市值，挖掘稳健收息标的。
- **连续高增长策略 (Continuous Growth)**：扫描 A/港/美 股市场，筛选利润与营收连续多个季度保持高增速（如 CAGR > 20%）的成长股，并严格规避伪成长。
- **市场风口与 ETF 动量 (Hot Spot & Momentum)**：追踪全球核心宽基及行业 ETF，自动提取近期表现最强势的资金抱团板块。
- **自动归因分析**：对每一只入选标的，自动调用大模型生成“入选逻辑 (Reason)”，用一句话点透其财务与技术面亮点。

---

## 目录结构 (Architecture)

```text
Global-Macro-Radar/
├── quant-strategy/          # 量化初筛与策略核心代码 (原 QF_strategy)
│   ├── scripts/             # 每日量化跑批脚本、回测框架、Pnl绘图
│   └── ...
├── industry-radar/          # 产业新闻大模型分析核心代码
│   ├── config.example.yaml  # 评分锚点、白名单及 RSS 订阅源配置
│   ├── main.py              # 雷达主干逻辑
│   ├── score.py             # 双轨打分与去重核心逻辑
│   └── deep_dive.py         # 深度研报生成逻辑
├── run_all.sh               # 全局一键启动总控脚本
└── README.md                # 本说明文档
```

---

## 快速启动 (Quick Start)

### 1. 环境准备
```bash
# 推荐使用 Python 3.9 及以上版本
python3 -m venv venv
source venv/bin/activate
pip install -r quant-strategy/requirements.txt # (如果有的话，或者手动安装 akshare, yfinance, openai, google-genai 等)
```

### 2. 配置密钥与邮箱
在 `industry-radar` 目录下创建 `.env` 文件，填入您的大模型 API Keys：
```env
DEEPSEEK_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here
```

然后，复制 `config.example.yaml` 为 `config.yaml`，并在其中配置您的投研体系基准以及收发件邮箱：
```bash
cp industry-radar/config.example.yaml industry-radar/config.yaml
```

### 3. 一键执行每日跑批
只需在根目录下运行外壳脚本，系统即可自动并发执行量化初筛、RSS 信息流拉取、大模型打分，并将最终的精美 HTML 综合研报发送至您的邮箱。

```bash
bash run_all.sh
```

---

## 自动化部署建议
建议将本系统与 macOS 的 `crontab`、`Keyboard Maestro` 或云服务器的定时任务结合，设定在每个交易日的早晨（如 8:00 AM）自动运行，打造您的专属 AI 投研早班车。

```bash
# 示例：每天早晨 8:00 自动执行
0 8 * * 1-5 cd /path/to/Global-Macro-Radar && bash run_all.sh >> run.log 2>&1
```
