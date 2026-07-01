import yaml
import os
from datetime import datetime
from ingest import fetch_rss_feeds
from score import score_article
from dotenv import load_dotenv
from cache_manager import load_cache, save_cache
import smtplib
from email.message import EmailMessage
import markdown

def load_config(config_path="config.yaml"):
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def generate_markdown_report(scored_articles, config, output_dir="reports"):
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    report_path = os.path.join(output_dir, f"industry_report_{date_str}.md")
    
    min_score = config.get("output", {}).get("min_score_to_keep", 8)
    
    high_scoring = []
    for a in scored_articles:
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
            # Check deep dive cache
            cache_key = a.get('link')
            dd = None
            if 'deep_dive' in a:
                dd = a['deep_dive']
            else:
                from deep_dive import generate_deep_dive_report
                dd = generate_deep_dive_report(a, config)
                
            if dd:
                a['deep_dive'] = dd
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
    load_dotenv()
    config = load_config()
    
    print("Fetching articles from RSS feeds...", flush=True)
    hours_back = config.get("output", {}).get("hours_back", 48)
    articles = fetch_rss_feeds(config.get("rss_feeds", []), hours_back=hours_back)
    print(f"Fetched {len(articles)} articles.", flush=True)
    
    if not os.getenv("OPENAI_API_KEY"):
        print("WARNING: OPENAI_API_KEY is not set. Using mock dual-track data.", flush=True)
        from bs4 import BeautifulSoup
        for i, article in enumerate(articles):
            summary = article.get('summary', '')
            clean_summary = BeautifulSoup(summary, "html.parser").get_text()[:200] + "..." if summary else "无摘要"
            
            # Mock distribution
            if i % 3 == 0:
                i_score, t_score = 9, 9
            elif i % 3 == 1:
                i_score, t_score = 9, 5
            else:
                i_score, t_score = 4, 9
                
            article['score_data'] = {
                "innovation_score": i_score, 
                "traffic_score": t_score,
                "justification": "未配置 API Key，展示双轨制模拟打分。", 
                "is_relevant": True, 
                "translated_title": article['title'],
                "translated_summary": clean_summary
            }
        scored_articles = articles
    else:
        scored_articles = []
        
        # Load incremental cache
        cache_data = load_cache()
        
        print(f"Loaded {len(cache_data)} articles from incremental cache.", flush=True)
        print("Scoring articles using Dual-Track LLM...", flush=True)
        
        import concurrent.futures
        from score import pre_filter_articles_batch, score_articles_batch
        
        cache_updates = 0
        new_articles = []
        
        for idx, article in enumerate(articles):
            article['id'] = idx
            link = article.get('link')
            
            if link in cache_data and 'score_data' in cache_data[link]:
                sd = cache_data[link]['score_data']
                print(f"[{idx+1}/{len(articles)}] (Cached) [I:{sd.get('innovation_score',0)} T:{sd.get('traffic_score',0)}] {article['title'][:30]}...", flush=True)
                article['score_data'] = sd
                if 'deep_dive' in cache_data[link]:
                    article['deep_dive'] = cache_data[link]['deep_dive']
                scored_articles.append(article)
            else:
                new_articles.append(article)
                
        print(f"Found {len(new_articles)} new articles to process.", flush=True)
        
        if new_articles:
            print("--- Phase 1: Pre-filtering (Batches of 20) ---", flush=True)
            passed_pre_filter = []
            
            def process_pre_filter_batch(batch):
                try:
                    res = pre_filter_articles_batch(batch, config)
                    return res.get("results", [])
                except Exception as e:
                    print(f"Phase 1 Error: {e}", flush=True)
                    return []
            
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
                                
                                link = matched.get('link')
                                if link not in cache_data: cache_data[link] = {}
                                cache_data[link]['score_data'] = sd
                                cache_updates += 1
                                
            print(f"Phase 1 complete. {len(passed_pre_filter)} articles survived.", flush=True)
            
            if passed_pre_filter:
                print("--- Phase 2: Detailed Scoring (Batches of 5) ---", flush=True)
                
                def process_scoring_batch(batch):
                    try:
                        res = score_articles_batch(batch, config)
                        return res.get("results", [])
                    except Exception as e:
                        print(f"Phase 2 Error: {e}", flush=True)
                        return []
                        
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
                                    "is_relevant": r.get("is_relevant", True),
                                    "innovation_score": r.get("innovation_score", 0),
                                    "traffic_score": r.get("traffic_score", 0),
                                    "justification": r.get("justification", ""),
                                    "translated_title": r.get("translated_title", matched['title']),
                                    "translated_summary": r.get("translated_summary", "")
                                }
                                scored_articles.append(matched)
                                print(f"  -> Scored [{matched['id']}] [I:{matched['score_data']['innovation_score']} T:{matched['score_data']['traffic_score']}] {matched['title'][:30]}", flush=True)
                                
                                link = matched.get('link')
                                if link not in cache_data: cache_data[link] = {}
                                cache_data[link]['score_data'] = matched['score_data']
                                cache_updates += 1
                                
            if cache_updates > 0:
                save_cache(cache_data)
    
    
    # We must also save cache if any deep dives were generated during the report phase
    # Update cache with newly generated deep_dives
    new_dd = False
    for a in scored_articles:
        link = a.get('link')
        if 'deep_dive' in a and link in cache_data:
            if 'deep_dive' not in cache_data[link]:
                cache_data[link]['deep_dive'] = a['deep_dive']
                new_dd = True
    
    if new_dd:
        save_cache(cache_data)
        
    report_path = generate_markdown_report(scored_articles, config)
    print(f"\nReport generated successfully: {report_path}", flush=True)
    
    # 5. Send Email
    # Email is now sent by the unified daily runner
    # send_email(report_path, config)

if __name__ == "__main__":
    main()
