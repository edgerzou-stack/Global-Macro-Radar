import yaml
import os
import json
import tempfile
from datetime import datetime, timedelta
from ingest import fetch_rss_feeds
from score import score_article
from dotenv import load_dotenv
from cache_manager import (
    build_cache_key,
    get_cached_score,
    load_cache,
    make_cache_entry,
    save_cache,
)
import smtplib
from email.message import EmailMessage
import markdown


def scoring_cache_config(config):
    """Return only configuration that can change score semantics."""
    return {
        "industries": config.get("industries", []),
        "importance_criteria": config.get("importance_criteria", ""),
        "scoring_weights": config.get("scoring_weights", {}),
        "trusted_sources": config.get("trusted_sources", []),
        "language": config.get("output", {}).get("language", "Chinese"),
    }


def configured_scoring_identities(config):
    provider_keys = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    defaults = {
        "gemini": "gemini-2.5-flash",
        "openai": "gpt-4.1-mini",
        "deepseek": config.get("output", {}).get("model", "deepseek-chat"),
    }
    llm_config = config.get("llm", {})
    provider_config = llm_config.get("providers", {})
    identities = []
    for provider in llm_config.get("order", ["gemini", "openai", "deepseek"]):
        settings = provider_config.get(provider, {})
        enabled = settings.get("enabled", True)
        if enabled and os.getenv(provider_keys.get(provider, "")):
            identities.append((provider, settings.get("model", defaults.get(provider, "unknown"))))
    return identities


def find_cached_article(cache_data, article, config):
    from score import SCORING_PROMPT_VERSION

    for provider, model in configured_scoring_identities(config):
        cache_key = build_cache_key(
            article,
            scoring_cache_config(config),
            SCORING_PROMPT_VERSION,
            provider,
            model,
        )
        score_data = get_cached_score(cache_data.get(cache_key), cache_key)
        if score_data is not None:
            return score_data, cache_key
    return None, None


def store_article_score(cache_data, article, score_data, config, **extra):
    from score import SCORING_PROMPT_VERSION

    identities = configured_scoring_identities(config)
    provider = score_data.get("llm_provider") if isinstance(score_data, dict) else None
    model = score_data.get("llm_model") if isinstance(score_data, dict) else None
    if not provider or not model:
        if not identities:
            raise RuntimeError("Cannot cache a score without a configured LLM identity")
        provider, model = identities[0]
    cache_key = build_cache_key(
        article,
        scoring_cache_config(config),
        SCORING_PROMPT_VERSION,
        provider,
        model,
    )
    cache_data[cache_key] = make_cache_entry(
        cache_key,
        score_data,
        raw_title=article.get("title", ""),
        raw_summary=article.get("summary", ""),
        provider=provider,
        model=model,
        **extra,
    )
    article["_cache_key"] = cache_key
    return cache_key


def run_validated_batch(batch, config, scorer, attempts=2):
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            payload = scorer(batch, config)
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or len(results) != len(batch):
                raise ValueError(
                    f"batch result count {len(results) if isinstance(results, list) else 'invalid'} "
                    f"does not match input count {len(batch)}"
                )
            return results
        except Exception as error:
            last_error = error
            print(
                f"Validated batch attempt {attempt}/{attempts} failed: {error}",
                flush=True,
            )
    raise RuntimeError(f"Validated batch failed after {attempts} attempts") from last_error


def save_json_atomic(path, payload):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            temporary_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def validate_rss_health(health, max_failure_ratio=0.5):
    if not health:
        raise RuntimeError("RSS health check failed: no sources were configured")
    failed = [item for item in health if item.get("status") == "failed"]
    failure_ratio = len(failed) / len(health)
    if failure_ratio > max_failure_ratio:
        raise RuntimeError(
            f"RSS health check failed: {len(failed)}/{len(health)} sources unavailable"
        )

def load_config(config_path="config.yaml"):
    # P4.1: Graceful fallback for missing config.yaml
    if not os.path.exists(config_path):
        example_path = "config.example.yaml"
        if os.path.exists(example_path):
            import shutil
            shutil.copy2(example_path, config_path)
            print(f"Warning: {config_path} not found. Auto-created from {example_path}.")
        else:
            raise FileNotFoundError(f"Missing both {config_path} and {example_path}!")
            
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def generate_markdown_report(scored_articles, config, output_dir="reports"):
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    report_path = os.path.join(output_dir, f"industry_report_{date_str}.md")
    
    min_score = config.get("output", {}).get("min_score_to_keep", 6)
    lookback_days = config.get("output", {}).get("report_days_lookback", 2)
    
    cutoff_date_str = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    
    high_scoring = []
    for a in scored_articles:
        pub_date = a.get('published_at', '')[:10]
        if pub_date and pub_date < cutoff_date_str:
            continue
            
        sd = a.get('score_data', {})
        if not sd.get('is_relevant'):
            continue
            
        i_score = sd.get('innovation_score', 0)
        t_score = sd.get('traffic_score', 0)
        
        if i_score >= min_score or t_score >= min_score:
            high_scoring.append(a)
            
    if high_scoring:
        print(f"Deduplicating {len(high_scoring)} high-scoring articles...", flush=True)
        from score import deduplicate_articles
        high_scoring = deduplicate_articles(high_scoring, config)
        print(f"After deduplication: {len(high_scoring)} articles remaining.", flush=True)

    supernova = []
    hardcore = []
    hype = []
    deep_dives = []
    
    for a in high_scoring:
        sd = a.get('score_data', {})
        i_score = sd.get('innovation_score', 0)
        t_score = sd.get('traffic_score', 0)
        
        if (i_score + t_score) >= 18:
            if 'deep_dive' in a:
                deep_dives.append(a)
        
        if i_score >= min_score and t_score >= min_score:
            supernova.append(a)
        elif i_score >= min_score:
            hardcore.append(a)
        elif t_score >= min_score:
            hype.append(a)
            
    # Sort descending
    supernova.sort(key=lambda x: x['score_data'].get('innovation_score', 0) + x['score_data'].get('traffic_score', 0), reverse=True)
    hardcore.sort(key=lambda x: x['score_data'].get('innovation_score', 0), reverse=True)
    hype.sort(key=lambda x: x['score_data'].get('traffic_score', 0), reverse=True)
    
    # Limit to top 10 to prevent information overload
    supernova = supernova[:10]
    hardcore = hardcore[:10]
    hype = hype[:10]
    
    def write_article_block(file, article):
        sd = article['score_data']
        title = sd.get('translated_title', article['title'])
        file.write(f"### [硬核:{float(sd.get('innovation_score', 0)):.1f} | 流量:{float(sd.get('traffic_score', 0)):.1f}] {title}\n")
        if title != article['title'] and sd.get('translated_title'):
            file.write(f"*{article['title']}*\n\n")
        file.write(f"**来源**: {article['source']} | **日期**: {article['published_at'][:10]}\n\n")
        if sd.get('translated_summary'):
            file.write(f"**摘要**: {sd['translated_summary']}\n\n")
        file.write(f"> **点评**: {sd['justification']}\n\n")
        file.write(f"[阅读原文]({article['link']})\n\n---\n")

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"# 科技产业情报雷达 - Daily Report ({date_str})\n\n")
        
        if not (supernova or hardcore or hype):
            f.write("今天没有任何新闻达到你设置的超高标准 (全板块 8 分以下)。\n\n_真正的结构性大机会不会每天都有，享受这片刻的宁静吧。_\n")
            return report_path
            
        if deep_dives:
            f.write("## 🤿 深度研报 (Deep Dive)\n_系统已自动溯源第一手官方资料，由 AI 生成顶尖研报。_\n\n")
            for idx, a in enumerate(deep_dives):
                sd = a['score_data']
                title = sd.get('translated_title', a['title'])
                dd = a['deep_dive']
                f.write(f"### [硬核:{float(sd.get('innovation_score', 0)):.1f} | 流量:{float(sd.get('traffic_score', 0)):.1f}] {title}\n")
                if title != a['title'] and sd.get('translated_title'):
                    f.write(f"*{a['title']}*\n\n")
                f.write(f"**来源**: {a['source']} | **日期**: {a['published_at'][:10]}\n\n")
                if sd.get('translated_summary'):
                    f.write(f"**摘要**: {sd['translated_summary']}\n\n")
                f.write(f"> **点评**: {sd['justification']}\n\n")
                
                f.write(f"[🌐 溯源官方原文]({dd['primary_url']})\n\n")
                f.write(f"<details markdown=\"1\" style=\"margin-top: 15px; margin-bottom: 20px;\">\n")
                f.write(f"  <summary style=\"cursor: pointer; color: #3b82f6; font-weight: bold; font-size: 16px;\">👇 点击展开/收起 AI 深度研报全文</summary>\n")
                f.write(f"  <div markdown=\"1\" style=\"margin-top: 15px; padding: 20px; background: #f8fafc; border-radius: 8px; border-left: 4px solid #3b82f6; font-size: 14px; line-height: 1.6;\">\n\n")
                f.write(f"**{title} - 深度研报**\n\n")
                f.write(f"{dd['report_content']}\n\n")
                f.write(f"  </div>\n")
                f.write(f"</details>\n\n---\n")
                
        if supernova:
            f.write("## 🌟 顶流硬核 (Supernova)\n_兼具颠覆性技术价值与爆炸性市场流量的里程碑事件！_\n\n")
            for a in supernova:
                write_article_block(f, a)
                
        if hardcore:
            f.write("## 🔬 科技硬核创新 (Hardcore Innovation)\n_改变世界的底层力量。也许目前大众尚未狂热，但具有长远商业价值。_\n\n")
            for a in hardcore:
                write_article_block(f, a)
                
        if hype:
            f.write("## 📈 产业焦点与流量狂欢 (Traffic & Hype)\n_当前资本和大众的注意力焦点。可能是风口，也可能是抓马泡沫。_\n\n")
            for a in hype:
                write_article_block(f, a)
                
        # Appendix removed as Deep Dive is now inline
                
    return report_path

def send_email(report_path, config):
    delivery_cfg = config.get("delivery", {})
    if not delivery_cfg.get("enabled"):
        return
        
    sender = delivery_cfg.get("sender_email")
    recipient = delivery_cfg.get("recipient_email")
    server = delivery_cfg.get("smtp_server", "smtp.mail.me.com")
    port = delivery_cfg.get("smtp_port", 587)
    
    password = os.getenv("ICLOUD_APP_PASSWORD")
    if not sender or not recipient or not password:
        print("Email configuration or ICLOUD_APP_PASSWORD missing. Skipping email delivery.")
        return
        
    print(f"Sending report via email to {recipient}...")
    
    with open(report_path, 'r', encoding='utf-8') as f:
        md_content = f.read()

    # Convert markdown to HTML
    html_body = markdown.markdown(md_content, extensions=['tables', 'md_in_html'])
    
    # CSS styling for a premium newsletter look
    html_content = f"""
    <html>
    <head>
    <style>
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            line-height: 1.6;
            color: #1f2937;
            background-color: #f3f4f6;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            max-width: 650px;
            margin: 0 auto;
            background: #ffffff;
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.05);
        }}
        h1 {{
            color: #111827;
            font-size: 26px;
            border-bottom: 3px solid #3b82f6;
            padding-bottom: 12px;
            margin-bottom: 25px;
            font-weight: 800;
        }}
        h2 {{
            color: #2563eb;
            font-size: 22px;
            margin-top: 35px;
            border-bottom: 1px solid #e5e7eb;
            padding-bottom: 10px;
            font-weight: 700;
        }}
        h3 {{
            color: #111827;
            font-size: 18px;
            margin-top: 25px;
            line-height: 1.4;
        }}
        p {{
            margin-bottom: 15px;
            color: #4b5563;
            font-size: 15px;
        }}
        a {{
            color: #3b82f6;
            text-decoration: none;
            font-weight: 500;
        }}
        a:hover {{
            text-decoration: underline;
        }}
        em {{
            color: #6b7280;
            font-style: italic;
            font-size: 14px;
        }}
        hr {{
            border: 0;
            height: 1px;
            background: #e5e7eb;
            margin: 25px 0;
        }}
        strong {{
            color: #111827;
            font-weight: 600;
        }}
    </style>
    </head>
    <body>
        <div class="container">
            {html_body}
        </div>
    </body>
    </html>
    """

    date_str = datetime.now().strftime("%Y-%m-%d")
    msg = EmailMessage()
    msg['Subject'] = f"🚀 科技产业情报雷达 - {date_str}"
    msg['From'] = sender
    msg['To'] = recipient
    msg['Bcc'] = sender # 密送给自己一份，作为“已发送”的备份记录
    
    msg.set_content(md_content) # Plain text fallback
    msg.add_alternative(html_content, subtype='html') # Rich HTML version
    
    try:
        with smtplib.SMTP(server, port) as smtp:
            smtp.starttls()
            smtp.login(sender, password)
            smtp.send_message(msg)
        print("Email sent successfully!")
    except Exception as e:
        print(f"Failed to send email: {e}")

def main():
    print("Starting Dual-Track Industry Intelligence Gatherer...", flush=True)
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
    config = load_config()
    
    print("Fetching articles from RSS feeds...", flush=True)
    hours_back = config.get("output", {}).get("hours_back", 48)
    articles, rss_health = fetch_rss_feeds(
        config.get("rss_feeds", []), hours_back=hours_back, return_health=True
    )
    save_json_atomic(os.path.join("reports", "rss_health.json"), rss_health)
    validate_rss_health(
        rss_health,
        max_failure_ratio=float(
            config.get("output", {}).get("rss_max_failure_ratio", 0.5)
        ),
    )
    print(f"Fetched {len(articles)} articles.", flush=True)
    
    # P3.21: 评分前增加基于 URL/Title 的快速预去重
    unique_articles = []
    seen_urls = set()
    seen_titles = set()
    for a in articles:
        if a['link'] not in seen_urls and a['title'].lower() not in seen_titles:
            unique_articles.append(a)
            seen_urls.add(a['link'])
            seen_titles.add(a['title'].lower())
    
    if len(unique_articles) < len(articles):
        print(f"Pre-scoring deduplication removed {len(articles) - len(unique_articles)} duplicates. {len(unique_articles)} articles remaining.", flush=True)
    articles = unique_articles
    
    # Load incremental cache
    cache_data = load_cache()
    cache_updates = 0
    
    if not os.getenv("GEMINI_API_KEY") and not os.getenv("DEEPSEEK_API_KEY") and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("CRITICAL ERROR: No LLM API Key is configured. Please configure GEMINI_API_KEY, DEEPSEEK_API_KEY, or OPENAI_API_KEY in .env.")
    else:
        scored_articles = []
        print(f"Loaded {len(cache_data)} articles from incremental cache.", flush=True)
        print("Scoring articles using Dual-Track LLM...", flush=True)
        
        import concurrent.futures
        from score import pre_filter_articles_batch, score_articles_batch
        
        cache_updates = 0
        new_articles = []
        
        for idx, article in enumerate(articles):
            article['id'] = idx

            sd, cache_key = find_cached_article(cache_data, article, config)
            if sd is not None:
                try:
                    i_score = float(sd.get('innovation_score', 0))
                    t_score = float(sd.get('traffic_score', 0))
                except:
                    i_score = t_score = 0
                print(f"[{idx+1}/{len(articles)}] (Cached) [I:{i_score:.1f} T:{t_score:.1f}] {article['title'][:30]}...", flush=True)
                article['score_data'] = sd
                article['_cache_key'] = cache_key
                if 'deep_dive' in cache_data[cache_key]:
                    article['deep_dive'] = cache_data[cache_key]['deep_dive']
                scored_articles.append(article)
            else:
                new_articles.append(article)
                
        print(f"Found {len(new_articles)} new articles to process.", flush=True)
        
        if new_articles:
            # P3.21: 评分前去重 (Pre-deduplication before hitting LLM APIs)
            import difflib
            print("--- Phase 0: Local String Deduplication ---", flush=True)
            local_dedup_groups = []
            for a in new_articles:
                long_text = a.get('content', a.get('summary', ''))
                if long_text: long_text = long_text[:800]
                text_to_match = (a.get('title', '') + " " + long_text).lower()
                
                found_group = False
                for group in local_dedup_groups:
                    rep = group[0]
                    rep_text = rep.get('content', rep.get('summary', ''))
                    if rep_text: rep_text = rep_text[:800]
                    rep_match = (rep.get('title', '') + " " + rep_text).lower()
                    
                    if difflib.SequenceMatcher(None, text_to_match, rep_match).ratio() > 0.85:
                        group.append(a)
                        found_group = True
                        break
                if not found_group:
                    local_dedup_groups.append([a])
                    
            deduped_new_articles = [g[0] for g in local_dedup_groups]
            print(f"Reduced from {len(new_articles)} to {len(deduped_new_articles)} unique events.", flush=True)
            new_articles = deduped_new_articles
            
            print("--- Phase 1: Pre-filtering (Batches of 20) ---", flush=True)
            passed_pre_filter = []
            
            def process_pre_filter_batch(batch):
                return run_validated_batch(batch, config, pre_filter_articles_batch)
            
            batches_p1 = [new_articles[i:i + 20] for i in range(0, len(new_articles), 20)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures_p1 = [executor.submit(process_pre_filter_batch, b) for b in batches_p1]
                
                for future in concurrent.futures.as_completed(futures_p1):
                    results = future.result()
                    for r in results:
                        article_id = r.get("id")
                        is_rel = r.get("is_relevant", False)
                        
                        # Find the article
                        matched = next((a for a in new_articles if a['id'] == article_id), None)
                        if matched:
                            if is_rel:
                                passed_pre_filter.append(matched)
                            else:
                                # Mark as irrelevant and cache immediately
                                sd = {
                                    "is_relevant": False,
                                    "innovation_score": 0, "traffic_score": 0,
                                    "justification": "Filtered out in Phase 1 (Pre-filter)",
                                    "translated_title": matched['title'],
                                    "translated_summary": ""
                                }
                                matched['score_data'] = sd
                                scored_articles.append(matched)
                                
                                store_article_score(cache_data, matched, sd, config)
                                cache_updates += 1
                                
            print(f"Phase 1 complete. {len(passed_pre_filter)} articles survived.", flush=True)
            
            if passed_pre_filter:
                print("--- Phase 2: Detailed Scoring (Batches of 5) ---", flush=True)
                
                def process_scoring_batch(batch):
                    return run_validated_batch(batch, config, score_articles_batch)
                        
                batches_p2 = [passed_pre_filter[i:i + 5] for i in range(0, len(passed_pre_filter), 5)]
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    futures_p2 = [executor.submit(process_scoring_batch, b) for b in batches_p2]
                    
                    for future in concurrent.futures.as_completed(futures_p2):
                        results = future.result()
                        for r in results:
                            article_id = r.get("id")
                            # Find the article
                            matched = next((a for a in passed_pre_filter if a['id'] == article_id), None)
                            if matched:
                                matched['score_data'] = {
                                    key: value for key, value in r.items() if key != "id"
                                }
                                scored_articles.append(matched)
                                try:
                                    i_s = float(matched['score_data']['innovation_score'])
                                    t_s = float(matched['score_data']['traffic_score'])
                                except:
                                    i_s = t_s = 0
                                print(f"  -> Scored [{matched['id']}] [I:{i_s:.1f} T:{t_s:.1f}] {matched['title'][:30]}", flush=True)
                                
                                store_article_score(
                                    cache_data, matched, matched['score_data'], config
                                )
                                cache_updates += 1
                                
            if cache_updates > 0:
                save_cache(cache_data)
    
    
    # P1.9: 将 Deep Dive 的生成逻辑移出 Markdown 渲染循环，放到主流水线并行化阶段
    min_score = config.get("output", {}).get("min_score_to_keep", 6)
    high_scoring_for_dd = [a for a in scored_articles if a.get('score_data', {}).get('innovation_score', 0) + a.get('score_data', {}).get('traffic_score', 0) >= 18]
    
    new_dd = False
    if high_scoring_for_dd:
        import concurrent.futures
        from deep_dive import generate_deep_dive_report
        
        print(f"Checking Deep Dive for {len(high_scoring_for_dd)} highly rated articles (concurrently)...", flush=True)
        
        def process_dd(a):
            cache_key = a.get('_cache_key')
            if 'deep_dive' not in a and (
                not cache_key
                or cache_key not in cache_data
                or 'deep_dive' not in cache_data[cache_key]
            ):
                print(f"Generating Deep Dive for {a['title'][:30]}...", flush=True)
                dd = generate_deep_dive_report(a, config)
                return a, cache_key, dd
            elif cache_key in cache_data and 'deep_dive' in cache_data[cache_key]:
                a['deep_dive'] = cache_data[cache_key]['deep_dive']
            return None, None, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_dd, a) for a in high_scoring_for_dd]
            for future in concurrent.futures.as_completed(futures):
                try:
                    a, cache_key, dd = future.result()
                    if dd:
                        a['deep_dive'] = dd
                        if not cache_key:
                            cache_key = store_article_score(
                                cache_data, a, a['score_data'], config
                            )
                        cache_data[cache_key]['deep_dive'] = dd
                        new_dd = True
                except Exception as e:
                    print(f"Error in deep dive worker: {e}", flush=True)

    if cache_updates > 0 or new_dd:
        save_cache(cache_data)
        
    report_path = generate_markdown_report(scored_articles, config)
    print(f"\nReport generated successfully: {report_path}", flush=True)
    
    # 5. Send Email
    # Email is now sent by the unified daily runner
    # send_email(report_path, config)

if __name__ == "__main__":
    main()
