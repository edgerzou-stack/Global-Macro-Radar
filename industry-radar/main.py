import yaml
import os
import json
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from ingest import fetch_rss_feeds, load_rss_fixture
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
from run_date import logical_date_text, logical_today


def _aware_utc_timestamp(value, field_name):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid ISO timestamp") from exc
    else:
        raise ValueError(f"{field_name} must be a timezone-aware timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def rss_reference_time_utc():
    """Return one stable reference instant for a live RSS collection stage."""
    configured = os.environ.get("MOCK_NOW_UTC")
    if configured:
        return _aware_utc_timestamp(configured, "MOCK_NOW_UTC")
    return datetime.now(timezone.utc)


def validate_rss_fixture_effective_date(articles, health, effective_date):
    """Reject fixture content that was not available by the logical run date."""
    if not isinstance(effective_date, date):
        raise TypeError("effective_date must be a date")
    cutoff = datetime.combine(
        effective_date + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    for index, article in enumerate(articles):
        published_at = _aware_utc_timestamp(
            article.get("published_at"), f"articles[{index}].published_at"
        )
        if published_at >= cutoff:
            raise ValueError(
                "RSS fixture contains post-effective-date article: "
                f"{article.get('title', '<untitled>')} at {published_at.isoformat()}"
            )
    for index, item in enumerate(health):
        newest = item.get("newest_published_at")
        if newest is None:
            continue
        newest_at = _aware_utc_timestamp(
            newest, f"health[{index}].newest_published_at"
        )
        if newest_at >= cutoff:
            raise ValueError(
                "RSS fixture health contains post-effective-date content: "
                f"{item.get('url', '<unknown>')} at {newest_at.isoformat()}"
            )


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
        "deepseek": config.get("output", {}).get("model", "deepseek-v4-flash"),
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


def validate_scoring_configuration(config):
    identities = configured_scoring_identities(config)
    if not identities:
        raise ValueError(
            "CRITICAL ERROR: No enabled LLM provider has a configured API key. "
            "Check llm.order, llm.providers.*.enabled, and the corresponding "
            "GEMINI_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY."
        )
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


def validate_rss_health(
    health,
    max_failure_ratio=0.5,
    *,
    min_healthy_ratio=0.0,
    min_fresh_sources=1,
    min_total_fresh_entries=1,
    min_configured_sources=1,
    article_count=None,
    critical_source_groups=None,
    reference_time=None,
):
    """Validate that RSS coverage is usable, not merely reachable.

    A source can be reachable yet stale or unable to produce a single recent
    entry.  Production therefore gates on healthy sources and fresh content in
    addition to the hard-failure ratio.
    """
    if not health:
        raise RuntimeError("RSS health check failed: no sources were configured")

    ratio_settings = {
        "max_failure_ratio": max_failure_ratio,
        "min_healthy_ratio": min_healthy_ratio,
    }
    for name, value in ratio_settings.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        if not 0 <= float(value) <= 1:
            raise ValueError(f"{name} must be between 0 and 1")

    minimum_settings = {
        "min_fresh_sources": min_fresh_sources,
        "min_total_fresh_entries": min_total_fresh_entries,
        "min_configured_sources": min_configured_sources,
    }
    for name, value in minimum_settings.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    failed = [item for item in health if item.get("status") == "failed"]
    healthy = [item for item in health if item.get("status") == "healthy"]
    fresh = [
        item
        for item in health
        if item.get("status") != "failed"
        and int(item.get("fresh_entries") or 0) > 0
    ]
    total_fresh_entries = sum(int(item.get("fresh_entries") or 0) for item in health)
    failure_ratio = len(failed) / len(health)
    healthy_ratio = len(healthy) / len(health)
    reasons = []
    critical_group_summaries = []
    reference_time = _aware_utc_timestamp(
        reference_time or datetime.now(timezone.utc), "reference_time"
    )

    if len(health) < min_configured_sources:
        reasons.append(
            f"configured sources {len(health)} below minimum {min_configured_sources}"
        )
    if failure_ratio > max_failure_ratio:
        reasons.append(
            f"failed sources {len(failed)}/{len(health)} exceed "
            f"{max_failure_ratio:.0%}"
        )
    if healthy_ratio < min_healthy_ratio:
        reasons.append(
            f"healthy sources {len(healthy)}/{len(health)} below "
            f"{min_healthy_ratio:.0%}"
        )
    if len(fresh) < min_fresh_sources:
        reasons.append(
            f"fresh sources {len(fresh)} below minimum {min_fresh_sources}"
        )
    if total_fresh_entries < min_total_fresh_entries:
        reasons.append(
            f"fresh entries {total_fresh_entries} below minimum "
            f"{min_total_fresh_entries}"
        )
    if article_count is not None and article_count != total_fresh_entries:
        reasons.append(
            f"article count {article_count} does not match source total "
            f"{total_fresh_entries}"
        )

    if critical_source_groups is None:
        critical_source_groups = []
    if not isinstance(critical_source_groups, list):
        raise ValueError("critical_source_groups must be a list")
    health_by_url = {str(item.get("url")): item for item in health}
    seen_group_names = set()
    for index, group in enumerate(critical_source_groups):
        if not isinstance(group, dict):
            raise ValueError(f"critical_source_groups[{index}] must be an object")
        name = group.get("name")
        sources = group.get("sources")
        if not isinstance(name, str) or not name.strip() or name in seen_group_names:
            raise ValueError(
                f"critical_source_groups[{index}] has invalid/duplicate name"
            )
        seen_group_names.add(name)
        if (
            not isinstance(sources, list)
            or not sources
            or any(not isinstance(url, str) or not url.strip() for url in sources)
            or len(sources) != len(set(sources))
        ):
            raise ValueError(
                f"critical source group {name} must contain unique source URLs"
            )
        legacy_healthy_gate = "min_available_sources" not in group
        legacy_fresh_gate = "min_current_sources" not in group
        min_group_available = group.get(
            "min_available_sources", group.get("min_healthy_sources", 1)
        )
        min_group_current = group.get(
            "min_current_sources", group.get("min_fresh_sources", 1)
        )
        for setting_name, value in (
            ("min_available_sources", min_group_available),
            ("min_current_sources", min_group_current),
        ):
            if type(value) is not int or not 0 <= value <= len(sources):
                raise ValueError(
                    f"critical source group {name} {setting_name} must be between "
                    f"0 and {len(sources)}"
                )
        content_max_age_hours = group.get("content_max_age_hours")
        if content_max_age_hours is not None and (
            isinstance(content_max_age_hours, bool)
            or not isinstance(content_max_age_hours, (int, float))
            or content_max_age_hours <= 0
        ):
            raise ValueError(
                f"critical source group {name} content_max_age_hours "
                "must be a positive number"
            )

        group_health = [health_by_url[url] for url in sources if url in health_by_url]
        missing = [url for url in sources if url not in health_by_url]
        group_healthy = sum(item.get("status") == "healthy" for item in group_health)
        group_fresh = sum(
            item.get("status") != "failed"
            and int(item.get("fresh_entries") or 0) > 0
            for item in group_health
        )
        group_available = sum(
            item.get("status") != "failed"
            and int(item.get("total_entries") or 0) > 0
            for item in group_health
        )
        if content_max_age_hours is None:
            group_current = group_fresh
        else:
            group_current = 0
            maximum_age = timedelta(hours=float(content_max_age_hours))
            for item in group_health:
                if item.get("status") == "failed":
                    continue
                newest = item.get("newest_published_at")
                if not newest:
                    continue
                newest_at = _aware_utc_timestamp(
                    newest,
                    f"critical source group {name} newest_published_at",
                )
                age = reference_time - newest_at
                if -timedelta(minutes=5) <= age <= maximum_age:
                    group_current += 1
        if missing:
            reasons.append(
                f"critical source group {name} missing {len(missing)} configured sources"
            )
        availability_count = (
            group_healthy if legacy_healthy_gate else group_available
        )
        availability_label = (
            "healthy sources" if legacy_healthy_gate else "available sources"
        )
        current_count = group_fresh if legacy_fresh_gate else group_current
        current_label = "fresh sources" if legacy_fresh_gate else "current sources"
        if availability_count < min_group_available:
            reasons.append(
                f"critical source group {name} {availability_label} "
                f"{availability_count} below "
                f"minimum {min_group_available}"
            )
        if current_count < min_group_current:
            reasons.append(
                f"critical source group {name} {current_label} {current_count} below "
                f"minimum {min_group_current}"
            )
        critical_group_summaries.append(
            {
                "name": name,
                "configured_sources": len(group_health),
                "expected_sources": len(sources),
                "available_sources": group_available,
                "current_sources": group_current,
                "content_max_age_hours": (
                    float(content_max_age_hours)
                    if content_max_age_hours is not None
                    else None
                ),
                "healthy_sources": group_healthy,
                "fresh_sources": group_fresh,
            }
        )
    if reasons:
        raise RuntimeError("RSS health check failed: " + "; ".join(reasons))

    return {
        "configured_sources": len(health),
        "failed_sources": len(failed),
        "healthy_sources": len(healthy),
        "fresh_sources": len(fresh),
        "total_fresh_entries": total_fresh_entries,
        "failure_ratio": failure_ratio,
        "healthy_ratio": healthy_ratio,
        "critical_source_groups": critical_group_summaries,
    }


def load_config(config_path=None):
    config_path = config_path or os.environ.get("RADAR_CONFIG", "config.yaml")
    # P4.1: Graceful fallback for missing config.yaml
    if not os.path.exists(config_path):
        example_path = os.path.join(
            os.path.dirname(os.path.abspath(config_path)), "config.example.yaml"
        )
        if not os.path.exists(example_path) and config_path == "config.yaml":
            example_path = "config.example.yaml"
        if os.path.exists(example_path):
            import shutil
            shutil.copy2(example_path, config_path)
            print(f"Warning: {config_path} not found. Auto-created from {example_path}.")
        else:
            raise FileNotFoundError(f"Missing both {config_path} and {example_path}!")
            
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

SCORED_ARTICLES_FIXTURE_SCHEMA_VERSION = 1


def load_scored_articles_fixture(path, rss_articles, config):
    """Attach validated deterministic scores to the exact ingested URL set."""
    fixture_path = os.path.abspath(os.fspath(path))
    try:
        with open(fixture_path, "r", encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Cannot load scored-articles fixture {fixture_path}: {error}"
        ) from error
    if not isinstance(fixture, dict):
        raise ValueError("scored-articles fixture must be a JSON object")
    if set(fixture) != {"schema_version", "scores"}:
        raise ValueError("scored-articles fixture has invalid top-level fields")
    if fixture.get("schema_version") != SCORED_ARTICLES_FIXTURE_SCHEMA_VERSION:
        raise ValueError("unsupported scored-articles fixture schema_version")
    scores = fixture.get("scores")
    if not isinstance(scores, list):
        raise ValueError("scored-articles fixture scores must be a list")

    rss_by_url = {}
    for article in rss_articles:
        link = article.get("link")
        if not isinstance(link, str) or not link.strip() or link in rss_by_url:
            raise ValueError("RSS articles must have unique non-empty links")
        rss_by_url[link] = article

    score_by_url = {}
    for index, entry in enumerate(scores):
        if not isinstance(entry, dict) or set(entry) != {"link", "score_data"}:
            raise ValueError(
                f"scored-articles fixture entry {index} must contain link and score_data"
            )
        link = entry.get("link")
        if not isinstance(link, str) or not link.strip() or link in score_by_url:
            raise ValueError(
                f"scored-articles fixture entry {index} has invalid/duplicate link"
            )
        score_by_url[link] = entry.get("score_data")

    rss_urls = set(rss_by_url)
    score_urls = set(score_by_url)
    if rss_urls != score_urls:
        missing = sorted(rss_urls - score_urls)
        unexpected = sorted(score_urls - rss_urls)
        raise ValueError(
            "scored-articles fixture URL mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )

    from score import _apply_composite_scores, _validate_score_result, _validate_weights

    weights = _validate_weights(config)
    scored_articles = []
    for article_id, article in enumerate(rss_articles):
        raw_score = score_by_url[article["link"]]
        if not isinstance(raw_score, dict):
            raise ValueError(
                f"scored-articles fixture score_data for {article['link']} must be an object"
            )
        score_data = _validate_score_result(dict(raw_score))
        score_data = _apply_composite_scores(score_data, weights)
        scored = dict(article)
        scored["id"] = article_id
        scored["score_data"] = score_data
        scored_articles.append(scored)
    return scored_articles


def generate_markdown_report(
    scored_articles, config, output_dir=None, *, deduplicate=True
):
    output_dir = output_dir or os.environ.get("RADAR_REPORTS_DIR", "reports")
    os.makedirs(output_dir, exist_ok=True)
    report_date = logical_today()
    date_str = report_date.isoformat()
    report_path = os.path.join(output_dir, f"industry_report_{date_str}.md")
    
    min_score = config.get("output", {}).get("min_score_to_keep", 6)
    lookback_days = config.get("output", {}).get("report_days_lookback", 2)
    
    cutoff_date_str = (report_date - timedelta(days=lookback_days)).isoformat()
    
    high_scoring = []
    for a in scored_articles:
        pub_date = a.get('published_at', '')[:10]
        if pub_date and (pub_date < cutoff_date_str or pub_date > date_str):
            continue
            
        sd = a.get('score_data', {})
        if not sd.get('is_relevant'):
            continue
            
        i_score = sd.get('innovation_score', 0)
        t_score = sd.get('traffic_score', 0)
        
        if i_score >= min_score or t_score >= min_score:
            high_scoring.append(a)
            
    if high_scoring and deduplicate:
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

    date_str = logical_date_text()
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
    reports_dir = os.environ.get("RADAR_REPORTS_DIR", "reports")
    
    print("Fetching articles from RSS feeds...", flush=True)
    hours_back = config.get("output", {}).get("hours_back", 48)
    rss_fixture = os.environ.get("RADAR_RSS_FIXTURE")
    if rss_fixture:
        print(f"Loading deterministic RSS fixture: {rss_fixture}", flush=True)
        articles, rss_health = load_rss_fixture(rss_fixture)
        effective_date = logical_today()
        validate_rss_fixture_effective_date(
            articles, rss_health, effective_date
        )
        rss_reference_time = datetime.combine(
            effective_date + timedelta(days=1),
            time.min,
            tzinfo=timezone.utc,
        ) - timedelta(microseconds=1)
    else:
        rss_reference_time = rss_reference_time_utc()
        articles, rss_health = fetch_rss_feeds(
            config.get("rss_feeds", []),
            hours_back=hours_back,
            now=rss_reference_time,
            return_health=True,
        )
    save_json_atomic(os.path.join(reports_dir, "rss_health.json"), rss_health)
    validate_rss_health(
        rss_health,
        max_failure_ratio=float(
            config.get("output", {}).get("rss_max_failure_ratio", 0.5)
        ),
        min_healthy_ratio=float(
            config.get("output", {}).get("rss_min_healthy_ratio", 0.0)
        ),
        min_fresh_sources=int(
            config.get("output", {}).get("rss_min_fresh_sources", 1)
        ),
        min_total_fresh_entries=int(
            config.get("output", {}).get("rss_min_total_fresh_entries", 1)
        ),
        min_configured_sources=int(
            config.get("output", {}).get("rss_min_configured_sources", 1)
        ),
        article_count=len(articles),
        critical_source_groups=config.get("rss_critical_source_groups", []),
        reference_time=rss_reference_time,
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

    scored_fixture = os.environ.get("RADAR_SCORED_ARTICLES_FIXTURE")
    if scored_fixture:
        print(
            f"Loading deterministic scored-articles fixture: {scored_fixture}",
            flush=True,
        )
        scored_articles = load_scored_articles_fixture(
            scored_fixture, articles, config
        )
        report_path = generate_markdown_report(
            scored_articles, config, deduplicate=False
        )
        print(f"\nReport generated successfully: {report_path}", flush=True)
        return report_path
    
    # Load incremental cache
    cache_data = load_cache()
    cache_updates = 0
    
    scoring_identities = validate_scoring_configuration(config)
    if scoring_identities:
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
