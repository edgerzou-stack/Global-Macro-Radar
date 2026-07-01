#  Industry Radar (科技产业情报雷达)

[![License: CC BY-NC 4.0](https://img.shields.io/badge/License-CC_BY--NC_4.0-lightgrey.svg)](#)


An automated, dual-track intelligence gathering system that acts as your personal Silicon Valley VC analyst. It continuously monitors top-tier global tech media and hacker communities, using advanced LLMs to strictly filter out the noise and deliver only world-changing tech breakthroughs and major industry focal points straight to your inbox.

## Features

- **Dual-Track VC Scoring System**: Articles are rigorously evaluated on two dimensions (0-10 scale):
  - ** Hardcore Innovation (硬核创新)**: Measures underlying technological breakthroughs, long-term commercial value, and disruptive potential.
  - ** Traffic & Hype (流量舆情)**: Measures short-term market sentiment, consumer frenzy, and social media buzz.
- **Triple-Tier Cascade LLM Engine**: Leverages a robust fallback chain (Google Gemini -> OpenAI -> DeepSeek) to maximize stability, prevent API rate limits, and minimize costs.
- **Strict "Needle in a Haystack" Anti-Spam**: Employs context-aware temporal logic (injecting the precise Current Date) to ruthlessly identify and filter out "炒冷饭" (re-hashed old news), news roundups, and shopping deals.
- **Intelligent Deduplication**: Automatically merges identical news events reported by multiple outlets into a single, highly condensed summary while preserving the earliest publication date.
- **Deep Dive Reports**: Automatically generates a multi-thousand-word deep-dive appendix for the rarest "Supernova" events (Requires Innovation Score + Traffic Score >= 18), elegantly separated by massive whitespace for a clean reading experience.
- **Top-Tier Global Sources**: Pulls raw intelligence from high-signal sources including *The Information, Hacker News, MIT Technology Review, TechCrunch, 36Kr*, and more.

## Deployment & Execution

> **Monorepo Architecture Notice**
> This module is fully integrated into the **Global Macro Radar** monorepo. 
> For complete installation, API key configuration, and unified Crontab deployment instructions, please refer to the [Root README.md](../README.md).
> 
> The standalone execution scripts (like `main.py` and `score.py`) are now coordinated by the top-level `run_all.sh`.

---


## Content Index

| Item | Type | Description |
|---|---|---|
| `cache_manager.py` | **File** | Python Script / Logic |
| `config.example.yaml` | **File** | YAML Configuration |
| `deep_dive.py` | **File** | Python Script / Logic |
| `heuristics.yaml` | **File** | YAML Configuration |
| `ingest.py` | **File** | Python Script / Logic |
| `list_models.py` | **File** | Python Script / Logic |
| `llm_router.py` | **File** | Python Script / Logic |
| `main.py` | **File** | Python Script / Logic |
| `requirements.txt` | **File** | Configuration or Requirements |
| `score.py` | **File** | Python Script / Logic |

---

## License & Copyright
> **开源协议声明 (License & Copyright)**
> 本仓库包含的架构文档、设计思路及配套代码均采用 **CC BY-NC 4.0 (知识共享-署名-非商业性使用)** 协议发布。
> 允许个人学习、学术研究及开源技术交流。**严格禁止任何企业或个人将其直接或间接用于任何商业目的**（包括但不限于商业芯片研发、企业内部培训、闭源软件开发等）。如需商业使用，请与作者联系获取单独授权。
