# 🚀 Industry Radar (科技产业情报雷达)

[![License: CC BY-NC 4.0](https://img.shields.io/badge/License-CC_BY--NC_4.0-lightgrey.svg)](#)


An automated, dual-track intelligence gathering system that acts as your personal Silicon Valley VC analyst. It continuously monitors top-tier global tech media and hacker communities, using advanced LLMs to strictly filter out the noise and deliver only world-changing tech breakthroughs and major industry focal points straight to your inbox.

##  Features

- **Dual-Track VC Scoring System**: Articles are rigorously evaluated on two dimensions (0-10 scale):
  - **🔬 Hardcore Innovation (硬核创新)**: Measures underlying technological breakthroughs, long-term commercial value, and disruptive potential.
  - ** Traffic & Hype (流量舆情)**: Measures short-term market sentiment, consumer frenzy, and social media buzz.
- **Triple-Tier Cascade LLM Engine**: Leverages a robust fallback chain (Google Gemini -> OpenAI -> DeepSeek) to maximize stability, prevent API rate limits, and minimize costs.
- **Strict "Needle in a Haystack" Anti-Spam**: Employs context-aware temporal logic (injecting the precise Current Date) to ruthlessly identify and filter out "炒冷饭" (re-hashed old news), news roundups, and shopping deals.
- **Intelligent Deduplication**: Automatically merges identical news events reported by multiple outlets into a single, highly condensed summary while preserving the earliest publication date.
- **Deep Dive Reports**: Automatically generates a multi-thousand-word deep-dive appendix for the rarest "Supernova" events (Requires Innovation >= 9 AND Traffic >= 9), elegantly separated by massive whitespace for a clean reading experience.
- **Top-Tier Global Sources**: Pulls raw intelligence from high-signal sources including *The Information, Hacker News, MIT Technology Review, TechCrunch, 36Kr*, and more.

## 🛠️ Setup & Installation

### 1. Clone the repository
```bash
git clone https://github.com/edgerzou-stack/industry-radar.git
cd industry-radar
```

### 2. Install dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration
Copy the `.env.example` file to `.env`:
```bash
cp .env.example .env
```
Fill in your credentials in `.env`:
- `OPENAI_API_KEY`: Your OpenAI or proxy API key.
- `OPENAI_BASE_URL`: Base URL for the OpenAI API (defaults to `https://api.openai.com/v1`, change if using a proxy).
- `ICLOUD_APP_PASSWORD`: An App-Specific Password for your iCloud email (used for SMTP).

Adjust scoring thresholds, AI models (e.g. `gpt-5.4`, `gpt-4o-mini`), and target industries in `config.yaml`.

### 4. Run the Radar
```bash
python main.py
```

### 5. Automation
To run the radar automatically every day at 2:00 PM, add a cron job:
```bash
crontab -e
# Add the following line:
0 14 * * * cd /absolute/path/to/industry-radar && source venv/bin/activate && python main.py >> run.log 2>&1
```

## 📄 License
MIT


---

## License & Copyright

> **开源协议声明 (License & Copyright)**
> 本仓库包含的架构文档、设计思路及配套代码均采用 **CC BY-NC 4.0 (知识共享-署名-非商业性使用)** 协议发布。
> 允许个人学习、学术研究及开源技术交流。**严格禁止任何企业或个人将其直接或间接用于任何商业目的**（包括但不限于商业芯片研发、企业内部培训、闭源软件开发等）。如需商业使用，请与作者联系获取单独授权。
